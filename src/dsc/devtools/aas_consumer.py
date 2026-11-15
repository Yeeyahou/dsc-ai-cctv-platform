"""Read-only external AAS consumer; never follows descriptor endpoints with credentials."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from dsc.backend.aas import encoded, validate_model
from dsc.devtools.producer import publish
from dsc.shared.observability import configure_logging, log_methods


class ConsumerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    aas_id: str = Field(min_length=1)
    global_asset_id: str = Field(min_length=1)
    submodel_ids: list[str] = Field(min_length=1)
    forbidden_aas_id: str = Field(min_length=1)
    forbidden_submodel_id: str = Field(min_length=1)
    access_token_file: Path | None = None
    token_url: str | None = None
    client_id: str | None = None
    client_secret_file: Path | None = None
    scopes: str = "dsc/aas.read"
    allow_local_http: bool = False
    max_pages: int = Field(default=100, ge=1, le=1000)
    timeout_seconds: float = Field(default=15, gt=0, le=120, allow_inf_nan=False)

    @model_validator(mode="after")
    def configuration(self) -> ConsumerSettings:
        for value in (self.base_url, self.token_url):
            if value is None:
                continue
            url = urlsplit(value)
            if (
                not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or (
                    url.scheme != "https"
                    and not (
                        self.allow_local_http
                        and url.scheme == "http"
                        and url.hostname == "127.0.0.1"
                    )
                )
            ):
                raise ValueError("HTTPS origin is required; loopback HTTP is opt-in for tests")
        credentials = (self.token_url, self.client_id, self.client_secret_file)
        if self.access_token_file:
            if any(credentials):
                raise ValueError("choose an access token file or client credentials")
        elif not all(credentials):
            raise ValueError("client credentials or an access token file are required")
        if self.aas_id == self.forbidden_aas_id:
            raise ValueError("allowed and forbidden AAS must differ")
        if self.forbidden_submodel_id in self.submodel_ids:
            raise ValueError("allowed and forbidden submodels must differ")
        return self


def read_secret(path: Path) -> str:
    if path.is_symlink() or (os.name == "posix" and path.stat().st_mode & 0o077):
        raise PermissionError("credential file must be private and not a symlink")
    value = path.read_text().strip()
    if not value or len(value) > 32768 or any(c.isspace() for c in value):
        raise ValueError("invalid credential file")
    return value


@log_methods()
class Consumer:
    def __init__(self, settings: ConsumerSettings, transport=None):
        self.settings = settings
        self.http = httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds, connect=5),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )
        self.token = ""
        self.evidence: list[dict] = []

    def close(self) -> None:
        self.http.close()

    def authenticate(self) -> None:
        cfg = self.settings
        if cfg.access_token_file:
            self.token = read_secret(cfg.access_token_file)
            return
        with self.http.stream(
            "POST",
            cfg.token_url,
            auth=httpx.BasicAuth(cfg.client_id, read_secret(cfg.client_secret_file)),
            data={"grant_type": "client_credentials", "scope": cfg.scopes},
        ) as response:
            if response.status_code != 200:
                raise ValueError(f"authentication HTTP {response.status_code}")
            body = self.body(response)
        self.token = body["access_token"]
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("invalid access token response")

    @staticmethod
    def body(response: httpx.Response):
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 2 * 1024 * 1024:
                raise ValueError("response exceeds 2 MiB")
            chunks.append(chunk)
        return json.loads(b"".join(chunks))

    def get(self, path: str, *, params=None, expected=200):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("relative API path is required")
        with self.http.stream(
            "GET",
            self.settings.base_url.rstrip("/") + path,
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
        ) as response:
            self.evidence.append({"route": path.split("/")[1], "status": response.status_code})
            if response.status_code != expected:
                raise ValueError(f"unexpected HTTP {response.status_code}; expected {expected}")
            if expected != 200:
                return None
            return self.body(response)

    def pages(self, path: str, params=None) -> list:
        params = {**(params or {}), "limit": 2}
        values, seen = [], set()
        for _ in range(self.settings.max_pages):
            body = self.get(path, params=params)
            if not isinstance(body.get("result"), list):
                raise ValueError("invalid page result")
            values.extend(body["result"])
            cursor = body.get("paging_metadata", {}).get("cursor")
            if not cursor:
                return values
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("repeated or invalid cursor")
            seen.add(cursor)
            params["cursor"] = cursor
        raise ValueError("pagination limit exceeded")

    def verify(self) -> dict:
        cfg = self.settings
        self.authenticate()
        description = self.get("/description")
        if description != {"profiles": []}:
            raise ValueError("unexpected supported service profiles")
        lookup = encoded(json.dumps({"name": "globalAssetId", "value": cfg.global_asset_id}))
        discovered = self.pages("/lookup/shells", {"assetIds": lookup})
        if cfg.aas_id not in discovered or cfg.forbidden_aas_id in discovered:
            raise ValueError("Discovery identity or permission mismatch")
        descriptors = self.pages("/shell-descriptors")
        identifiers = {item["id"] for item in descriptors}
        if cfg.aas_id not in identifiers or cfg.forbidden_aas_id in identifiers:
            raise ValueError("Registry identity or permission mismatch")
        aas_path = encoded(cfg.aas_id)
        descriptor = self.get(f"/shell-descriptors/{aas_path}")
        shell = self.get(f"/shells/{aas_path}")
        validate_model(shell, "AssetAdministrationShell")
        if descriptor["id"] != cfg.aas_id or shell["id"] != cfg.aas_id:
            raise ValueError("Shell identity mismatch")
        if shell["assetInformation"]["globalAssetId"] != cfg.global_asset_id:
            raise ValueError("globalAssetId mismatch")
        refs = self.pages(f"/shells/{aas_path}/submodel-refs")
        expected = set(cfg.submodel_ids)
        for references in (refs, shell.get("submodels", [])):
            actual = {ref["keys"][0]["value"] for ref in references}
            if actual != expected:
                raise ValueError("allowed submodel references mismatch")
        for identity in cfg.submodel_ids:
            path = encoded(identity)
            sub_descriptor = self.get(f"/submodel-descriptors/{path}")
            model = self.get(f"/submodels/{path}")
            validate_model(model, "Submodel")
            values = self.get(f"/submodels/{path}/$value")
            if (
                model["id"] != identity
                or sub_descriptor["id"] != identity
                or not isinstance(values, dict)
            ):
                raise ValueError("Submodel identity or value serialization mismatch")
        forbidden = encoded(cfg.forbidden_aas_id)
        for route in (
            f"/shells/{forbidden}",
            f"/shell-descriptors/{forbidden}",
            f"/shells/{forbidden}/submodel-refs",
        ):
            self.get(route, expected=404)
        forbidden = encoded(cfg.forbidden_submodel_id)
        for route in (
            f"/submodels/{forbidden}",
            f"/submodels/{forbidden}/$value",
            f"/submodel-descriptors/{forbidden}",
        ):
            self.get(route, expected=404)
        return {
            "status": "PASS",
            "boundary": "configured-http-endpoint",
            "recordedAt": datetime.now(UTC).isoformat(),
            "requests": self.evidence,
            "submodelsChecked": len(cfg.submodel_ids),
        }


def main() -> int:
    configure_logging("aas-consumer")
    parser = argparse.ArgumentParser(description="Read-only external AAS acceptance consumer")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    consumer = None
    try:
        cfg = ConsumerSettings.model_validate_json(args.config.read_text())
        consumer = Consumer(cfg)
        report = consumer.verify()
    except Exception as exc:
        # Never serialize exception text: it can contain a bearer token or response body.
        report = {
            "status": "FAIL",
            "errorType": type(exc).__name__,
            "requests": consumer.evidence if consumer else [],
        }
    finally:
        if consumer:
            consumer.close()
    publish(args.report, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    print(json.dumps({"status": report["status"], "requests": len(report["requests"])}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
