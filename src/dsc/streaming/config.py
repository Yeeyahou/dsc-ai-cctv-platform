from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DSC_LIVE_", extra="ignore")
    environment: Literal["production", "test"] = "production"
    api_url: str
    token_url: str
    client_id: str
    secret_file: Path
    oauth_origin: str
    staff_client_id: str
    browser_origin: str
    allowed_gateway_origins: list[str]
    state_dir: Path = Path("/var/lib/dsc/live")
    mediamtx_binary: Path = Path("/usr/local/bin/mediamtx")
    mediamtx_config: Path = Path("/etc/dsc/mediamtx.json")
    request_timeout: float = Field(default=5, gt=0, le=10)
    poll_seconds: int = Field(default=5, ge=1, le=10)
    max_peers: int = Field(default=32, ge=1, le=64)
    role: Literal["field"] = "field"

    @model_validator(mode="after")
    def endpoints(self) -> "GatewaySettings":
        for value in [
            self.api_url,
            self.token_url,
            self.oauth_origin,
            self.browser_origin,
            *self.allowed_gateway_origins,
        ]:
            url = urlsplit(value)
            if (
                not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or not (
                    url.scheme == "https"
                    or self.environment == "test"
                    and url.scheme == "http"
                    and url.hostname == "127.0.0.1"
                )
            ):
                raise ValueError("live endpoints require HTTPS without credentials")
        for value in [self.oauth_origin, self.browser_origin, *self.allowed_gateway_origins]:
            if urlsplit(value).path not in {"", "/"}:
                raise ValueError("browser and OAuth origins must not contain paths")
        if not self.allowed_gateway_origins:
            raise ValueError("at least one approved gateway origin is required")
        return self
