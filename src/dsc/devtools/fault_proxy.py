"""Loopback-only fault injector for development JSON control APIs."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from dsc.shared.observability import configure_logging, emit_event, logged


class FaultSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upstream: str
    mode: str = Field(pattern=r"^(delay|drop-before|drop-after)$")
    route_prefix: str = "/ingest"
    nth: int = Field(default=1, ge=1)
    delay_seconds: float = Field(default=2, ge=0, le=60, allow_inf_nan=False)
    allow_local_upstream: bool = False

    @model_validator(mode="after")
    def validate_target(self) -> FaultSettings:
        url = urlsplit(self.upstream)
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or (
                url.scheme != "https"
                and not (
                    self.allow_local_upstream
                    and url.scheme == "http"
                    and url.hostname == "127.0.0.1"
                )
            )
        ):
            raise ValueError("HTTPS upstream or explicit test loopback is required")
        if not self.route_prefix.startswith("/") or self.route_prefix.startswith("//"):
            raise ValueError("relative route prefix is required")
        return self


HOP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}


def make_server(settings: FaultSettings, port: int = 0) -> ThreadingHTTPServer:
    """Expose no public listener and forward only to one configured origin."""
    counter, lock = 0, threading.Lock()
    client = httpx.Client(
        timeout=httpx.Timeout(75, connect=5), follow_redirects=False, trust_env=False
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        @logged()
        def forward(self):
            nonlocal counter
            self.connection.settimeout(80)
            if not self.path.startswith("/") or self.path.startswith("//"):
                self.send_error(400)
                return
            if self.headers.get("Transfer-Encoding"):
                self.send_error(411)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if not 0 <= size <= 256 * 1024:
                self.send_error(413)
                return
            data = self.rfile.read(size)
            if len(data) != size:
                self.close_connection = True
                return
            inject = False
            if self.path.split("?", 1)[0].startswith(settings.route_prefix):
                with lock:
                    counter += 1
                    inject = counter == settings.nth
            if inject and settings.mode == "delay":
                time.sleep(settings.delay_seconds)
            if inject and settings.mode == "drop-before":
                self.drop()
                return
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            try:
                with client.stream(
                    self.command,
                    settings.upstream.rstrip("/") + self.path,
                    headers=headers,
                    content=data,
                ) as response:
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > 4 * 1024 * 1024:
                            self.send_error(502)
                            return
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if inject and settings.mode == "drop-after":
                        self.drop()
                        return
                    self.send_response(response.status_code)
                    for key, value in response.headers.items():
                        if key.lower() not in HOP_HEADERS | {"content-encoding"}:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
            except (httpx.HTTPError, OSError):
                self.close_connection = True
            self.close_connection = True

        def drop(self):
            emit_event("fault_injected")
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        do_GET = do_POST = do_PUT = forward

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            super().server_close()
            client.close()

    return Server(("127.0.0.1", port), Handler)


def main() -> int:
    configure_logging("fault-proxy")
    parser = argparse.ArgumentParser(description="Development JSON API fault proxy; loopback only")
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--mode", choices=("delay", "drop-before", "drop-after"), required=True)
    parser.add_argument("--route-prefix", default="/ingest")
    parser.add_argument("--nth", type=int, default=1)
    parser.add_argument("--delay-seconds", type=float, default=2)
    parser.add_argument("--allow-local-upstream", action="store_true")
    args = parser.parse_args()
    settings = FaultSettings(
        upstream=args.upstream,
        mode=args.mode,
        route_prefix=args.route_prefix,
        nth=args.nth,
        delay_seconds=args.delay_seconds,
        allow_local_upstream=args.allow_local_upstream,
    )
    with make_server(settings, args.port) as server:
        print(
            json.dumps(
                {
                    "listen": "127.0.0.1",
                    "port": server.server_port,
                    "mode": settings.mode,
                    "nth": settings.nth,
                }
            ),
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
