import json
from datetime import UTC, datetime

import pytest

from dsc.devtools.producer import prepare_run, publish, replay
from dsc.edge.config import EdgeSettings
from dsc.edge.inbox import receive_inbox
from dsc.edge.store import EdgeStore
from dsc.shared.contracts import EventEnvelope


@pytest.fixture
def edge(tmp_path):
    root = tmp_path / "producer-input"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("test-only")
    secret.chmod(0o600)
    return EdgeSettings(
        environment="test",
        role="field",
        server_id="edge-server-1",
        site_id="site-1",
        api_url="https://api.test",
        token_url="https://auth.test/token",
        client_id="edge",
        secret_file=secret,
        nas_root=root,
        state_dir=tmp_path / "state",
        inbox=tmp_path / "inbox",
        archive_allowed_hosts=["office.test"],
    )


def plan(tmp_path, channel="cctv", count=20):
    video = tmp_path / "unit-container.mp4"
    # Only the producer's file contract is under test; playback is a separate FFmpeg rehearsal.
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"unit-test-container")
    directory = tmp_path / channel
    value = prepare_run(
        directory,
        channel=channel,
        site_id="site-1",
        asset_id="camera-1",
        count=count,
        seed=2,
        started_at=datetime(2026, 11, 15, tzinfo=UTC),
        video=video,
    )
    return directory, value


@pytest.mark.parametrize("channel,prefix", [("cctv", "DSC-1"), ("robot", "DSC-2")])
def test_producer_replay_into_real_inbox_is_idempotent(tmp_path, edge, channel, prefix):
    directory, value = plan(tmp_path, channel)
    settings = edge
    store = EdgeStore(settings.state_dir / "outbox.sqlite")
    replay(directory, settings.inbox, settings.nas_root)
    assert receive_inbox(settings, store) == 40
    first = [dict(row) for row in store.rows()]
    replay(directory, settings.inbox, settings.nas_root)
    receive_inbox(settings, store)
    assert [dict(row) for row in store.rows()] == first
    assert len(first) == 40
    assert all(entry["eventCode"].startswith(prefix) for entry in value["entries"])
    assert len({entry["eventId"] for entry in value["entries"]}) == 20
    pending = None
    for entry in value["entries"]:
        envelope = EventEnvelope.model_validate_json(
            (directory / "envelopes" / entry["envelopes"][0]).read_text()
        )
        event = envelope.event
        if pending:
            assert event.event_code == pending.event_code[:-1] + "B"
            assert event.incident_id == pending.incident_id
            pending = None
        elif event.event_code in {"DSC-102A", "DSC-104A", "DSC-106A", "DSC-202A"}:
            pending = event
    assert pending is None


@pytest.mark.parametrize("target", ["clip", "envelope"])
def test_replay_refuses_changed_saved_bytes_before_publishing(tmp_path, target):
    directory, value = plan(tmp_path, count=1)
    entry = value["entries"][0]
    path = (
        directory / "clips" / entry["relativePath"]
        if target == "clip"
        else directory / "envelopes" / entry["envelopes"][0]
    )
    path.write_bytes(path.read_bytes() + b" ")
    inbox = tmp_path / "inbox"
    with pytest.raises(ValueError, match="changed"):
        replay(directory, inbox, tmp_path / "input")
    assert not inbox.exists()


def test_publish_conflict_never_overwrites_and_partial_plan_is_not_replayed(tmp_path):
    target = tmp_path / "inbox" / "identity.json"
    publish(target, b"original")
    with pytest.raises(ValueError, match="different bytes"):
        publish(target, b"changed")
    assert target.read_bytes() == b"original"
    assert not list(target.parent.glob("*.tmp"))
    with pytest.raises(FileNotFoundError):
        replay(tmp_path, tmp_path / "inbox", tmp_path / "input")


def test_existing_plan_requires_explicit_replay(tmp_path):
    directory, value = plan(tmp_path, count=1)
    with pytest.raises(ValueError, match="already exists"):
        prepare_run(
            directory,
            channel="robot",
            site_id="site-1",
            asset_id="robot-1",
            count=1,
            seed=1,
            started_at=datetime.now(UTC),
        )
    assert json.loads((directory / "plan.json").read_text()) == value
