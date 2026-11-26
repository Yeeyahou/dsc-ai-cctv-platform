import argparse
import json
import logging
import subprocess
import threading

import uvicorn

from dsc.shared.observability import configure_logging, contextual, emit_event, logged
from dsc.streaming.config import GatewaySettings
from dsc.streaming.gateway import Gateway, create_app


@logged()
def serve(settings: GatewaySettings) -> None:
    if settings.secret_file.stat().st_mode & 0o077:
        raise PermissionError("field client secret must have mode 0600")
    if not settings.secret_file.read_text().strip():
        raise ValueError("field client secret is empty")
    if settings.mediamtx_config.stat().st_mode & 0o077:
        raise PermissionError("MediaMTX configuration must have mode 0600")
    config = json.loads(settings.mediamtx_config.read_text())
    required = {
        "authMethod": "http",
        "authHTTPAddress": "http://127.0.0.1:8090/internal/authorize",
        "authHTTPExclude": [{"action": "api"}],
        "api": True,
        "apiAddress": "127.0.0.1:9997",
        "webrtcAddress": "127.0.0.1:8889",
        "rtsp": False,
        "rtmp": False,
        "hls": False,
        "srt": False,
        "moq": False,
        "playback": False,
        "metrics": False,
        "pprof": False,
    }
    if any(config.get(key) != value for key, value in required.items()):
        raise ValueError("use dsc-live-config to generate the restricted configuration")
    gateway = Gateway(settings)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(gateway),
            host="0.0.0.0",
            port=8090,
            access_log=False,
            log_level="warning",
            timeout_graceful_shutdown=10,
            proxy_headers=False,
        )
    )
    stopped = threading.Event()
    # Native diagnostics can include credential-bearing RTSP URLs. Emit only sanitized events.
    process = subprocess.Popen(
        [str(settings.mediamtx_binary), str(settings.mediamtx_config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    @contextual
    def supervise() -> None:
        try:
            # MediaMTX starts its control listener before servicing viewers.
            for _ in range(20):
                if process.poll() is not None or stopped.wait(0.25):
                    return
                try:
                    gateway.reconcile()
                    break
                except Exception:
                    continue
            else:
                raise RuntimeError("media startup failed")
            while not stopped.wait(settings.poll_seconds):
                if process.poll() is not None:
                    raise RuntimeError("media process stopped")
                gateway.reconcile()
        except Exception as exc:
            emit_event("live_supervisor_failed", level=logging.ERROR, errorType=type(exc).__name__)
        finally:
            server.should_exit = True
            if process.poll() is None:
                process.terminate()

    thread = threading.Thread(target=supervise, name="live-supervisor", daemon=True)
    thread.start()
    try:
        server.run()
    finally:
        stopped.set()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        thread.join(timeout=30)
        gateway.close()


def main() -> int:
    configure_logging("live-gateway")
    parser = argparse.ArgumentParser(description="DSC supervised WebRTC gateway")
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    try:
        serve(GatewaySettings(_env_file=args.env_file))
    except Exception as exc:
        emit_event("live_start_failed", level=logging.ERROR, errorType=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
