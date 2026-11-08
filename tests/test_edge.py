import json

import httpx
import pytest

from dsc.edge.client import EdgeClient
from dsc.edge.config import EdgeSettings
from dsc.edge.inbox import receive_inbox
from dsc.edge.paths import open_clip
from dsc.edge.store import EdgeStore
from dsc.edge.worker import run_job, send_pending
from dsc.shared.contracts import EventEnvelope, MediaEnvelope
from dsc.shared.observability import emit_event


@pytest.fixture
def edge(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("test-only-secret")
    secret.chmod(0o600)
    return EdgeSettings(
        environment="test",
        agent_id="agent-1",
        site_id="site-1",
        api_url="https://api.test",
        token_url="https://auth.test/oauth2/token",
        client_id="edge",
        secret_file=secret,
        nas_root=nas,
        state_dir=tmp_path / "state",
        inbox=tmp_path / "inbox",
    )


def test_atomic_inbox_and_restart_recovery(edge, event_body):
    edge.inbox.mkdir()
    envelope = {
        "kind": "event",
        "event": event_body,
        "localMedia": {"clipId": event_body["media"]["clipId"], "relativePath": "clip.mp4"},
    }
    partial = edge.inbox / "unfinished.tmp"
    partial.write_text("incomplete")
    source = edge.inbox / f"{event_body['eventId']}.event.json"
    source.write_text(json.dumps(envelope))
    store = EdgeStore(edge.state_dir / "outbox.sqlite")
    assert receive_inbox(edge, store) == 1
    assert partial.exists()
    assert "relativePath" not in store.rows()[0]["body"]
    assert store.next_item()["event_id"] == event_body["eventId"]
    restarted = EdgeStore(edge.state_dir / "outbox.sqlite")
    assert restarted.rows()[0]["state"] == "PENDING"
    source.write_text(json.dumps(envelope))
    receive_inbox(edge, restarted)
    assert len(restarted.rows()) == 1


def test_media_waits_for_event_ack(edge, event_body):
    store = EdgeStore(edge.state_dir / "outbox.sqlite")
    store.accept(EventEnvelope(kind="event", event=event_body))
    media = {**event_body["media"], "revision": 1}
    store.accept(MediaEnvelope(kind="media", eventId=event_body["eventId"], media=media))
    first = store.next_item()
    assert first["kind"] == "event"
    assert store.next_item() is None
    store.mark(first["id"], "ACKED")
    assert store.next_item()["kind"] == "media"


@pytest.mark.parametrize(
    "status,state",
    [
        (201, "ACKED"),
        (200, "ACKED"),
        (429, "PENDING"),
        (503, "PENDING"),
        (401, "PENDING"),
        (403, "QUARANTINED"),
        (409, "QUARANTINED"),
    ],
)
def test_retry_and_quarantine(edge, event_body, status, state, operation_logs):
    store = EdgeStore(edge.state_dir / "outbox.sqlite")
    store.accept(EventEnvelope(kind="event", event=event_body))

    class Client:
        def request(self, *_):
            return httpx.Response(
                status,
                json={
                    "eventId": event_body["eventId"],
                    "agentId": "agent-1",
                    "siteId": "site-1",
                    "receivedAt": "2026-11-07T12:00:00Z",
                    "duplicate": status == 200,
                },
                headers={"Retry-After": "2"},
            )

    send_pending(Client(), store, edge)
    assert store.rows()[0]["state"] == state
    transition = [entry for entry in operation_logs if entry["event"] == "outbox_state_changed"][-1]
    assert transition["state"] == state and transition["eventId"] == event_body["eventId"]
    assert transition["outboxId"] == store.rows()[0]["id"]


def test_false_ack_preserves_outbox(edge, event_body):
    store = EdgeStore(edge.state_dir / "outbox.sqlite")
    store.accept(EventEnvelope(kind="event", event=event_body))

    class Client:
        def request(self, *_):
            return httpx.Response(200, json={})

    send_pending(Client(), store, edge)
    assert store.rows()[0]["state"] == "PENDING"


def test_path_traversal_and_symlink_rejected(edge, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"clip")
    (edge.nas_root / "link.mp4").symlink_to(outside)
    with pytest.raises(ValueError), open_clip(edge.nas_root, "../outside.mp4"):
        pass
    with pytest.raises(OSError), open_clip(edge.nas_root, "link.mp4"):
        pass
    with open_clip(tmp_path, "outside.mp4") as stream:
        assert stream.read() == b"clip"


def test_token_refresh_once_and_no_backend_token_on_s3(edge, operation_logs):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "auth.test":
            return httpx.Response(
                200, json={"access_token": f"token-{len(calls)}", "expires_in": 3600}
            )
        if request.url.host == "s3.test":
            return httpx.Response(200)
        return httpx.Response(401)

    client = EdgeClient(edge, transport=httpx.MockTransport(handler))
    assert client.request("POST", "/agent/heartbeat", {}).status_code == 401
    assert len(calls) == 4
    import io

    client.put_clip("https://s3.test/clip?signature=private-signature", {}, io.BytesIO(b"clip"), 4)
    assert "Authorization" not in calls[-1].headers
    client.close()
    text = json.dumps(operation_logs)
    assert all(
        value not in text
        for value in ("token-1", "token-3", "test-only-secret", "private-signature", "https://")
    )


def test_inbox_invalid_file_quarantine(edge, event_body):
    edge.inbox.mkdir()
    source = edge.inbox / f"{event_body['eventId']}.event.json"
    source.write_text("not json")
    store = EdgeStore(edge.state_dir / "outbox.sqlite")
    assert receive_inbox(edge, store) == 0
    assert (edge.inbox / "rejected" / source.name).exists()
    assert store.rows() == []


def test_clip_worker_uploads_before_completion(edge, event_body):
    store = EdgeStore(edge.state_dir / "outbox.sqlite")
    (edge.nas_root / "clip.mp4").write_bytes(b"clip")
    store.accept(
        EventEnvelope(
            kind="event",
            event=event_body,
            localMedia={"clipId": event_body["media"]["clipId"], "relativePath": "clip.mp4"},
        )
    )
    calls = []

    class Client:
        def post(self, path, body):
            calls.append(path)
            return {"uploadUrl": "https://s3.test/clip", "headers": {}}

        def put_clip(self, url, headers, stream, size):
            assert stream.read() == b"clip" and size == 4
            calls.append("uploaded")

    run_job(
        Client(),
        store,
        edge,
        {
            "requestId": "job",
            "leaseToken": "x" * 32,
            "clipId": event_body["media"]["clipId"],
            "eventId": event_body["eventId"],
        },
    )
    assert calls == ["/agent/jobs/job/upload-url", "uploaded", "/agent/jobs/job/complete"]


def test_lease_thread_inherits_job_context(edge, event_body, operation_logs):
    import threading

    renewed = threading.Event()
    store = EdgeStore(edge.state_dir / "outbox.sqlite")
    (edge.nas_root / "private-clip.mp4").write_bytes(b"clip")
    store.accept(
        EventEnvelope(
            kind="event",
            event=event_body,
            localMedia={
                "clipId": event_body["media"]["clipId"],
                "relativePath": "private-clip.mp4",
            },
        )
    )

    class Client:
        def post(self, path, body):
            if path.endswith("/lease"):
                emit_event("test_lease_renewed")
                renewed.set()
            return {"uploadUrl": "https://s3.test/?signature=private", "headers": {}}

        def put_clip(self, *args):
            assert renewed.wait(3), "renewal thread never ran"

    operation_logs.clear()
    run_job(
        Client(),
        store,
        edge.model_copy(update={"lease_renew_seconds": 1}),
        {
            "requestId": "job-1",
            "eventId": event_body["eventId"],
            "clipId": event_body["media"]["clipId"],
            "leaseToken": "private-lease-token",
        },
    )
    renewal = [entry for entry in operation_logs if entry["event"] == "test_lease_renewed"][0]
    assert renewal["requestId"] == "job-1" and renewal["eventId"] == event_body["eventId"]
    assert len({entry["traceId"] for entry in operation_logs}) == 1
    assert "private-clip.mp4" not in json.dumps(operation_logs)
    assert "private-lease-token" not in json.dumps(operation_logs)


def test_missing_clip_is_logged_as_business_failure(edge, event_body, operation_logs):
    store = EdgeStore(edge.state_dir / "outbox.sqlite")
    reports = []

    class Client:
        def post(self, path, body):
            reports.append(body["failureCode"])

    run_job(
        Client(),
        store,
        edge,
        {
            "requestId": "job-1",
            "eventId": event_body["eventId"],
            "clipId": event_body["media"]["clipId"],
            "leaseToken": "private-lease-token",
        },
    )
    failure = [entry for entry in operation_logs if entry["event"] == "clip_job_failed"][0]
    assert failure["errorCode"] == "SOURCE_NOT_FOUND" and failure["requestId"] == "job-1"
    assert reports == ["SOURCE_NOT_FOUND"]
