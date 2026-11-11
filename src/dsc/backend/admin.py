import argparse
import json
import re
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from sqlalchemy import select

from dsc.backend.aas import PROPERTIES
from dsc.backend.aws import load_secrets
from dsc.backend.config import Settings
from dsc.backend.db import Asset, Audit, EdgeServer, Principal, database
from dsc.shared.contracts import Contract, Identifier, now_ms
from dsc.shared.observability import configure_logging, logged

SCOPES = {"dsc/aas.read", "dsc/timeseries.read", "dsc/ingest.write", "dsc/edge-server.work"}


class AssetRegistration(Contract):
    asset_id: Identifier
    site_id: Identifier
    asset_type: str
    display_name: str
    aas: dict

    @field_validator("aas")
    @classmethod
    def metadata(cls, value: dict) -> dict:
        for key in ("aasId", "globalAssetId"):
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value.get(key, "")):
                raise ValueError("AAS identifiers must be URIs")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", value.get("idShort", "")):
            raise ValueError("invalid AAS idShort")
        for kind, item in value.get("submodels", {}).items():
            if kind not in PROPERTIES or not all(item.get(k) for k in ("id", "semanticId")):
                raise ValueError("invalid submodel registration")
            for key in value.get("values", {}).get(kind, {}):
                if key not in PROPERTIES[kind]:
                    raise ValueError("unmapped AAS property")
        return value


class PrincipalRegistration(Contract):
    principal_id: str
    active: bool = True
    scopes: list[str]
    sites: list[Identifier] = []
    assets: list[Identifier] = []
    submodels: list[str] = ["Nameplate", "TechnicalData"]
    video: bool = False

    @field_validator("principal_id")
    @classmethod
    def principal_key(cls, value: str) -> str:
        if not value.startswith(("user:", "client:")) or len(value) > 256:
            raise ValueError("principalId requires user:sub or client:client_id")
        return value

    @field_validator("scopes")
    @classmethod
    def allowed_scopes(cls, value: list[str]) -> list[str]:
        if not set(value).issubset(SCOPES):
            raise ValueError("unknown scope")
        return value

    @field_validator("submodels")
    @classmethod
    def allowed_submodels(cls, value: list[str]) -> list[str]:
        if not set(value).issubset(PROPERTIES):
            raise ValueError("unknown submodel")
        return value


class EdgeServerRegistration(Contract):
    edge_server_id: Identifier
    principal_id: str
    site_id: Identifier
    assets: list[Identifier] = Field(min_length=1)
    queue_url: str
    role: Literal["legacy", "field", "archive"] = "legacy"
    archive_url: str | None = None
    archive_server_id: Identifier | None = None

    @field_validator("archive_url")
    @classmethod
    def secure_receiver(cls, value: str | None) -> str | None:
        if value is not None:
            url = urlsplit(value)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "archiveUrl requires a registered HTTPS endpoint without credentials"
                )
            return value.rstrip("/")
        return value


class Registration(Contract):
    assets: list[AssetRegistration] = []
    principals: list[PrincipalRegistration] = []
    edge_servers: list[EdgeServerRegistration] = []


