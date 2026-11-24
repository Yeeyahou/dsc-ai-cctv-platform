"""Check the published edge API inventory and example contracts without external calls."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from dsc.backend.live import LiveAuthorization, LiveCheck
from dsc.shared import contracts

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = [
    ("EC01", "POST", "/ingest", "EventInput"),
    ("EC02", "PUT", "/ingest/events/{event_id}/media", "MediaUpdate"),
    ("EC03", "POST", "/edge-server/heartbeat", "Heartbeat"),
    ("EC04", "POST", "/edge-server/replicas", "ReplicaBody"),
    ("EC05", "POST", "/edge-server/archive/prepare", "ReplicaBody"),
    ("EC06", "POST", "/edge-server/archive/{transfer_id}/authorize", "TransferToken"),
    ("EC07", "POST", "/edge-server/archive/{transfer_id}/complete", "TransferReceipt"),
    ("EC08", "POST", "/edge-server/replicas/retire", "ReplicaBody"),
    ("EC09", "POST", "/edge-server/jobs/claim", "EmptyBody"),
    ("EC10", "POST", "/edge-server/jobs/{request_id}/lease", "LeaseBody"),
    ("EC11", "POST", "/edge-server/jobs/{request_id}/upload-url", "UploadBody"),
    ("EC12", "POST", "/edge-server/jobs/{request_id}/complete", "LeaseBody"),
    ("EC13", "POST", "/edge-server/jobs/{request_id}/fail", "FailureBody"),
]
LIVE_OPERATIONS = [
    ("LA01", "GET", "/live/cameras", None),
    ("LA02", "POST", "/live/cameras/{asset_id}/sessions", contracts.EmptyBody),
    ("LA03", "POST", "/live/sessions/{session_id}/stop", contracts.EmptyBody),
    ("LA04", "POST", "/edge-server/live/authorize", LiveAuthorization),
    ("LA05", "POST", "/edge-server/live/check", LiveCheck),
]


def main() -> None:
    text = (ROOT / "docs/edge-cloud-api-v1.2.md").read_text()
    api = json.loads((ROOT / "docs/openapi.json").read_text())
    actual = {
        (method.upper(), path)
        for path, methods in api["paths"].items()
        if path.startswith(("/ingest", "/edge-server"))
        for method in methods
        if method in {"get", "post", "put", "patch", "delete", "head", "options"}
    }
    expected = {(method, path) for _, method, path, _ in OPERATIONS}
    expected.update(
        (method, path) for _, method, path, _ in LIVE_OPERATIONS if path.startswith("/edge-server")
    )
    if actual != expected:
        raise ValueError("AWS edge API inventory changed; update the reference")
    sections = re.split(r"(?=^### 5 \d+ EC\d+ )", text, flags=re.MULTILINE)[1:]
    if len(sections) != len(OPERATIONS):
        raise ValueError("API detail section is missing or duplicated")
    for (identity, method, path, model), section in zip(OPERATIONS, sections, strict=True):
        if not section.startswith(f"### 5 {int(identity[2:])} {identity} "):
            raise ValueError(f"API section order mismatch: {identity}")
        operation = api["paths"][path][method.lower()]
        reference = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        if reference.rsplit("/", 1)[-1] != model or f"{method} {path}" not in section:
            raise ValueError(f"API request contract mismatch: {identity}")
        request_fields = section.split("\n요청 필드\n", 1)[1].split("\n요청 본문 예제\n", 1)[0]
        response_fields = re.search(
            r"\n응답 필드\n(.*?)\n(?:200|201) 응답 예제\n", section, re.DOTALL
        )
        if response_fields is None:
            raise ValueError(f"Response field table missing: {identity}")
        for kind in ("request", "response"):
            example = json.loads(
                (ROOT / f"examples/edge-cloud-api/{identity.lower()}-{kind}.json").read_text()
            )
            if json.dumps(example, ensure_ascii=False, indent=2) not in section:
                raise ValueError(f"Example file differs from reference: {identity} {kind}")
            if kind == "request":
                getattr(contracts, model).model_validate(example)
                if set(example) - set(api["components"]["schemas"][model]["properties"]):
                    raise ValueError(f"Request fields must use wire names: {identity}")
            else:
                for field, value in example.items():
                    if f"| {field} |" not in response_fields[1]:
                        raise ValueError(f"Missing response field: {identity} {field}")
                    if isinstance(value, dict):
                        for nested in value:
                            if f"| {field}.{nested} |" not in response_fields[1]:
                                raise ValueError(f"Missing nested response field: {identity}")
        for name in api["components"]["schemas"][model]["properties"]:
            if f"| {name} |" not in request_fields:
                raise ValueError(f"Missing request field: {identity} {name}")
    constants = set()
    for path in (ROOT / "src/dsc/backend").glob("*.py"):
        constants.update(
            node.value
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    for line in text.splitlines():
        match = re.match(r"\| [45]\d\d \| ([A-Z_ /]+) \|", line)
        if match:
            for code in match[1].split(" / "):
                if code not in constants:
                    raise ValueError(f"Undeclared backend error code: {code}")
    for code in contracts.EVENT_CODES:
        if code not in text:
            raise ValueError(f"Event code missing from reference: {code}")
    if "POST /archive/transfers/{transfer_id}/verify" not in text:
        raise ValueError("Related archive verify interface is missing")
    live_text = (ROOT / "docs/live-api-v1.3.md").read_text()
    for identity, method, path, model in LIVE_OPERATIONS:
        if f"### {identity} {method} {path}" not in live_text:
            raise ValueError(f"Live API documentation missing: {identity}")
        operation = api["paths"][path][method.lower()]
        if model:
            schema = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            if schema.rsplit("/", 1)[-1] != model.__name__:
                raise ValueError(f"Live request model mismatch: {identity}")
            sample = json.loads(
                (ROOT / f"examples/live-api/{identity.lower()}-request.json").read_text()
            )
            model.model_validate(sample)
            if json.dumps(sample, ensure_ascii=False, indent=2) not in live_text:
                raise ValueError(f"Live request example mismatch: {identity}")
        response = json.loads(
            (ROOT / f"examples/live-api/{identity.lower()}-response.json").read_text()
        )
        if json.dumps(response, ensure_ascii=False, indent=2) not in live_text:
            raise ValueError(f"Live response example mismatch: {identity}")
    if {
        (method.upper(), path)
        for path, operations in api["paths"].items()
        for method in operations
        if path.startswith("/live") and method in {"get", "post", "put", "patch", "delete"}
    } != {(method, path) for _, method, path, _ in LIVE_OPERATIONS if path.startswith("/live")}:
        raise ValueError("Live viewer API inventory changed")
    print("Legacy edge APIs: 13; live APIs: 5; request models and examples verified")


if __name__ == "__main__":
    main()
