import base64
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from dsc.backend.admin import Registration, register
from dsc.backend.api import create_app
from dsc.backend.auth import authenticate
from dsc.backend.config import Settings
from dsc.backend.db import Base, database
from dsc.backend.errors import Problem
from dsc.backend.service import Platform
from dsc.shared.observability import JsonFormatter


class Clock:
    def __init__(self):
        self.value = int(datetime(2026, 11, 7, 12, tzinfo=UTC).timestamp() * 1000)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds * 1000


class Storage:
    def __init__(self):
        self.objects = {}
        self.urls = []

    def upload_url(self, key, size, checksum, seconds):
        self.urls.append(("PUT", key, seconds))
        return f"https://storage.test/{key}", {
            "Content-Type": "video/mp4",
            "x-amz-checksum-sha256": checksum,
        }

    def verify(self, key, size, checksum):
        value = self.objects.get(key, b"")
        if (
            len(value) != size
            or base64.b64encode(hashlib.sha256(value).digest()).decode() != checksum
        ):
            raise Problem(409, "UPLOAD_VERIFICATION_FAILED", "mismatch")

    def download_url(self, key, seconds):
        self.urls.append(("GET", key, seconds))
        return f"https://storage.test/{key}?expires={seconds}"

    def delete(self, key):
        self.objects.pop(key, None)


class Queue:
    def __init__(self, clock):
        self.clock, self.messages = clock, []
        self.fail_send = False

    def send(self, queue, job_id, version):
        if self.fail_send:
            raise OSError("queue is down")
        self.messages.append(
            {
                "Queue": queue,
                "Body": json.dumps({"requestId": job_id, "dispatchVersion": version}),
                "ReceiptHandle": str(uuid4()),
                "until": 0,
            }
        )

    def receive(self, queue):
        rows = [m for m in self.messages if m["Queue"] == queue and m["until"] <= self.clock()][:1]
        for m in rows:
            m["until"] = self.clock() + 300000
        return rows

    def extend(self, queue, receipt, seconds):
        for m in self.messages:
            if m["Queue"] == queue and m["ReceiptHandle"] == receipt:
                m["until"] = self.clock() + seconds * 1000

    def delete(self, queue, receipt):
        self.messages = [
            m for m in self.messages if not (m["Queue"] == queue and m["ReceiptHandle"] == receipt)
        ]


def asset(asset_id="camera-1", site_id="site-1"):
    return {
        "assetId": asset_id,
        "siteId": site_id,
        "assetType": "camera",
        "displayName": asset_id,
        "aas": {
            "aasId": f"urn:dsc:aas:{asset_id}",
            "globalAssetId": f"urn:dsc:asset:{asset_id}",
            "idShort": asset_id.replace("-", "_"),
            "submodels": {
                kind: {
                    "id": f"urn:dsc:submodel:{asset_id}:{kind}",
                    "semanticId": f"urn:dsc:semantic:{kind}:1",
                }
                for kind in ("Nameplate", "TechnicalData", "TimeSeries")
            },
            "values": {
                "Nameplate": {"manufacturerName": "Example Manufacturer"},
                "TechnicalData": {"assetType": "camera"},
            },
        },
    }


@pytest.fixture
def system(tmp_path):
    clock = Clock()
    url = os.environ.get("DSC_TEST_POSTGRES_URL", f"sqlite:///{tmp_path}/backend.sqlite")
    engine, sessions = database(url)
    if "DSC_TEST_POSTGRES_URL" in os.environ:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    scopes = ["dsc/aas.read", "dsc/timeseries.read", "dsc/ingest.write", "dsc/agent.work"]
    register(
        sessions,
        Registration.model_validate(
            {
                "assets": [asset(), asset("rack-1"), asset("other-camera", "other-site")],
                "principals": [
                    {"principalId": "client:edge", "scopes": scopes[2:]},
                    {"principalId": "client:edge-other", "scopes": scopes[2:]},
                    {
                        "principalId": "user:staff",
                        "scopes": scopes[:2],
                        "sites": ["site-1"],
                        "submodels": ["Nameplate", "TechnicalData", "TimeSeries"],
                        "video": True,
                    },
                    {"principalId": "client:partner", "scopes": scopes[:1], "assets": ["camera-1"]},
                    {
                        "principalId": "user:partial",
                        "scopes": scopes[:2],
                        "assets": ["camera-1"],
                        "video": True,
                    },
                ],
                "agents": [
                    {
                        "agentId": "agent-1",
                        "principalId": "client:edge",
                        "siteId": "site-1",
                        "assets": ["camera-1", "rack-1"],
                        "queueUrl": "https://sqs.test/agent-1",
                    },
                    {
                        "agentId": "agent-other",
                        "principalId": "client:edge-other",
                        "siteId": "other-site",
                        "assets": ["other-camera"],
                        "queueUrl": "https://sqs.test/agent-other",
                    },
                ],
            }
        ),
    )
    settings = Settings(environment="test", cursor_secret=SecretStr("a" * 32))
    storage, queue = Storage(), Queue(clock)
    platform = Platform(sessions, settings, storage, queue, clock)

    def claims(token):
        if token not in {"edge", "edge-other", "staff", "partner", "partial"}:
            raise Problem(401, "INVALID_TOKEN", "invalid")
        result = {"sub": token, "client_id": token, "scope": " ".join(scopes)}
        if token in {"staff", "partial"}:
            result["username"] = token
        return result

    def access(token):
        with sessions() as session:
            return authenticate(session, claims(token))

    client = TestClient(create_app(platform, claims), raise_server_exceptions=False)
    yield SimpleNamespace(
        engine=engine,
        sessions=sessions,
        platform=platform,
        storage=storage,
        queue=queue,
        clock=clock,
        client=client,
        access=access,
        tmp_path=tmp_path,
    )
    client.close()
    engine.dispose()


@pytest.fixture
def event_body():
    return {
        "schemaVersion": "1.0",
        "eventId": str(uuid4()),
        "eventCode": "DSC-102A",
        "siteId": "site-1",
        "occurredAt": "2026-11-07T10:00:00.000Z",
        "incidentId": str(uuid4()),
        "assets": [
            {"assetId": "camera-1", "role": "detector"},
            {"assetId": "rack-1", "role": "subject"},
        ],
        "media": {
            "clipId": str(uuid4()),
            "status": "AVAILABLE",
            "sizeBytes": 4,
            "contentType": "video/mp4",
            "startedAt": "2026-11-07T09:59:55.000Z",
            "endedAt": "2026-11-07T10:00:05.000Z",
        },
    }


def headers(token="staff"):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def operation_logs():
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(json.loads(self.format(record)))

    logger = logging.getLogger("dsc.operations")
    previous = logger.level
    capture = Capture()
    capture.setFormatter(JsonFormatter())
    logger.setLevel(logging.DEBUG)
    logger.addHandler(capture)
    try:
        yield records
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous)
