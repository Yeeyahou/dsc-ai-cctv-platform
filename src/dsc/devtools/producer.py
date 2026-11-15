"""Synthetic producer for the v1.2 local inbox contract."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from dsc.shared.contracts import (
    EVENT_CODES,
    EventEnvelope,
    EventInput,
    MediaEnvelope,
    canonical,
)
from dsc.shared.observability import configure_logging, logged

CHANNEL_CODES = {
    "cctv": sorted(code for code in EVENT_CODES if code.startswith("DSC-1")),
    "robot": sorted(code for code in EVENT_CODES if code.startswith("DSC-2")),
}


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(path: Path, data: bytes) -> None:
    """Make a complete file visible without overwriting a conflicting identity."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".producer-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise ValueError("existing identity has different bytes") from None
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@logged()
def synthetic_video(ffmpeg: str, path: Path, color: str, duration: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.mp4")
    try:
        subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x{color}:s=160x120:r=10:d={duration}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        publish(path, temporary.read_bytes())
    finally:
        temporary.unlink(missing_ok=True)


@logged()
def prepare_run(
    run_dir: Path,
    *,
    channel: str,
    site_id: str,
    asset_id: str,
    count: int,
    seed: int,
    started_at: datetime,
    video: Path | None = None,
    ffmpeg: str = "ffmpeg",
) -> dict:
    """Persist the complete plan before publishing any input to a running edge."""
    if channel not in CHANNEL_CODES or not 1 <= count <= 10000:
        raise ValueError("channel or count is invalid")
    if started_at.tzinfo is None:
        raise ValueError("started_at must include a timezone")
    run_dir.mkdir(parents=True, exist_ok=True)
    if (run_dir / "plan.json").exists():
        raise ValueError("run already exists; use --replay with its plan")
    if video:
        if not 0 < video.stat().st_size <= 512 * 1024 * 1024:
            raise ValueError("synthetic MP4 must be at most 512 MiB")
        with video.open("rb") as stream:
            if stream.read(12)[4:8] != b"ftyp":
                raise ValueError("--video must be an MP4 file")
    run_id = str(uuid4())
    rng = random.Random(seed)
    entries = []
    pending_pair: tuple[str, str] | None = None
    for index in range(count):
        if pending_pair:
            code, incident = pending_pair
            pending_pair = None
        else:
            codes = [c for c in CHANNEL_CODES[channel] if c[-1] != "B" or c == "DSC-103B"]
            if index == count - 1:
                codes = [
                    c for c in codes if c not in {"DSC-102A", "DSC-104A", "DSC-106A", "DSC-202A"}
                ]
            code = rng.choice(codes)
            incident = str(uuid5(UUID(run_id), f"incident:{index}"))
            if code in {"DSC-102A", "DSC-104A", "DSC-106A", "DSC-202A"}:
                pending_pair = (code[:-1] + "B", incident)
        event_id = str(uuid5(UUID(run_id), f"event:{index}"))
        clip_id = str(uuid5(UUID(run_id), f"clip:{index}"))
        occurred = started_at + timedelta(seconds=index * 5)
        duration = rng.randint(1, 3)
        relative = f"{run_id}/{clip_id}.mp4"
        clip = run_dir / "clips" / relative
        if video:
            publish(clip, video.read_bytes())
            duration = 1
        else:
            synthetic_video(ffmpeg, clip, f"{rng.randrange(0x1000000):06x}", duration)
        media = {"clipId": clip_id, "status": "RECORDING"}
        body = {
            "schemaVersion": "1.0",
            "eventId": event_id,
            "eventCode": code,
            "siteId": site_id,
            "occurredAt": occurred,
            "assets": [{"assetId": asset_id, "role": "detector"}],
            "modelVersion": "synthetic-producer-1",
            "media": media,
        }
        if code in {
            "DSC-102A",
            "DSC-102B",
            "DSC-104A",
            "DSC-104B",
            "DSC-106A",
            "DSC-106B",
            "DSC-202A",
            "DSC-202B",
        }:
            body["incidentId"] = incident
        event = EventEnvelope(kind="event", event=EventInput.model_validate(body))
        update = MediaEnvelope.model_validate(
            {
                "kind": "media",
                "eventId": event_id,
                "media": {
                    **media,
                    "status": "AVAILABLE",
                    "revision": 1,
                    "startedAt": occurred,
                    "endedAt": occurred + timedelta(seconds=duration),
                    "contentType": "video/mp4",
                    "sizeBytes": clip.stat().st_size,
                },
                "localMedia": {"clipId": clip_id, "relativePath": relative},
            }
        )
        names = [f"{event_id}.event.json", f"{event_id}.1.media.json"]
        for name, envelope in zip(names, (event, update), strict=True):
            publish(run_dir / "envelopes" / name, (canonical(envelope.wire()) + "\n").encode())
        entries.append(
            {
                "eventId": event_id,
                "clipId": clip_id,
                "eventCode": code,
                "relativePath": relative,
                "sizeBytes": clip.stat().st_size,
                "sha256": hashlib.sha256(clip.read_bytes()).hexdigest(),
                "envelopes": names,
                "envelopeHashes": {
                    name: hashlib.sha256((run_dir / "envelopes" / name).read_bytes()).hexdigest()
                    for name in names
                },
            }
        )
    plan = {
        "version": 1,
        "runId": run_id,
        "channel": channel,
        "siteId": site_id,
        "assetId": asset_id,
        "seed": seed,
        "entries": entries,
    }
    publish(run_dir / "plan.json", (canonical(plan) + "\n").encode())
    return plan


@logged()
def replay(run_dir: Path, inbox: Path, clip_root: Path, interval: float = 0) -> dict:
    if not math.isfinite(interval) or interval < 0 or interval > 3600:
        raise ValueError("interval must be between 0 and 3600 seconds")
    plan = json.loads((run_dir / "plan.json").read_text())
    # Check every saved input before exposing any of this run to the consumer.
    for entry in plan["entries"]:
        relative = Path(entry["relativePath"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe clip path in plan")
        clip = run_dir / "clips" / relative
        if (
            clip.stat().st_size != entry["sizeBytes"]
            or hashlib.sha256(clip.read_bytes()).hexdigest() != entry["sha256"]
        ):
            raise ValueError("saved clip changed; replay refused")
        for name in entry["envelopes"]:
            if Path(name).name != name:
                raise ValueError("unsafe envelope name")
            data = (run_dir / "envelopes" / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["envelopeHashes"][name]:
                raise ValueError("saved envelope changed; replay refused")
            raw = json.loads(data)
            if raw["kind"] == "event":
                envelope = EventEnvelope.model_validate(raw)
                expected = f"{envelope.event.event_id}.event.json"
                if str(envelope.event.event_id) != entry["eventId"]:
                    raise ValueError("event identity mismatch")
            else:
                envelope = MediaEnvelope.model_validate(raw)
                expected = f"{envelope.event_id}.{envelope.media.revision}.media.json"
                if (
                    str(envelope.event_id) != entry["eventId"]
                    or not envelope.local_media
                    or envelope.local_media.relative_path != str(relative)
                    or envelope.media.size_bytes != entry["sizeBytes"]
                ):
                    raise ValueError("media identity mismatch")
            if name != expected:
                raise ValueError("filename identity mismatch")
    for index, entry in enumerate(plan["entries"]):
        relative = entry["relativePath"]
        clip_data = (run_dir / "clips" / relative).read_bytes()
        if (
            len(clip_data) != entry["sizeBytes"]
            or hashlib.sha256(clip_data).hexdigest() != entry["sha256"]
        ):
            raise ValueError("saved clip changed during replay")
        publish(clip_root / relative, clip_data)
        for name in entry["envelopes"]:
            data = (run_dir / "envelopes" / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["envelopeHashes"][name]:
                raise ValueError("saved envelope changed during replay")
            publish(inbox / name, data)
        if interval and index + 1 < len(plan["entries"]):
            time.sleep(interval)
    return {
        "runId": plan["runId"],
        "events": len(plan["entries"]),
        "envelopes": len(plan["entries"]) * 2,
    }


def main() -> int:
    configure_logging("producer-mock")
    parser = argparse.ArgumentParser(
        description="Synthetic AI/recorder inbox producer; no AWS access"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--clip-root", type=Path, required=True)
    parser.add_argument("--channel", choices=CHANNEL_CODES, default="cctv")
    parser.add_argument("--site-id", default="site-1")
    parser.add_argument("--asset-id", default="camera-1")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--interval", type=float, default=0)
    parser.add_argument("--started-at", type=datetime.fromisoformat)
    parser.add_argument(
        "--video", type=Path, help="Existing synthetic MP4, treated as a one-second clip"
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (args.run_dir / ".producer.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not args.replay:
                if not args.video and not shutil.which(args.ffmpeg):
                    parser.error(
                        "ffmpeg is required for random videos; or supply --video synthetic.mp4"
                    )
                prepare_run(
                    args.run_dir,
                    channel=args.channel,
                    site_id=args.site_id,
                    asset_id=args.asset_id,
                    count=args.count,
                    seed=args.seed,
                    started_at=args.started_at or datetime.now(UTC),
                    video=args.video,
                    ffmpeg=args.ffmpeg,
                )
            print(json.dumps(replay(args.run_dir, args.inbox, args.clip_root, args.interval)))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(
            f"producer failed: {type(exc).__name__}; preserve run directory for inspection",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