@logged()
def register(sessions, data: Registration) -> None:
    with sessions.begin() as session:
        for entry in data.assets:
            prior = session.get(Asset, entry.asset_id)
            if prior and prior.site_id != entry.site_id:
                raise ValueError(
                    "asset relocation requires an explicit migration of historical access"
                )
            session.merge(
                Asset(
                    id=entry.asset_id,
                    site_id=entry.site_id,
                    asset_type=entry.asset_type,
                    display_name=entry.display_name,
                    aas=entry.aas,
                )
            )
        session.flush()
        ids = []
        for asset in session.scalars(select(Asset)):
            ids.append(asset.aas["aasId"])
            ids.extend(x["id"] for x in asset.aas.get("submodels", {}).values())
        if len(ids) != len(set(ids)):
            raise ValueError("AAS and submodel identifiers must be unique")
        for entry in data.principals:
            for asset_id in entry.assets:
                if session.get(Asset, asset_id) is None:
                    raise ValueError("principal references an unregistered asset")
            session.merge(
                Principal(
                    id=entry.principal_id,
                    active=entry.active,
                    scopes=entry.scopes,
                    sites=entry.sites,
                    assets=entry.assets,
                    submodels=entry.submodels,
                    video=entry.video,
                )
            )
        session.flush()
        for entry in data.edge_servers:
            principal = session.get(Principal, entry.principal_id)
            if (
                principal is None
                or not entry.principal_id.startswith("client:")
                or "dsc/edge-server.work" not in principal.scopes
                or (entry.role != "archive" and "dsc/ingest.write" not in principal.scopes)
            ):
                raise ValueError(
                    "edge server requires a registered M2M principal with ingest/work scopes"
                )
            if not entry.queue_url.startswith("https://sqs."):
                raise ValueError("an AWS per-edge-server SQS queue URL is required")
            for asset_id in entry.assets:
                asset = session.get(Asset, asset_id)
                if asset is None or asset.site_id != entry.site_id:
                    raise ValueError("edge server asset belongs to another site")
            if entry.role == "archive" and (not entry.archive_url or entry.archive_server_id):
                raise ValueError(
                    "archive workers require archiveUrl and cannot target another archive"
                )
            if entry.role != "archive" and entry.archive_url:
                raise ValueError("only archive workers expose an archive endpoint")
            prior = session.get(EdgeServer, entry.edge_server_id)
            if prior and (
                prior.site_id != entry.site_id or prior.principal_id != entry.principal_id
            ):
                raise ValueError(
                    "edge server identity and site are immutable; register a replacement"
                )
            if prior:
                prior.assets, prior.queue_url = entry.assets, entry.queue_url
                prior.role, prior.archive_url = entry.role, entry.archive_url
            else:
                session.add(
                    EdgeServer(
                        id=entry.edge_server_id,
                        principal_id=entry.principal_id,
                        site_id=entry.site_id,
                        assets=entry.assets,
                        queue_url=entry.queue_url,
                        role=entry.role,
                        archive_url=entry.archive_url,
                    )
                )
        session.flush()
        for entry in data.edge_servers:
            row = session.get(EdgeServer, entry.edge_server_id)
            if entry.archive_server_id:
                target = session.get(EdgeServer, entry.archive_server_id)
                if (
                    target is None
                    or target.role != "archive"
                    or target.site_id != entry.site_id
                    or not set(entry.assets).issubset(target.assets)
                ):
                    raise ValueError(
                        "archive target must cover this installation's site and assets"
                    )
            row.archive_server_id = entry.archive_server_id
        queues = list(session.scalars(select(EdgeServer.queue_url)))
        if len(queues) != len(set(queues)):
            raise ValueError("each installation requires a distinct SQS queue")
        session.add(
            Audit(
                principal_id="operator",
                action="registration.apply",
                object_id="deployment-manifest",
                at=now_ms(),
            )
        )


def main() -> int:
    configure_logging("backend-admin")
    return run_cli()


@logged()
def run_cli() -> int:
    parser = argparse.ArgumentParser(description="DSC registration and operations")
    parser.add_argument("--env-file", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("register")
    bootstrap.add_argument("manifest", type=Path)
    commands.add_parser("edge-server-status")
    args = parser.parse_args()
    settings = load_secrets(Settings(_env_file=args.env_file))
    configure_logging("backend-admin", settings.log_level)
    _, sessions = database(settings.database_url.get_secret_value())
    if args.command == "register":
        data = Registration.model_validate_json(args.manifest.read_text())
        register(sessions, data)
        print("등록 설정 적용 완료")
    else:
        with sessions() as session:
            for row in session.scalars(select(EdgeServer).order_by(EdgeServer.id)):
                print(
                    json.dumps(
                        {
                            "edgeServerId": row.id,
                            "siteId": row.site_id,
                            "status": "ONLINE"
                            if row.last_seen is not None and now_ms() - row.last_seen < 300000
                            else "OFFLINE",
                            "lastReceivedAtMs": row.last_seen,
                        },
                        ensure_ascii=False,
                    )
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
