import logging
from dataclasses import dataclass

import jwt
from sqlalchemy import exists, or_, select

from dsc.backend.db import Asset, EdgeServer, Event, EventAsset, Principal
from dsc.backend.errors import Problem, missing
from dsc.shared.observability import log_methods, logged


@log_methods(level=logging.DEBUG, include=("__call__",))
class CognitoVerifier:
    def __init__(self, issuer: str, clients: list[str]):
        self.issuer = issuer.rstrip("/")
        self.clients = set(clients)
        self.keys = jwt.PyJWKClient(f"{self.issuer}/.well-known/jwks.json", timeout=5)

    def __call__(self, token: str) -> dict:
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={
                    "verify_aud": False,
                    "require": ["exp", "iss", "sub", "client_id", "token_use"],
                },
            )
            if claims["token_use"] != "access" or claims["client_id"] not in self.clients:
                raise jwt.InvalidTokenError("invalid access token")
            return claims
        except (jwt.PyJWTError, ValueError) as exc:
            raise Problem(401, "INVALID_TOKEN", "유효한 access token이 필요합니다") from exc


@log_methods(level=logging.DEBUG, fields={"principal": "self.principal.id"})
@dataclass(frozen=True)
class Access:
    principal: Principal
    scopes: frozenset[str]
    edge_server: EdgeServer | None

    def require(self, *scopes: str) -> None:
        if not set(scopes).issubset(self.scopes):
            raise Problem(403, "INSUFFICIENT_SCOPE", "요청 기능에 대한 권한이 없습니다")

    def asset_allowed(self, asset: Asset) -> bool:
        return asset.site_id in self.principal.sites or asset.id in self.principal.assets

    def event_clause(self):
        unauthorized = exists(
            select(EventAsset.event_id).where(
                EventAsset.event_id == Event.id,
                EventAsset.asset_id.not_in(self.principal.assets),
            )
        )
        return or_(Event.site_id.in_(self.principal.sites), ~unauthorized)

    def check_event(self, session, event: Event, video: bool = False) -> None:
        if video and not self.principal.video:
            raise missing()
        if event.site_id not in self.principal.sites:
            linked = set(
                session.scalars(select(EventAsset.asset_id).where(EventAsset.event_id == event.id))
            )
            if not linked or not linked.issubset(self.principal.assets):
                raise missing()


@logged(level=logging.DEBUG)
def authenticate(session, claims: dict) -> Access:
    # Cognito user access tokens have username; M2M tokens are mapped only by client_id.
    key = f"user:{claims['sub']}" if "username" in claims else f"client:{claims['client_id']}"
    principal = session.get(Principal, key)
    if principal is None or not principal.active:
        raise Problem(401, "INACTIVE_PRINCIPAL", "등록된 활성 주체가 필요합니다")
    scopes = frozenset(claims.get("scope", "").split()) & frozenset(principal.scopes)
    edge_server = session.scalar(select(EdgeServer).where(EdgeServer.principal_id == key))
    return Access(principal, scopes, edge_server)
