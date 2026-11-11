"""Durable immutable clips in service-owned directories."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Self
from uuid import uuid4

from dsc.edge.config import EdgeSettings
from dsc.edge.paths import open_clip, relative_parts
from dsc.shared.contracts import Media, MediaUpdate
from dsc.shared.observability import log_methods, logged


@logged(level=logging.DEBUG)
def fingerprint(stream: BinaryIO) -> tuple[int, str]:
    before = os.fstat(stream.fileno())
    checksum = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        checksum.update(chunk)
        size += len(chunk)
    after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or size != before.st_size:
        raise ValueError("clip changed while being read")
    stream.seek(0)
    return size, base64.b64encode(checksum.digest()).decode()


@contextmanager
def copy_lock(
    state_dir: Path, event_id: str, revision: int, exclusive: bool = False
) -> Iterator[None]:
    directory = state_dir / "clip-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(f"{event_id}:{revision}".encode()).hexdigest()
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


@contextmanager
def clip_parent(root: Path, relative_path: str, create: bool = False) -> Iterator[tuple[int, str]]:
    parts = relative_parts(relative_path)
    descriptors = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        yield fd, parts[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@log_methods(level=logging.DEBUG, include=("__enter__", "__exit__"))
class ImmutableWriter:
    def __init__(self, root: Path, relative_path: str, size: int, checksum: str | None = None):
        self.root, self.relative_path = root, relative_path
        self.expected_size, self.expected_checksum = size, checksum
        self.size, self.hash = 0, hashlib.sha256()

    def __enter__(self) -> Self:
        self.parent = clip_parent(self.root, self.relative_path, create=True)
        self.fd, self.name = self.parent.__enter__()
        # The caller holds this clip's exclusive lock, so partials belong to stopped attempts.
        for name in os.listdir(self.fd):
            if name.startswith(f".{self.name}.") and name.endswith(".partial"):
                os.unlink(name, dir_fd=self.fd)
        self.temporary = f".{self.name}.{uuid4()}.partial"
        try:
            fd = os.open(
                self.temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.fd,
            )
            self.stream = os.fdopen(fd, "wb")
        except BaseException:
            self.parent.__exit__(None, None, None)
            raise
        return self

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.expected_size:
            raise ValueError("clip exceeds its declared size")
        self.stream.write(chunk)
        self.hash.update(chunk)

    def finish(self) -> tuple[int, str]:
        checksum = base64.b64encode(self.hash.digest()).decode()
        if self.size != self.expected_size or (
            self.expected_checksum and checksum != self.expected_checksum
        ):
            raise ValueError("clip size or SHA-256 mismatch")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        try:
            os.link(
                self.temporary,
                self.name,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            with open_clip(self.root, self.relative_path) as existing:
                if fingerprint(existing) != (self.size, checksum):
                    raise ValueError("immutable destination contains different bytes") from None
        os.fsync(self.fd)
        return self.size, checksum

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.stream.close()
            os.unlink(self.temporary, dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            self.parent.__exit__(exc_type, exc, traceback)


@logged(level=logging.DEBUG)
def check_volume(root: Path, identity: str) -> None:
    with open_clip(root, ".dsc-volume-id") as stream:
        if stream.read(512).decode().strip() != identity:
            raise OSError("archive volume identity mismatch")


@logged(level=logging.DEBUG)
def stage_clip(
    settings: EdgeSettings, event_id: str, media: Media | MediaUpdate, relative_path: str
) -> dict:
    from dsc.shared.contracts import milliseconds

    revision = getattr(media, "revision", 0)
    destination = f"{event_id}/{revision}/{media.clip_id}.mp4"
    settings.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with copy_lock(settings.state_dir, event_id, revision, exclusive=True):
        with open_clip(settings.nas_root, relative_path) as source:
            before = os.fstat(source.fileno())
            if before.st_size != media.size_bytes or before.st_size > settings.max_clip_bytes:
                raise ValueError("local clip size differs from media metadata")
            with ImmutableWriter(settings.cache_root, destination, media.size_bytes) as writer:
                while chunk := source.read(1024 * 1024):
                    writer.write(chunk)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError("producer clip changed during local acceptance")
                size, checksum = writer.finish()
    return {
        "event_id": event_id,
        "clip_id": str(media.clip_id),
        "revision": revision,
        "relative_path": destination,
        "size_bytes": size,
        "checksum": checksum,
        "created_at": milliseconds(media.ended_at),
    }


@logged(level=logging.DEBUG)
def delete_copy(root: Path, relative_path: str, size: int, checksum: str) -> None:
    with open_clip(root, relative_path) as stream:
        if fingerprint(stream) != (size, checksum):
            raise ValueError("refusing to delete a changed clip")
        info = os.fstat(stream.fileno())
        with clip_parent(root, relative_path) as (fd, name):
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("clip path changed before deletion")
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)
