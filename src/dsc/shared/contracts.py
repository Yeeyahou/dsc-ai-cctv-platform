from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
MetricNumber = Annotated[FiniteFloat, Field(strict=True)]
EVENT_CODES = frozenset(
    [
        "DSC-101",
        "DSC-102A",
        "DSC-102B",
        "DSC-103A",
        "DSC-103B",
        "DSC-103C",
        "DSC-104A",
        "DSC-104B",
        "DSC-105",
        "DSC-106A",
        "DSC-106B",
        "DSC-201",
        "DSC-202A",
        "DSC-202B",
        "DSC-203",
        "DSC-301",
        "DSC-302",
        "DSC-303",
    ]
)
PAIRED_CODES = frozenset(
    ["DSC-102A", "DSC-102B", "DSC-104A", "DSC-104B", "DSC-106A", "DSC-106B", "DSC-202A", "DSC-202B"]
)
METRICS = {
    "pingLatencyMs": ({"DSC-101"}, 0, None),
    "packetLossRatePct": ({"DSC-101"}, 0, 100),
    "estimatedClearanceMarginM": ({"DSC-102A", "DSC-102B"}, 0, None),
    "delaySec": (EVENT_CODES, 0, None),
    "estimatedDropHeightM": ({"DSC-105"}, 0, None),
    "readFailureCount": ({"DSC-201"}, 0, None),
    "fireAnomalyScore": ({"DSC-202A"}, None, None),
    "noiseVariance": ({"DSC-203"}, None, None),
}


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def milliseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone is required")
    return int(value.timestamp() * 1000)


def canonical(value: dict) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: dict | str) -> str:
    raw = canonical(value) if isinstance(value, dict) else value
    return hashlib.sha256(raw.encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", allow_inf_nan=False
    )

    @model_validator(mode="before")
    @classmethod
    def absent_instead_of_null(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("omit absent optional fields instead of sending null")
        return value

    def wire(self) -> dict:
        def convert(value):
            if isinstance(value, datetime):
                return iso(milliseconds(value))
            if isinstance(value, UUID):
                return str(value)
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, list):
                return [convert(item) for item in value]
            return value

        return convert(self.model_dump(mode="python", by_alias=True, exclude_none=True))


class AssetLink(Contract):
    asset_id: Identifier
    role: Literal["detector", "subject", "context"]


class Media(Contract):
    clip_id: UUID
    status: Literal["RECORDING", "AVAILABLE", "MISSING", "NOT_APPLICABLE"]
    started_at: datetime | None = None
    ended_at: datetime | None = None
    content_type: Literal["video/mp4"] | None = None
    size_bytes: Annotated[int, Field(strict=True, gt=0)] | None = None

    @field_validator("started_at", "ended_at")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            milliseconds(value)
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def available(self) -> Media:
        if self.status == "AVAILABLE" and any(
            x is None for x in (self.started_at, self.ended_at, self.content_type, self.size_bytes)
        ):
            raise ValueError("AVAILABLE requires startedAt, endedAt, contentType and sizeBytes")
        if self.started_at and self.ended_at and self.started_at >= self.ended_at:
            raise ValueError("endedAt must be later than startedAt")
        return self


class MediaUpdate(Media):
    revision: Annotated[int, Field(strict=True, ge=1)]


class EventInput(Contract):
    schema_version: Literal["1.0"]
    event_id: UUID
    event_code: str
    site_id: Identifier
    occurred_at: datetime
    incident_id: UUID | None = None
    assets: Annotated[list[AssetLink], Field(min_length=1, max_length=100)]
    metrics: dict[str, MetricNumber] | None = None
    confidence: Annotated[MetricNumber, Field(ge=0, le=1)] | None = None
    model_version: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    media: Media | None = None

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        milliseconds(value)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def event_rules(self) -> EventInput:
        if self.event_code not in EVENT_CODES:
            raise ValueError("unknown eventCode")
        if self.event_code in PAIRED_CODES and self.incident_id is None:
            raise ValueError("incidentId is required for paired events")
        for key, value in (self.metrics or {}).items():
            if key not in METRICS:
                raise ValueError(f"unknown metric {key}")
            codes, low, high = METRICS[key]
            if self.event_code not in codes:
                raise ValueError(f"metric {key} is not valid for this eventCode")
            if (low is not None and value < low) or (high is not None and value > high):
                raise ValueError(f"metric {key} is outside the allowed range")
            if key == "readFailureCount" and not value.is_integer():
                raise ValueError("readFailureCount must be an integer")
        # Remove duplicate relations while preserving their first occurrence.
        self.assets = list({(a.asset_id, a.role): a for a in self.assets}.values())
        return self


class Heartbeat(Contract):
    schema_version: Literal["1.1"]
    edge_server_version: Annotated[str, Field(min_length=1, max_length=128)]
    observed_at: datetime
    outbox_pending_count: Annotated[int, Field(strict=True, ge=0)]
    disk_free_bytes: Annotated[int, Field(strict=True, ge=0)]
    nas_status: Literal["OK", "UNREACHABLE", "READ_ERROR"]
    oldest_pending_at: datetime | None = None

    @field_validator("observed_at", "oldest_pending_at")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            milliseconds(value)
        return value

    @model_validator(mode="after")
    def oldest_required(self) -> Heartbeat:
        if self.outbox_pending_count > 0 and self.oldest_pending_at is None:
            raise ValueError("oldestPendingAt is required when pending events exist")
        return self


class EmptyBody(Contract):
    pass


class LeaseBody(Contract):
    lease_token: Annotated[str, Field(min_length=20, max_length=256)]


class UploadBody(LeaseBody):
    size_bytes: Annotated[int, Field(strict=True, gt=0, le=512 * 1024 * 1024)]
    checksum_sha256: str
    content_type: Literal["video/mp4"]

    @field_validator("checksum_sha256")
    @classmethod
    def checksum(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("invalid SHA-256 base64 checksum") from exc
        if len(raw) != 32 or base64.b64encode(raw).decode() != value:
            raise ValueError("SHA-256 must encode 32 bytes")
        return value


class FailureBody(LeaseBody):
    failure_code: Literal[
        "SOURCE_NOT_FOUND", "SOURCE_UNREACHABLE", "UPLOAD_FAILED", "UPLOAD_VERIFICATION_FAILED"
    ]
    retryable: bool


class LocalMedia(Contract):
    clip_id: UUID
    relative_path: Annotated[str, Field(min_length=1, max_length=2048)]


class EventEnvelope(Contract):
    kind: Literal["event"]
    event: EventInput
    local_media: LocalMedia | None = None

    @model_validator(mode="after")
    def matching_clip(self) -> EventEnvelope:
        if self.local_media and (
            self.event.media is None or self.local_media.clip_id != self.event.media.clip_id
        ):
            raise ValueError("localMedia clipId must match event.media.clipId")
        return self


class MediaEnvelope(Contract):
    kind: Literal["media"]
    event_id: UUID
    media: MediaUpdate
    local_media: LocalMedia | None = None

    @model_validator(mode="after")
    def matching_clip(self) -> MediaEnvelope:
        if self.local_media and self.local_media.clip_id != self.media.clip_id:
            raise ValueError("localMedia clipId must match media.clipId")
        return self
