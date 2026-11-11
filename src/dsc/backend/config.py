from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DSC_", extra="ignore")
    environment: str = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: SecretStr = SecretStr("sqlite:///runtime/backend.sqlite")
    database_secret_arn: str = ""
    cognito_issuer: str = ""
    allowed_client_ids: list[str] = []
    additional_client_ids: list[str] = []
    allowed_origins: list[str] = []
    cursor_secret: SecretStr = SecretStr("")
    runtime_secret_arn: str = ""
    aws_region: str = "ap-northeast-2"
    clip_bucket: str = ""
    queue_prefix: str = ""
    public_base_url: str = "https://dsc.f2x.ai"
    lease_seconds: int = Field(default=300, ge=60)
    job_seconds: int = Field(default=1800, ge=300)
    max_attempts: int = Field(default=3, ge=1, le=10)
    clip_retention_seconds: int = Field(default=86400, ge=300)
    put_url_seconds: int = Field(default=900, ge=60, le=3600)
    get_url_seconds: int = Field(default=300, ge=1, le=300)
    worker_offline_seconds: int = Field(default=180, ge=30)
    assignment_seconds: int = Field(default=60, ge=10)
    archive_transfer_seconds: int = Field(default=900, ge=60, le=3600)
    local_retention_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def production(self) -> "Settings":
        if self.environment not in {"local", "test", "production"}:
            raise ValueError("environment must be local, test or production")
        if self.environment == "production":
            if not self.cognito_issuer.startswith("https://cognito-idp."):
                raise ValueError("production requires a Cognito issuer")
            if not self.clip_bucket or not self.allowed_client_ids:
                raise ValueError("production requires clip bucket and client allowlist")
            if not self.database_secret_arn and not self.database_url.get_secret_value().startswith(
                "postgresql+psycopg://"
            ):
                raise ValueError("production requires PostgreSQL")
            if not self.database_secret_arn:
                from sqlalchemy.engine import make_url

                query = make_url(self.database_url.get_secret_value()).query
                if query.get("sslmode") != "verify-full" or not query.get("sslrootcert"):
                    raise ValueError("production requires verified PostgreSQL TLS")
            if not self.runtime_secret_arn and len(self.cursor_secret.get_secret_value()) < 32:
                raise ValueError("production requires a cursor secret of at least 32 characters")
        return self
