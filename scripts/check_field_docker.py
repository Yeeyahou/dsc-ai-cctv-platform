"""Exercise the offline field container with a finalized local MP4; stdlib only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/edge/compose.field-local.yaml"
CONTAINER_ROOT = "/var/lib/dsc/field-local"


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def publish(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".docker-check-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise ValueError("refusing to overwrite conflicting input") from None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def run(video: Path, duration: float, *, build: bool) -> None:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("--duration-seconds must be finite and positive")
    if not 0 < video.stat().st_size <= 512 * 1024 * 1024:
        raise ValueError("MP4 must be at most 512 MiB")
    if os.getuid() == 0:
        raise ValueError("run this local rehearsal as a non-root user")
    with video.open("rb") as stream:
        if stream.read(12)[4:8] != b"ftyp":
            raise ValueError("supply a finalized MP4 file")
    parent = ROOT / "runtime/field-docker"
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    project = f"dsc-field-local-{uuid4().hex[:10]}"
    env_file = directory / "compose.env"
    env_file.write_text(
        f"COMPOSE_PROJECT_NAME={project}\n"
        f"DSC_FIELD_LOCAL_DIR={json.dumps(str(directory))}\n"
        f"DSC_FIELD_UID={os.getuid()}\nDSC_FIELD_GID={os.getgid()}\n"
    )
    env_file.chmod(0o600)
    base = ["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE)]
    prefix = shlex.join(base)
    (directory / "USAGE.md").write_text(
        "# 이 맥북의 시험 컨테이너 사용법\n\n"
        "네트워크가 차단된 개발용 엣지가 파일 입력을 기다리는 상태다. "
        "실제 카메라 수신·분석, AWS 전송과 NAS 복제는 수행하지 않는다.\n\n"
        f"영속 데이터 폴더: `{directory}`\n\n"
        "상태 확인:\n\n```sh\n" + prefix + " ps\n```\n\n"
        "최근 로그:\n\n```sh\n" + prefix + " logs --tail=30 field-local\n```\n\n"
        "중지:\n\n```sh\n" + prefix + " stop field-local\n```\n\n"
        "다시 시작:\n\n```sh\n" + prefix + " start field-local\n```\n\n"
        "컨테이너를 제거해도 위 폴더는 남는다. 폴더를 삭제하면 DB·영상도 사라진다. "
        "이 시험 데이터를 운영 환경의 등록된 엣지 데이터로 사용하지 않는다.\n"
    )

    def docker(arguments: list[str], *, compose: bool = True, timeout: int = 30) -> str:
        command = [*(base if compose else ["docker"]), *arguments]
        completed = subprocess.run(
            command, cwd=ROOT, text=True, capture_output=True, timeout=timeout
        )
        with (directory / "docker.log").open("a") as log:
            log.write(completed.stderr)
            if completed.returncode:
                log.write(completed.stdout)
        if completed.returncode:
            raise RuntimeError(f"Docker command failed; inspect {directory / 'docker.log'}")
        return completed.stdout

    def snapshot() -> dict:
        return json.loads(
            docker(
                [
                    "exec",
                    "-T",
                    "field-local",
                    "python",
                    "-m",
                    "dsc.devtools.field_local",
                    "--root",
                    CONTAINER_ROOT,
                    "--inspect",
                ]
            )
        )

    def wait_received() -> dict:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            state = snapshot()
            if (
                state["ready"]
                and len(state["outbox"]) == 2
                and len(state["copies"]) == 1
                and not list((directory / "inbox").glob("*.json"))
            ):
                return state
            time.sleep(0.25)
        raise TimeoutError("local receiver did not accept the input before its deadline")

    checks = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            raise RuntimeError(message)
        checks.append(message)
        print(f"PASS  {message}", flush=True)

    try:
        if build:
            print("Building field image...", flush=True)
            docker(["build", "field-local"], timeout=600)
        docker(["up", "--detach", "--wait", "field-local"], timeout=60)
        container_before = docker(["ps", "-q", "field-local"]).strip()
        info = json.loads(docker(["inspect", container_before], compose=False))[0]
        check(info["HostConfig"]["NetworkMode"] == "none", "시험 컨테이너의 외부 네트워크 차단")
        check(
            info["Config"]["User"].split(":")[0] not in {"", "0", "root"}
            and info["HostConfig"]["ReadonlyRootfs"],
            "일반 사용자·읽기 전용 컨테이너로 실행",
        )
        event_id, clip_id = str(uuid4()), str(uuid4())
        relative = f"camera-01/{clip_id}.mp4"
        clip = directory / "input-clips" / relative
        clip.parent.mkdir(parents=True, exist_ok=True)
        source_hash = checksum(video)
        with video.open("rb") as source, clip.open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        check(checksum(clip) == source_hash, "실제 녹화 파일을 변경 없이 시험 입력으로 복사")
        ended = datetime.now(UTC)
        started = ended - timedelta(seconds=duration)
        event = {
            "kind": "event",
            "event": {
                "schemaVersion": "1.0",
                "eventId": event_id,
                "eventCode": "DSC-101",
                "siteId": "local-test-site",
                "occurredAt": started.isoformat(),
                "assets": [{"assetId": "local-test-camera", "role": "detector"}],
                "modelVersion": "docker-local-mock",
                "media": {"clipId": clip_id, "status": "RECORDING"},
            },
        }
        media = {
            "kind": "media",
            "eventId": event_id,
            "media": {
                "clipId": clip_id,
                "status": "AVAILABLE",
                "revision": 1,
                "startedAt": started.isoformat(),
                "endedAt": ended.isoformat(),
                "contentType": "video/mp4",
                "sizeBytes": clip.stat().st_size,
            },
            "localMedia": {"clipId": clip_id, "relativePath": relative},
        }
        inputs = {
            f"{event_id}.event.json": json.dumps(event).encode(),
            f"{event_id}.1.media.json": json.dumps(media).encode(),
        }
        for name, content in inputs.items():
            publish(directory / "producer" / name, content)
        partial = directory / "inbox/unfinished.json.tmp"
        publish(partial, b'{"kind":')

        def replay() -> None:
            for name, content in inputs.items():
                publish(directory / "inbox" / name, content)

        replay()
        first = wait_received()
        (directory / "before.json").write_text(json.dumps(first, ensure_ascii=False, indent=2))
        check(
            not list((directory / "inbox/rejected").glob("*.json")),
            "이벤트와 녹화 완료 알림 정상 접수",
        )
        managed = directory / "managed-clips" / first["copies"][0]["relative_path"]
        check(
            managed.stat().st_size == video.stat().st_size and checksum(managed) == source_hash,
            "엣지 관리 사본의 크기·SHA-256이 실제 녹화 원본과 일치",
        )
        docker(["restart", "--timeout", "10", "field-local"])
        restarted = wait_received()
        check(restarted == first, "컨테이너 재시작 후 이벤트·클립 정보 유지")
        docker(["down", "--timeout", "10"])
        docker(["up", "--detach", "--wait", "field-local"], timeout=60)
        container_after = docker(["ps", "-q", "field-local"]).strip()
        recreated = wait_received()
        check(
            container_before != container_after
            and recreated == first
            and checksum(managed) == source_hash,
            "컨테이너 삭제·재생성 후에도 맥북의 DB와 영상 보존",
        )
        replay()
        repeated = wait_received()
        check(
            repeated == first and not list((directory / "inbox/rejected").glob("*.json")),
            "같은 이벤트·클립 재입력 시 중복 생성 없음",
        )
        check(partial.read_bytes() == b'{"kind":', "작성 중인 임시 JSON 보존")
        check(
            all(row["state"] == "PENDING" for row in repeated["outbox"]),
            "클라우드 저장 확인 전 미전송 PENDING 상태 유지",
        )
        check(checksum(video) == source_hash, "모든 시험 후 실제 녹화 원본 보존")
        (directory / "after.json").write_text(json.dumps(repeated, ensure_ascii=False, indent=2))
        result = {
            "result": "PASS",
            "checks": checks,
            "project": project,
            "composeEnv": str(env_file),
            "containerId": container_after,
            "sourceVideo": str(video),
            "managedVideo": str(managed),
            "durationSeconds": duration,
            "sizeBytes": video.stat().st_size,
            "sha256": source_hash,
            "eventId": event_id,
            "clipId": clip_id,
            "eventIsMock": True,
            "eventTimeIsMock": True,
            "awsTested": False,
            "physicalNasTested": False,
            "aiInferenceTested": False,
        }
        (directory / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        print(f"\nResult: {directory / 'result.json'}\nCompose env: {env_file}")
        print(f"Usage: {directory / 'USAGE.md'}")
        print("Local receiver remains running; no AWS/NAS transmission.")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        (directory / "result.json").write_text(
            json.dumps({"result": "FAIL", "checksPassed": checks, "project": project})
        )
        print(f"Preserved evidence: {directory}", flush=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    run(args.video.resolve(), args.duration_seconds, build=not args.no_build)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
