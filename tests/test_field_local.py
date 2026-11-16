import json
import threading

import pytest

from dsc.devtools.field_local import local_settings, receive, snapshot
from dsc.devtools.producer import publish
from dsc.edge.store import EdgeStore


def test_local_receiver_persists_duplicate_inputs_and_recovers_pending(tmp_path, event_body):
    settings = local_settings(tmp_path)
    body = {**event_body, "siteId": settings.site_id}
    name = f"{body['eventId']}.event.json"
    content = json.dumps({"kind": "event", "event": body}).encode()
    partial = settings.inbox / "unfinished.json.tmp"
    publish(partial, b"incomplete")
    publish(settings.inbox / name, content)
    receive(tmp_path, threading.Event(), once=True)
    first = snapshot(tmp_path)
    assert len(first["outbox"]) == 1
    assert first["outbox"][0]["state"] == "PENDING"
    assert partial.read_bytes() == b"incomplete"
    store = EdgeStore(settings.state_dir / "outbox.sqlite")
    assert store.next_item() is not None
    assert snapshot(tmp_path)["outbox"][0]["state"] == "SENDING"
    assert store.rows()[0]["state"] == "SENDING"
    publish(settings.inbox / name, content)
    receive(tmp_path, threading.Event(), once=True)
    assert snapshot(tmp_path) == first


def test_inspection_does_not_create_a_database(tmp_path):
    assert snapshot(tmp_path) == {"ready": False, "outbox": [], "copies": []}
    assert not list(tmp_path.iterdir())


def test_local_receiver_does_not_allow_two_consumers(tmp_path):
    import fcntl

    settings = local_settings(tmp_path)
    settings.state_dir.mkdir(parents=True)
    with (settings.state_dir / "edge-server.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            receive(tmp_path, threading.Event(), once=True)
