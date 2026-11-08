from __future__ import annotations

import base64
import json
import logging
import re
from importlib.resources import files

from jsonschema import Draft202012Validator
from sqlalchemy import or_, select

from dsc.backend.auth import Access
from dsc.backend.db import Asset
from dsc.backend.errors import Problem, missing
from dsc.shared.contracts import digest
from dsc.shared.observability import log_methods

SCHEMA_ROOT = files("dsc.backend").joinpath("schemas")
SCHEMA = json.loads(SCHEMA_ROOT.joinpath("aas-metamodel-3.1.2.json").read_text())
PROPERTIES = json.loads(SCHEMA_ROOT.joinpath("properties-v1.json").read_text())


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decoded(value: str) -> str:
    try:
        if len(value) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError()
        result = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        ).decode()
        if encoded(result) != value:
            raise ValueError()
        return result
    except (ValueError, UnicodeError) as exc:
        raise Problem(400, "INVALID_AAS_IDENTIFIER", "base64url 식별자가 필요합니다") from exc


def reference(kind: str, value: str, external=False) -> dict:
    return {
        "type": "ExternalReference" if external else "ModelReference",
        "keys": [{"type": "GlobalReference" if external else kind, "value": value}],
    }


def validate_model(value: dict, kind: str) -> dict:
    schema = {**SCHEMA, "$ref": f"#/definitions/{kind}"}
    schema.pop("allOf", None)
    Draft202012Validator(schema).validate(value)
    return value


@log_methods(level=logging.DEBUG, fields={"principal": "access.principal.id"})
class AasAdapter:
    def __init__(self, platform):
        self.platform = platform
        self.base = platform.settings.public_base_url.rstrip("/")

    @staticmethod
    def kinds(access: Access, asset: Asset) -> list[str]:
        kinds = (
            set(access.principal.submodels)
            & set(asset.aas.get("submodels", {}))
            & PROPERTIES.keys()
        )
        if "dsc/timeseries.read" not in access.scopes:
            kinds.discard("TimeSeries")
        return sorted(kinds)

    def assets(self, access: Access) -> list[Asset]:
        access.require("dsc/aas.read")
        with self.platform.sessions() as session:
            return list(
                session.scalars(
                    select(Asset)
                    .where(
                        or_(
                            Asset.site_id.in_(access.principal.sites),
                            Asset.id.in_(access.principal.assets),
                        )
                    )
                    .order_by(Asset.id)
                )
            )

    def find(self, access: Access, value: str, submodel=False):
        access.require("dsc/aas.read")
        for asset in self.assets(access):
            if submodel:
                for kind in self.kinds(access, asset):
                    if asset.aas["submodels"][kind]["id"] == value:
                        return asset, kind
            elif asset.aas.get("aasId") == value:
                return asset
        raise missing()

    def shell(self, access: Access, asset: Asset) -> dict:
        return validate_model(
            {
                "modelType": "AssetAdministrationShell",
                "id": asset.aas["aasId"],
                "idShort": asset.aas["idShort"],
                "assetInformation": {
                    "assetKind": "Instance",
                    "globalAssetId": asset.aas["globalAssetId"],
                },
                "submodels": [
                    reference("Submodel", asset.aas["submodels"][k]["id"])
                    for k in self.kinds(access, asset)
                ],
            },
            "AssetAdministrationShell",
        )

    def submodel(self, access: Access, asset: Asset, kind: str, value_only=False) -> dict:
        if kind == "TimeSeries":
            values = self.platform.current(access, asset.id)
            values["endpoint"] = f"{self.base}/timeseries/{asset.id}/archive"
        else:
            values = asset.aas.get("values", {}).get(kind, {})
        values = {key: value for key, value in values.items() if key in PROPERTIES[kind]}
        if value_only:
            return values
        config = asset.aas["submodels"][kind]
        return validate_model(
            {
                "modelType": "Submodel",
                "id": config["id"],
                "idShort": kind,
                "kind": "Instance",
                "semanticId": reference("", config["semanticId"], external=True),
                "submodelElements": [
                    {
                        "modelType": "Property",
                        "idShort": key,
                        "valueType": f"xs:{PROPERTIES[kind][key]}",
                        "value": str(value),
                    }
                    for key, value in values.items()
                ],
            },
            "Submodel",
        )

    def descriptor(self, access: Access, asset: Asset, kind: str | None = None) -> dict:
        if kind:
            config = asset.aas["submodels"][kind]
            return {
                "id": config["id"],
                "idShort": kind,
                "semanticId": reference("", config["semanticId"], external=True),
                "endpoints": [
                    self._endpoint(f"/submodels/{encoded(config['id'])}", "SUBMODEL-3.1")
                ],
            }
        return {
            "id": asset.aas["aasId"],
            "idShort": asset.aas["idShort"],
            "assetKind": "Instance",
            "globalAssetId": asset.aas["globalAssetId"],
            "endpoints": [self._endpoint(f"/shells/{encoded(asset.aas['aasId'])}", "AAS-3.1")],
            "submodelDescriptors": [
                self.descriptor(access, asset, k) for k in self.kinds(access, asset)
            ],
        }

    def _endpoint(self, path: str, interface: str) -> dict:
        return {
            "interface": interface,
            "protocolInformation": {
                "href": f"{self.base}{path}",
                "endpointProtocol": "HTTP",
                "endpointProtocolVersion": ["1.1"],
            },
        }

    def page(
        self, access: Access, values: list, context: str, limit: int, cursor: str | None
    ) -> dict:
        if not 1 <= limit <= 500:
            raise Problem(422, "INVALID_LIMIT", "limit는 1~500이어야 합니다")
        binding = digest(
            {
                "route": context,
                "principal": access.principal.id,
                "sites": sorted(access.principal.sites),
                "assets": sorted(access.principal.assets),
                "submodels": sorted(access.principal.submodels),
                "scopes": sorted(access.scopes),
                "limit": limit,
            }
        )
        start = int(self.platform._decode_cursor(cursor, binding)[0]) if cursor else 0
        result = {"result": values[start : start + limit], "paging_metadata": {}}
        if start + limit < len(values):
            result["paging_metadata"]["cursor"] = self.platform._encode_cursor(
                binding, [start + limit]
            )
        return result
