import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from dsc.devtools.aas_consumer import ConsumerSettings
from dsc.devtools.fault_proxy import FaultSettings, make_server
from dsc.shared.observability import contextual


@contextmanager
def serving(server):
    thread = threading.Thread(target=contextual(server.serve_forever), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def backend_server(system, *, committed=None, release=None, seen=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def forward(self):
            if self.path == "/oauth2/token":
                status, data = 200, b'{"access_token":"edge","expires_in":3600}'
            else:
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size)
                response = system.client.request(
                    self.command, self.path, content=body, headers=dict(self.headers)
                )
                status, data = response.status_code, response.content
                if seen is not None:
                    seen.append((self.command, self.path, body, status))
                if self.path == "/ingest" and committed and not committed.is_set():
                    committed.set()
                    assert release.wait(10)
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass

        do_GET = do_POST = do_PUT = forward

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


@pytest.mark.parametrize("mode", ["drop-before", "drop-after", "delay"])
def test_real_socket_fault_proxy_preserves_duplicate_event(system, event_body, mode):
    seen = []
    with serving(backend_server(system, seen=seen)) as upstream:
        settings = FaultSettings(
            upstream=upstream, mode=mode, allow_local_upstream=True, delay_seconds=0.1
        )
        with serving(make_server(settings)) as proxy, httpx.Client(timeout=5) as client:
            before = time.monotonic()
            if mode == "delay":
                result = client.post(
                    proxy + "/ingest", json=event_body, headers={"Authorization": "Bearer edge"}
                )
                assert result.status_code == 201
                assert time.monotonic() - before >= 0.1
            else:
                with pytest.raises(httpx.RemoteProtocolError):
                    client.post(
                        proxy + "/ingest", json=event_body, headers={"Authorization": "Bearer edge"}
                    )
                assert len(seen) == (1 if mode == "drop-after" else 0)
            result = client.post(
                proxy + "/ingest", json=event_body, headers={"Authorization": "Bearer edge"}
            )
            assert result.status_code == (201 if mode == "drop-before" else 200)
            assert result.json()["duplicate"] is (mode != "drop-before")
            result = client.post(
                proxy + "/ingest", json=event_body, headers={"Authorization": "Bearer edge"}
            )
            assert result.status_code == 200 and result.json()["duplicate"] is True


def test_sigkill_sender_recovers_after_backend_commit_and_lost_ack(system, event_body, tmp_path):
    committed, release, seen = threading.Event(), threading.Event(), []
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / f"{event_body['eventId']}.event.json").write_text(
        json.dumps(
            {
                "kind": "event",
                "event": event_body,
            }
        )
    )
    secret = tmp_path / "secret"
    secret.write_text("test-only")
    secret.chmod(0o600)
    root = Path(__file__).resolve().parents[1]
    helper = root / "tests/support/inbox_sender.py"
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    with serving(backend_server(system, committed=committed, release=release, seen=seen)) as url:
        config = tmp_path / "sender.json"
        config.write_text(
            json.dumps(
                {
                    "environment": "test",
                    "server_id": "edge-server-1",
                    "site_id": "site-1",
                    "api_url": url,
                    "token_url": url + "/oauth2/token",
                    "client_id": "edge",
                    "secret_file": str(secret),
                    "nas_root": str(tmp_path),
                    "state_dir": str(tmp_path / "state"),
                    "inbox": str(inbox),
                }
            )
        )
        process = subprocess.Popen(
            [sys.executable, str(helper), str(config)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert committed.wait(8), "sender never reached the real backend transaction"
            process.kill()
            assert process.wait(timeout=5) != 0
            with sqlite3.connect(tmp_path / "state/outbox.sqlite") as db:
                body, state = db.execute("SELECT body,state FROM outbox").fetchone()
                assert state == "SENDING"
            release.set()
            subprocess.run(
                [sys.executable, str(helper), str(config)],
                env=env,
                capture_output=True,
                timeout=15,
                check=True,
            )
            with sqlite3.connect(tmp_path / "state/outbox.sqlite") as db:
                rows = db.execute("SELECT body,state FROM outbox").fetchall()
            assert rows == [(body, "ACKED")]
            assert len(seen) == 2
            assert seen[0][2] == seen[1][2]
            assert [entry[3] for entry in seen] == [201, 200]
        finally:
            release.set()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_aas_consumer_cli_uses_real_socket_and_writes_safe_report(system, tmp_path):
    token = tmp_path / "token"
    token.write_text("partner")
    token.chmod(0o600)
    root = Path(__file__).resolve().parents[1]
    with serving(backend_server(system)) as url:
        cfg = ConsumerSettings(
            base_url=url,
            allow_local_http=True,
            access_token_file=token,
            aas_id="urn:dsc:aas:camera-1",
            global_asset_id="urn:dsc:asset:camera-1",
            submodel_ids=[
                f"urn:dsc:submodel:camera-1:{name}" for name in ("Nameplate", "TechnicalData")
            ],
            forbidden_aas_id="urn:dsc:aas:other-camera",
            forbidden_submodel_id="urn:dsc:submodel:camera-1:TimeSeries",
        )
        config = tmp_path / "consumer.json"
        config.write_text(cfg.model_dump_json())
        report = tmp_path / "report.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "dsc.devtools.aas_consumer",
                "--config",
                str(config),
                "--report",
                str(report),
            ],
            env={**os.environ, "PYTHONPATH": str(root / "src")},
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
    assert json.loads(report.read_text())["status"] == "PASS"
    assert "partner" not in result.stdout + result.stderr + report.read_text()
