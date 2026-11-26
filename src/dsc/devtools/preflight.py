"""Inspect installation inputs and optional NAS semantics without exposing secrets."""

import argparse
import json
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from dsc.edge.config import EdgeSettings
from dsc.shared.observability import configure_logging, logged
from dsc.streaming.config import GatewaySettings
from dsc.streaming.install import Installation, media_config


def private_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise PermissionError("private file must have mode 0600")


@logged()
def nas_probe(root: Path, volume_id: str) -> None:
    if root.is_symlink() or not root.is_dir() or not volume_id:
        raise ValueError("a provisioned NAS root and volume ID are required")
    marker = root / ".dsc-volume-id"
    if marker.is_symlink() or marker.read_text().strip() != volume_id:
        raise ValueError("NAS volume marker mismatch")
    # Only a new scratch directory is touched; existing clips and manifests are never opened.
    with tempfile.TemporaryDirectory(prefix=".dsc-preflight-", dir=root) as directory:
        base = Path(directory)
        temporary = base / "partial"
        payload = os.urandom(4096)
        with temporary.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        committed = base / "committed"
        os.link(temporary, committed)
        temporary.unlink()
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if committed.read_bytes() != payload:
            raise ValueError("NAS read-after-write mismatch")


@logged()
def run(args) -> list[dict]:
    results = []

    def check(name, action):
        try:
            action()
            results.append({"check": name, "status": "PASS"})
        except (ValueError, OSError, httpx.HTTPError) as exc:
            results.append({"check": name, "status": "FAIL", "errorType": type(exc).__name__})

    for kind, path in [("field", args.field_env), ("office", args.office_env)]:
        if path:

            def edge_check(path=path, kind=kind):
                private_file(path)
                settings = EdgeSettings(_env_file=path)
                if settings.role != ("archive" if kind == "office" else "field"):
                    raise ValueError("wrong installation role")
                private_file(settings.secret_file)
                if not settings.secret_file.read_text().strip():
                    raise ValueError("empty client secret")
                for directory in [settings.inbox, settings.state_dir, settings.nas_root]:
                    if not directory.is_dir():
                        raise ValueError("installation directory is missing")

            check(kind + "-configuration", edge_check)
    if args.live_env:

        def live_check():
            private_file(args.live_env)
            settings = GatewaySettings(_env_file=args.live_env)
            private_file(settings.secret_file)
            private_file(settings.mediamtx_config)
            if not settings.state_dir.is_dir():
                raise ValueError("live state directory is missing")

        check("live-configuration", live_check)
    if args.installation:
        check(
            "camera-configuration",
            lambda: media_config(Installation.model_validate_json(args.installation.read_text())),
        )
        if args.network:
            install = Installation.model_validate_json(args.installation.read_text())
            for index, camera in enumerate(install.cameras, 1):
                url = urlsplit(camera.rtsp_url)

                def connect(url=url):
                    with socket.create_connection((url.hostname, url.port or 554), timeout=3):
                        pass

                check(f"camera-{index}-tcp", connect)
    if args.nas_root:
        check("nas-file-semantics", lambda: nas_probe(args.nas_root, args.volume_id))
    if not results:
        raise ValueError("select at least one installation input")
    return results


def main() -> int:
    configure_logging("installation-preflight")
    parser = argparse.ArgumentParser(description="DSC installation preflight; sanitized output")
    parser.add_argument("--field-env", type=Path)
    parser.add_argument("--office-env", type=Path)
    parser.add_argument("--live-env", type=Path)
    parser.add_argument("--installation", type=Path)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--nas-root", type=Path)
    parser.add_argument("--volume-id", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        results = run(args)
        value = {
            "checks": results,
            "physicalAcceptance": "NOT_RUN",
            "notes": "파일 기능 확인은 정전 영속성·마운트 정체성·백업 복원 검수가 아닙니다",
        }
        with args.output.open("x") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        print(json.dumps(value, ensure_ascii=False))
        return int(any(row["status"] == "FAIL" for row in results))
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "FAIL", "errorType": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
