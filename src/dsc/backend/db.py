from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool


class Base(DeclarativeBase):
    pass


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128), index=True)
    asset_type: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(256))
    aas: Mapped[dict] = mapped_column(JSON, default=dict)


class Principal(Base):
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    sites: Mapped[list] = mapped_column(JSON, default=list)
    assets: Mapped[list] = mapped_column(JSON, default=list)
    submodels: Mapped[list] = mapped_column(JSON, default=lambda: ["Nameplate", "TechnicalData"])
    video: Mapped[bool] = mapped_column(Boolean, default=False)


class EdgeServer(Base):
    __tablename__ = "edge_servers"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), unique=True)
    site_id: Mapped[str] = mapped_column(String(128))
    assets: Mapped[list] = mapped_column(JSON)
    queue_url: Mapped[str] = mapped_column(Text)
    last_seen: Mapped[int | None] = mapped_column(BigInteger)
    heartbeat: Mapped[dict | None] = mapped_column(JSON)
    role: Mapped[str] = mapped_column(String(16), default="legacy", server_default="legacy")
    archive_url: Mapped[str | None] = mapped_column(Text)
    archive_server_id: Mapped[str | None] = mapped_column(ForeignKey("edge_servers.id"))


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    edge_server_id: Mapped[str] = mapped_column(ForeignKey("edge_servers.id"))
    site_id: Mapped[str] = mapped_column(String(128))
    event_code: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[int] = mapped_column(BigInteger)
    received_at: Mapped[int] = mapped_column(BigInteger)
    incident_id: Mapped[str | None] = mapped_column(String(36), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        Index("ix_events_site_time_id", "site_id", "occurred_at", "id"),
        Index("ix_events_code_time", "event_code", "occurred_at"),
    )


class EventAsset(Base):
    __tablename__ = "event_assets"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(16), primary_key=True)


class EventMedia(Base):
    __tablename__ = "event_media"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), primary_key=True)
    clip_id: Mapped[str] = mapped_column(String(36), unique=True)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)


class ClipReplica(Base):
    __tablename__ = "clip_replicas"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    edge_server_id: Mapped[str] = mapped_column(ForeignKey("edge_servers.id"), primary_key=True)
    clip_id: Mapped[str] = mapped_column(String(36))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum: Mapped[str] = mapped_column(String(44))
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[int] = mapped_column(BigInteger)


class ArchiveTransfer(Base):
    __tablename__ = "archive_transfers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    clip_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[str] = mapped_column(ForeignKey("edge_servers.id"))
    receiver_id: Mapped[str] = mapped_column(ForeignKey("edge_servers.id"))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum: Mapped[str] = mapped_column(String(44))
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    completed_at: Mapped[int | None] = mapped_column(BigInteger)


class FileJob(Base):
    __tablename__ = "file_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    clip_id: Mapped[str] = mapped_column(String(36))
    edge_server_id: Mapped[str] = mapped_column(ForeignKey("edge_servers.id"), index=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    active_key: Mapped[str | None] = mapped_column(String(73), unique=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    deadline: Mapped[int] = mapped_column(BigInteger, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    dispatch_version: Mapped[int] = mapped_column(Integer, default=1)
    dispatched_version: Mapped[int] = mapped_column(Integer, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    object_expires_at: Mapped[int | None] = mapped_column(BigInteger)
    media_revision: Mapped[int | None] = mapped_column(Integer)
    expected_size: Mapped[int | None] = mapped_column(BigInteger)
    expected_checksum: Mapped[str | None] = mapped_column(String(44))
    assigned_at: Mapped[int | None] = mapped_column(BigInteger)


class Attempt(Base):
    __tablename__ = "job_leases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("file_requests.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str] = mapped_column(String(64))
    lease_until: Mapped[int] = mapped_column(BigInteger)
    receipt: Mapped[str | None] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(String(44))
    deleted_at: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("request_id", "number"),)


class Audit(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_id: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str] = mapped_column(String(256))
    at: Mapped[int] = mapped_column(BigInteger, index=True)


def database(url: str):
    options = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        # Bound Lambda concurrency plus no retained idle connections protects a small RDS instance.
        options["poolclass"] = NullPool
        options["connect_args"] = {"connect_timeout": 5, "options": "-c statement_timeout=10000"}
    engine = create_engine(url, **options)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def sqlite_setup(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")

    return engine, sessionmaker(engine, expire_on_commit=False)
