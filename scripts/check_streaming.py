"""Run an isolated synthetic WebRTC stack. Requires Docker and a Playwright browser driver."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], timeout: int = 60) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed: {command[0]} (output retained only in local logs)")
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic local streaming acceptance")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser-driver", type=Path, required=True)
    parser.add_argument("--node", default="node")
    parser.add_argument("--port", type=int, default=18090)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    project = "dsc-live-check-" + uuid4().hex[:8]
    names = [project + suffix for suffix in ["-proxy", "-gateway", "-source"]]
    fixture = args.output.resolve()
    media = {
        "logLevel": "error",
        "authMethod": "http",
        "authHTTPAddress": "http://127.0.0.1:8090/internal/authorize",
        "authHTTPExclude": [{"action": "api"}],
        "api": True,
        "apiAddress": "127.0.0.1:9997",
        "webrtcAddress": "127.0.0.1:8889",
        "webrtcAdditionalHosts": ["127.0.0.1"],
        "webrtcIPsFromInterfaces": False,
        "webrtcLocalUDPAddress": ":8189",
        "webrtcLocalTCPAddress": ":8189",
        "webrtcAllowOrigins": [f"http://127.0.0.1:{args.port}"],
        **{
            name: False
            for name in ["rtsp", "rtmp", "hls", "srt", "moq", "playback", "metrics", "pprof"]
        },
        "paths": {
            "camera-1": {
                "source": f"rtsp://{names[2]}:8554/test",
                "rtspTransport": "tcp",
                "sourceOnDemand": True,
                "maxReaders": 4,
            }
        },
    }
    (fixture / "mediamtx.json").write_text(json.dumps(media))
    (fixture / "mediamtx.json").chmod(0o600)
    config = (ROOT / "deploy/streaming/nginx.conf.example").read_text()
    config = config.replace("listen 100.64.0.10:443 ssl;", f"listen {args.port};")
    config = "\n".join(
        line
        for line in config.splitlines()
        if not line.strip().startswith(("ssl_", "add_header Strict", "add_header Content"))
    )
    (fixture / "nginx.conf").write_text(config)
    try:
        run(["docker", "network", "create", project])
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                names[2],
                "--network",
                project,
                "--entrypoint",
                "/bin/sh",
                "bluenviron/mediamtx:1.21.4-ffmpeg",
                "-c",
                "/mediamtx >/tmp/source.log 2>&1 & sleep 1; "
                "exec ffmpeg -hide_banner -loglevel error "
                "-re -f lavfi -i testsrc=size=640x360:rate=15 -pix_fmt yuv420p "
                "-c:v libx264 -profile:v baseline -tune zerolatency -g 15 -bf 0 "
                "-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/test",
            ],
            timeout=180,
        )
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                names[1],
                "--network",
                project,
                "-p",
                f"127.0.0.1:{args.port}:{args.port}",
                "-p",
                "127.0.0.1:18080:18080",
                "-p",
                "127.0.0.1:8189:8189/udp",
                "-p",
                "127.0.0.1:8189:8189/tcp",
                "-e",
                f"DSC_TEST_BROWSER_PORT={args.port}",
                "-v",
                f"{fixture}:/fixture:ro",
                "-v",
                f"{ROOT / 'tests'}:/tests:ro",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "dsc-live-gateway:1.3.0",
                "python",
                "/tests/support/live_rehearsal.py",
            ]
        )
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                names[0],
                "--network",
                f"container:{names[1]}",
                "-v",
                f"{fixture / 'nginx.conf'}:/etc/nginx/conf.d/default.conf:ro",
                "nginx:1.28-alpine",
            ],
            timeout=180,
        )
        time.sleep(3)
        result = subprocess.run(
            [args.node, str(args.browser_driver.resolve()), str(args.port), str(fixture)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        (fixture / "browser.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("browser acceptance failed; inspect browser.log")
        run(
            [
                "docker",
                "exec",
                names[1],
                "python",
                "-c",
                "import os,signal,pathlib; "
                "children=pathlib.Path('/proc/1/task/1/children').read_text().split(); "
                "assert len(children)==1; os.kill(int(children[0]),signal.SIGKILL)",
            ]
        )
        run(["docker", "wait", names[1]], timeout=20)
        report = json.loads((fixture / "result.json").read_text())
        report["checks"].append("Media process failure stops the supervised gateway")
        (fixture / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        for name in names:
            result = subprocess.run(
                ["docker", "logs", "--tail", "200", name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            (fixture / f"{name}.log").write_text(result.stdout + result.stderr)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
        subprocess.run(["docker", "network", "rm", project], capture_output=True, timeout=15)


if __name__ == "__main__":
    main()
