from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EdgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DSC_EDGE_", extra="ignore")
    environment: str = "production"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    role: Literal["legacy", "field", "archive"] = "legacy"
    server_id: str
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
    cache_root: Path | None = None
    archive_volume_id: str = ""
    archive_allowed_hosts: list[str] = []
    replication_seconds: int = Field(default=30, ge=1)
    retention_days: int = Field(default=30, ge=1, le=365)
    disk_warning_bytes: int = Field(default=5 * 1024**3, ge=0)
    receiver_host: str = "127.0.0.1"
    receiver_port: int = Field(default=8080, ge=1, le=65535)
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
        self.cache_root = (self.cache_root or self.state_dir / "clips").resolve()
        if self.role == "archive" and not self.archive_volume_id:
            raise ValueError("archive workers require a provisioned archive_volume_id marker")
        if self.role == "field" and not self.archive_allowed_hosts:
            raise ValueError(
                "field installations require an explicit archive_allowed_hosts allowlist"
            )
        return self
