"""Office HTTPS receiver. TLS terminates at the configured reverse proxy or VPN endpoint."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from dsc.backend.observability import LoggedRoute, RequestLogging
from dsc.edge.client import EdgeClient
from dsc.edge.config import EdgeSettings
from dsc.edge.files import ImmutableWriter, check_volume, copy_lock, fingerprint
from dsc.edge.paths import open_clip
from dsc.edge.store import EdgeStore
from dsc.shared.contracts import ReplicaBody, milliseconds
from dsc.shared.observability import configure_logging, emit_event


def create_receiver(
    settings: EdgeSettings, client: EdgeClient | None = None, store: EdgeStore | None = None
) -> FastAPI:
    if settings.role != "archive":
        raise ValueError("the receiver requires the archive installation role")
    configure_logging("archive-receiver", settings.log_level)
    client = client or EdgeClient(settings)
    store = store or EdgeStore(settings.state_dir / "outbox.sqlite", recover_sending=False)
    app = FastAPI(
        title="DSC office archive receiver", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.router.route_class = LoggedRoute
    app.add_middleware(RequestLogging)

    async def receive(
        transfer_id: UUID, request: Request, authorization: str, verify: bool = False
    ) -> dict:
        if not authorization.startswith("Bearer ") or len(authorization) > 300:
            raise HTTPException(401, "a transfer capability is required")
        token = authorization[7:]
        path = f"/edge-server/archive/{transfer_id}"
        try:
            metadata = await run_in_threadpool(
                client.post, f"{path}/authorize", {"transferToken": token}
            )
            if metadata["receiverId"] != settings.server_id:
                raise ValueError("transfer was assigned to another receiver")
            content = ReplicaBody.model_validate(
                {
                    key: metadata[key]
                    for key in ("eventId", "clipId", "revision", "sizeBytes", "checksumSha256")
                }
            )
            await run_in_threadpool(check_volume, settings.nas_root, settings.archive_volume_id)
            destination = f"{content.event_id}/{content.revision}/{content.clip_id}.mp4"
            lock = copy_lock(
                settings.state_dir, str(content.event_id), content.revision, exclusive=True
            )
            await run_in_threadpool(lock.__enter__)
            try:
                if verify:

                    def verify_file():
                        with open_clip(settings.nas_root, destination) as stream:
                            return fingerprint(stream)

                    size, checksum = await run_in_threadpool(verify_file)
                    if (size, checksum) != (content.size_bytes, content.checksum_sha256):
                        raise ValueError("archive copy differs from registered content")
                else:
                    if request.headers.get("content-type") != "video/mp4" or request.headers.get(
                        "content-length"
                    ) != str(content.size_bytes):
                        raise ValueError("declared upload headers differ from transfer metadata")
                    writer = ImmutableWriter(
                        settings.nas_root, destination, content.size_bytes, content.checksum_sha256
                    )
                    await run_in_threadpool(writer.__enter__)
                    try:
                        async with asyncio.timeout(settings.upload_timeout):
                            async for chunk in request.stream():
                                await run_in_threadpool(writer.write, chunk)
                        size, checksum = await run_in_threadpool(writer.finish)
                    finally:
                        await run_in_threadpool(writer.__exit__, None, None, None)
                await run_in_threadpool(check_volume, settings.nas_root, settings.archive_volume_id)
                await run_in_threadpool(
                    store.save_copy,
                    {
                        "event_id": str(content.event_id),
                        "clip_id": str(content.clip_id),
                        "revision": content.revision,
                        "relative_path": destination,
                        "size_bytes": size,
                        "checksum": checksum,
                        "created_at": milliseconds(
                            datetime.fromisoformat(metadata["createdAt"].replace("Z", "+00:00"))
                        ),
                    },
                )
                receipt = await run_in_threadpool(
                    client.post,
                    f"{path}/complete",
                    {"transferToken": token, "sizeBytes": size, "checksumSha256": checksum},
                )
                await run_in_threadpool(
                    store.copy_state, str(content.event_id), content.revision, "STORED", 0, receipt
                )
                return receipt
            finally:
                await run_in_threadpool(lock.__exit__, None, None, None)
        except FileNotFoundError as exc:
            raise HTTPException(404, "archive copy or volume unavailable") from exc
        except (ValueError, KeyError, TypeError) as exc:
            emit_event("archive_verification_failed", level=logging.WARNING)
            raise HTTPException(409, "archive metadata or checksum mismatch") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise HTTPException(
                status if status in {401, 403, 404, 409} else 503,
                "backend transfer authorization or confirmation failed",
            ) from exc
        except (httpx.RequestError, OSError, TimeoutError) as exc:
            emit_event("archive_receive_pending", level=logging.WARNING)
            raise HTTPException(503, "archive transfer must be retried") from exc

    @app.put("/archive/transfers/{transfer_id}")
    async def upload(transfer_id: UUID, request: Request, authorization: str = Header(default="")):
        return await receive(transfer_id, request, authorization)

    @app.post("/archive/transfers/{transfer_id}/verify")
    async def verify(transfer_id: UUID, request: Request, authorization: str = Header(default="")):
        return await receive(transfer_id, request, authorization, verify=True)

    app.state.client = client
    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="DSC office archive HTTPS upstream receiver")
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    settings = EdgeSettings(_env_file=args.env_file)
    # Proxy access logs must also omit Authorization and request bodies.
    uvicorn.run(
        create_receiver(settings),
        host=settings.receiver_host,
        port=settings.receiver_port,
        access_log=False,
        timeout_keep_alive=5,
        limit_concurrency=16,
    )
