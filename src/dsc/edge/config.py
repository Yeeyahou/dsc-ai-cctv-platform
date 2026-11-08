from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EdgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DSC_EDGE_", extra="ignore")
    environment: str = "production"
    agent_id: str
    site_id: str
    api_url: str
    token_url: str
    client_id: str
    secret_file: Path
    nas_root: Path
    state_dir: Path = Path("runtime/edge")
    inbox: Path = Path("runtime/inbox")
    heartbeat_seconds: int = Field(default=60, ge=1)
    claim_seconds: int = Field(default=5, ge=1)
    lease_renew_seconds: int = Field(default=60, ge=1)
    request_timeout: float = Field(default=30, gt=0)
    upload_timeout: float = Field(default=120, gt=0)
    max_clip_bytes: int = Field(default=512 * 1024 * 1024, gt=0, le=512 * 1024 * 1024)
    max_inbox_bytes: int = Field(default=300 * 1024, gt=0)

    @model_validator(mode="after")
    def secure_endpoints(self) -> "EdgeSettings":
        if self.environment not in {"production", "local", "test"}:
            raise ValueError("invalid edge environment")
        for url in (self.api_url, self.token_url):
            if not url.startswith("https://") and not (
                self.environment in {"local", "test"} and url.startswith("http://127.0.0.1")
            ):
                raise ValueError("HTTPS endpoints are required")
        self.nas_root = self.nas_root.resolve()
        return self
