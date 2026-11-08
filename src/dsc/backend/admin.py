import argparse
import json
import re
import sys
from pathlib import Path

from pydantic import Field, field_validator
from sqlalchemy import select

from dsc.backend.aas import PROPERTIES
from dsc.backend.aws import load_secrets
from dsc.backend.config import Settings
from dsc.backend.db import Agent, Asset, Audit, Principal, database
from dsc.shared.contracts import Contract, Identifier, now_ms

SCOPES = {"dsc/aas.read", "dsc/timeseries.read", "dsc/ingest.write", "dsc/agent.work"}


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


class AgentRegistration(Contract):
    agent_id: Identifier
    principal_id: str
    site_id: Identifier
    assets: list[Identifier] = Field(min_length=1)
    queue_url: str


class Registration(Contract):
    assets: list[AssetRegistration] = []
    principals: list[PrincipalRegistration] = []
    agents: list[AgentRegistration] = []


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
        for entry in data.agents:
            principal = session.get(Principal, entry.principal_id)
            if (
                principal is None
                or not entry.principal_id.startswith("client:")
                or not {"dsc/ingest.write", "dsc/agent.work"}.issubset(principal.scopes)
            ):
                raise ValueError(
                    "agent requires a registered M2M principal with ingest/work scopes"
                )
            if not entry.queue_url.startswith("https://sqs."):
                raise ValueError("an AWS per-agent SQS queue URL is required")
            for asset_id in entry.assets:
                asset = session.get(Asset, asset_id)
                if asset is None or asset.site_id != entry.site_id:
                    raise ValueError("agent asset belongs to another site")
            prior = session.get(Agent, entry.agent_id)
            if prior and (
                prior.site_id != entry.site_id or prior.principal_id != entry.principal_id
            ):
                raise ValueError(
                    "agent identity and site are immutable; register a replacement agent"
                )
            if prior:
                prior.assets, prior.queue_url = entry.assets, entry.queue_url
            else:
                session.add(
                    Agent(
                        id=entry.agent_id,
                        principal_id=entry.principal_id,
                        site_id=entry.site_id,
                        assets=entry.assets,
                        queue_url=entry.queue_url,
                    )
                )
        session.add(
            Audit(
                principal_id="operator",
                action="registration.apply",
                object_id="deployment-manifest",
                at=now_ms(),
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="DSC registration and operations")
    parser.add_argument("--env-file", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("register")
    bootstrap.add_argument("manifest", type=Path)
    commands.add_parser("agent-status")
    args = parser.parse_args()
    settings = load_secrets(Settings(_env_file=args.env_file))
    _, sessions = database(settings.database_url.get_secret_value())
    if args.command == "register":
        data = Registration.model_validate_json(args.manifest.read_text())
        register(sessions, data)
        print("등록 설정 적용 완료")
    else:
        with sessions() as session:
            for row in session.scalars(select(Agent).order_by(Agent.id)):
                print(
                    json.dumps(
                        {
                            "agentId": row.id,
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
