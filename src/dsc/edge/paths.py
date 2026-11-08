import logging
import os
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from dsc.shared.observability import logged


def relative_parts(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or "\\" in value or "\x00" in value:
        raise ValueError("relative NAS path required")
    if any(p in {"..", ".", ""} for p in value.split("/")):
        raise ValueError("invalid path segment")
    return path.parts


@logged(level=logging.DEBUG)
def check_path(root: Path, value: str) -> None:
    parts = relative_parts(value)
    if not root.joinpath(*parts).resolve().is_relative_to(root.resolve()):
        raise ValueError("path leaves allowed NAS root")


@contextmanager
@logged(level=logging.DEBUG)
def open_clip(root: Path, value: str):
    """Walk descriptors with O_NOFOLLOW so path swaps cannot escape the mounted NAS root."""
    parts = relative_parts(value)
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("secure descriptor traversal requires a POSIX edge host")
    descriptors = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        target = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(target, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("clip must be a regular file")
            yield stream
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
