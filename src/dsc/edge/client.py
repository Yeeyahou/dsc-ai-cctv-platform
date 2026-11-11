import logging
import os
import threading
import time

import httpx

from dsc.edge.config import EdgeSettings
from dsc.shared.observability import emit_event, log_methods


@log_methods(level=logging.DEBUG, include=("_token",))
class EdgeClient:
    def __init__(self, settings: EdgeSettings, transport=None, clock=time.time):
        self.settings, self.clock = settings, clock
        self.http = httpx.Client(
            timeout=httpx.Timeout(settings.request_timeout, connect=5),
            follow_redirects=False,
            transport=transport,
        )
        self.token = ""
        self.token_until = 0
        self.lock = threading.Lock()

    def close(self) -> None:
        self.http.close()

    def _token(self, refresh=False) -> str:
        with self.lock:
            if not refresh and self.token_until > self.clock() + 60:
                return self.token
            path = self.settings.secret_file
            if os.name == "posix" and path.stat().st_mode & 0o077:
                raise PermissionError("client secret file must not be group/world accessible")
            secret = path.read_text().strip()
            response = self.http.post(
                self.settings.token_url,
                auth=httpx.BasicAuth(self.settings.client_id, secret),
                data={
                    "grant_type": "client_credentials",
                    "scope": (
                        "dsc/edge-server.work"
                        if self.settings.role == "archive"
                        else "dsc/ingest.write dsc/edge-server.work"
                    ),
                },
            )
            response.raise_for_status()
            value = response.json()
            self.token = value["access_token"]
            self.token_until = self.clock() + int(value["expires_in"])
            return self.token

    def request(self, method: str, path: str, body: dict) -> httpx.Response:
        url = f"{self.settings.api_url.rstrip('/')}{path}"
        for attempt in range(2):
            token = self._token(refresh=attempt == 1)
            response = self.http.request(
                method, url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            emit_event(
                "backend_response_received",
                level=logging.WARNING if response.status_code >= 400 else logging.DEBUG,
                method=method,
                status=response.status_code,
                backendTraceId=response.headers.get("X-Request-Id"),
            )
            if response.status_code != 401 or attempt == 1:
                return response
        raise RuntimeError("unreachable")

    def post(self, path: str, body: dict) -> dict | None:
        response = self.request("POST", path, body)
        response.raise_for_status()
        return response.json() if response.status_code != 204 else None

    def put_clip(self, url: str, headers: dict, stream, size: int) -> None:
        # Backend bearer tokens never accompany S3 requests; signed redirects are not followed.
        if not url.startswith("https://"):
            raise ValueError("HTTPS upload URL is required")
        response = self.http.put(
            url,
            headers={**headers, "Content-Length": str(size)},
            content=stream,
            timeout=self.settings.upload_timeout,
        )
        response.raise_for_status()


def response_code(response: httpx.Response) -> str:
    try:
        code = response.json().get("error", {}).get("code", "HTTP_ERROR")
        return (
            code
            if isinstance(code, str) and len(code) <= 64 and code.replace("_", "").isalnum()
            else "HTTP_ERROR"
        )
    except (ValueError, AttributeError):
        return "HTTP_ERROR"
