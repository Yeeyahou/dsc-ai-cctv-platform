"""Test-only subprocess using the product inbox, durable outbox and HTTP client."""

import sys
from pathlib import Path

from dsc.edge.client import EdgeClient
from dsc.edge.config import EdgeSettings
from dsc.edge.inbox import receive_inbox
from dsc.edge.store import EdgeStore
from dsc.edge.worker import send_pending

settings = EdgeSettings.model_validate_json(Path(sys.argv[1]).read_text())
store = EdgeStore(settings.state_dir / "outbox.sqlite")
receive_inbox(settings, store)
client = EdgeClient(settings)
try:
    for _ in range(100):
        if not send_pending(client, store, settings):
            break
finally:
    client.close()
