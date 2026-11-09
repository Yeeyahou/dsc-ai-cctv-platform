from __future__ import annotations

import json
import logging
from datetime import datetime
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from dsc.backend.aas import AasAdapter, decoded, reference
from dsc.backend.auth import Access, CognitoVerifier, authenticate
from dsc.backend.errors import Problem
from dsc.backend.observability import LoggedRoute, RequestLogging
from dsc.backend.service import Filters, Platform
from dsc.shared.contracts import (
    EmptyBody,
    EventInput,
    FailureBody,
    Heartbeat,
    LeaseBody,
    MediaUpdate,
    UploadBody,
    milliseconds,
)
from dsc.shared.observability import configure_logging, emit_event, logged


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > 256 * 1024:
                emit_event(
                    "request_rejected",
                    level=logging.WARNING,
                    errorCode="BODY_TOO_LARGE",
                    status=413,
                )
                response = JSONResponse(
                    {
                        "error": {
                            "code": "BODY_TOO_LARGE",
                            "message": "요청 본문은 256 KiB 이하여야 합니다",
                            "retryable": False,
                            "traceId": scope["state"]["trace_id"],
                        }
                    },
                    status_code=413,
                )
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


def create_app(platform: Platform | None = None, verifier=None) -> FastAPI:
    configure_logging("backend")
    if platform is None:
        from dsc.backend.runtime import platform as runtime_platform

        platform = runtime_platform()
    configure_logging("backend", platform.settings.log_level)
    verify = verifier or CognitoVerifier(
        platform.settings.cognito_issuer,
        platform.settings.allowed_client_ids + platform.settings.additional_client_ids,
    )
    local_docs = platform.settings.environment == "local"
    app = FastAPI(
        title="DSC CCTV Backend",
        version="1.1.0",
        docs_url="/docs" if local_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if local_docs else None,
    )
    app.router.route_class = LoggedRoute
    app.state.platform = platform
    app.add_middleware(BodyLimit)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=platform.settings.allowed_origins,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-Id", "Location", "Retry-After"],
    )
    app.add_middleware(RequestLogging)
    aas = AasAdapter(platform)

    @app.exception_handler(Problem)
    async def problem(request: Request, exc: Problem):
        emit_event(
            "request_rejected",
            level=logging.ERROR if exc.status >= 500 else logging.WARNING,
            errorCode=exc.code,
            status=exc.status,
        )
        if request.url.path.startswith(("/shell", "/submodel", "/lookup", "/description")):
            return JSONResponse(
                {"messages": [{"code": exc.code, "messageType": "Error", "text": exc.message}]},
                status_code=exc.status,
            )
        return JSONResponse(
            {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "traceId": request.state.trace_id,
                    "retryable": exc.retryable,
                }
            },
            status_code=exc.status,
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else {},
        )

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, exc: RequestValidationError):
        if any(error["type"] == "json_invalid" for error in exc.errors()):
            return await problem(request, Problem(400, "INVALID_JSON", "잘못된 JSON입니다"))
        return await problem(
            request, Problem(422, "VALIDATION_ERROR", "요청 필드 또는 형식이 다릅니다")
        )

    @app.exception_handler(BotoCoreError)
    @app.exception_handler(ClientError)
    async def aws_error(request: Request, exc):
        emit_event("aws_request_failed", level=logging.ERROR, errorType=type(exc).__name__)
        return await problem(
            request, Problem(503, "AWS_UNAVAILABLE", "외부 저장소 접근 실패", True)
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        emit_event("database_request_failed", level=logging.ERROR, errorType=type(exc).__name__)
        return await problem(
            request, Problem(503, "STORAGE_UNAVAILABLE", "저장소 접근에 실패했습니다", True)
        )

    @logged(level=logging.DEBUG)
    def access(request: Request) -> Access:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise Problem(401, "AUTH_REQUIRED", "Bearer access token이 필요합니다")
        claims = verify(auth[7:])
        with platform.sessions() as session:
            result = authenticate(session, claims)
        request.state.principal = result.principal.id
        request.state.site_id = result.edge_server.site_id if result.edge_server else None
        return result

    def job_response(result: tuple[dict, int]):
        body, status = result
        headers = {"Location": body["statusUrl"], "Retry-After": "5"} if status == 202 else {}
        return JSONResponse(body, status_code=status, headers=headers)

    def make_filters(
        start: datetime, end: datetime, site_id=None, asset_id=None, event_code=None, limit=100
    ) -> Filters:
        try:
            return Filters(
                milliseconds(start), milliseconds(end), site_id, asset_id, event_code, limit
            )
        except ValueError as exc:
            raise Problem(422, "TIMEZONE_REQUIRED", "기간에는 시간대가 필요합니다") from exc

    @app.post(
        "/ingest",
        tags=["ingest"],
        responses={
            201: {"description": "신규 이벤트 영속 저장 완료"},
            200: {"description": "동일 이벤트 중복 수신 확인"},
        },
    )
    def ingest(body: EventInput, request: Request, actor: Access = Depends(access)):
        request.state.event_id = str(body.event_id)
        request.state.site_id = body.site_id
        result, status = platform.ingest(actor, body)
        return JSONResponse(result, status_code=status)

    @app.put("/ingest/events/{event_id}/media", tags=["ingest"])
    def media(event_id: UUID, body: MediaUpdate, actor: Access = Depends(access)):
        return platform.media(actor, str(event_id), body)

    @app.post("/edge-server/heartbeat", tags=["edge-server"])
    def heartbeat(body: Heartbeat, actor: Access = Depends(access)):
        return platform.heartbeat(actor, body)

    @app.get("/events", tags=["events"])
    def events(
        start: datetime = Query(alias="from"),
        end: datetime = Query(alias="to"),
        site_id: str | None = Query(None, alias="siteId"),
        asset_id: str | None = Query(None, alias="assetId"),
        event_code: str | None = Query(None, alias="eventCode"),
        limit: int = 100,
        cursor: str | None = None,
        actor: Access = Depends(access),
    ):
        return platform.events(
            actor, make_filters(start, end, site_id, asset_id, event_code, limit), cursor
        )

    @app.get("/events/{event_id}", tags=["events"])
    def event(event_id: UUID, actor: Access = Depends(access)):
        return platform.get_event(actor, str(event_id))

    @app.get("/timeseries/{asset_id}/current", tags=["timeseries"])
    def current(asset_id: str, actor: Access = Depends(access)):
        return platform.current(actor, asset_id)

    @app.get("/timeseries/{asset_id}/archive", tags=["timeseries"])
    def archive(
        asset_id: str,
        start: datetime = Query(alias="from"),
        end: datetime = Query(alias="to"),
        limit: int = 100,
        cursor: str | None = None,
        actor: Access = Depends(access),
    ):
        return platform.events(
            actor, make_filters(start, end, asset_id=asset_id, limit=limit), cursor
        )

    @app.post(
        "/files/{event_id}/requests",
        tags=["clips"],
        responses={
            200: {"description": "기존 READY 클립과 링크 반환"},
            202: {"description": "작업 접수 또는 처리 중, Location과 Retry-After 제공"},
        },
    )
    def request_clip(event_id: UUID, body: EmptyBody, actor: Access = Depends(access)):
        return job_response(platform.request_clip(actor, str(event_id)))

    @app.get(
        "/files/{event_id}",
        tags=["clips"],
        responses={
            202: {"description": "클립 전달 작업 처리 중"},
        },
    )
    def legacy_file(event_id: UUID, actor: Access = Depends(access)):
        return job_response(platform.file_status(actor, str(event_id), by_event=True))

    @app.get(
        "/file-requests/{request_id}",
        tags=["clips"],
        responses={
            202: {"description": "클립 전달 작업 처리 중"},
        },
    )
    def file_status(request_id: UUID, actor: Access = Depends(access)):
        return job_response(platform.file_status(actor, str(request_id)))

    @app.post(
        "/edge-server/jobs/claim",
        tags=["jobs"],
        responses={
            204: {"description": "배정할 작업 없음"},
        },
    )
    def claim(body: EmptyBody, actor: Access = Depends(access)):
        result = platform.claim(actor)
        return result if result else Response(status_code=204)

    @app.post("/edge-server/jobs/{request_id}/lease", tags=["jobs"])
    def lease(request_id: UUID, body: LeaseBody, actor: Access = Depends(access)):
        return platform.renew(actor, str(request_id), body.lease_token)

    @app.post("/edge-server/jobs/{request_id}/upload-url", tags=["jobs"])
    def upload_url(request_id: UUID, body: UploadBody, actor: Access = Depends(access)):
        return platform.upload_url(actor, str(request_id), body)

    @app.post("/edge-server/jobs/{request_id}/complete", tags=["jobs"])
    def complete(request_id: UUID, body: LeaseBody, actor: Access = Depends(access)):
        return platform.complete(actor, str(request_id), body.lease_token)

    @app.post("/edge-server/jobs/{request_id}/fail", tags=["jobs"])
    def fail(request_id: UUID, body: FailureBody, actor: Access = Depends(access)):
        return platform.fail(actor, str(request_id), body)

    @app.get("/description", tags=["aas"])
    def description(actor: Access = Depends(access)):
        actor.require("dsc/aas.read")
        # The supported read subset does not claim any complete IDTA service profile.
        return {"profiles": []}

    @app.get("/lookup/shells", tags=["aas"])
    def lookup(
        asset_ids: list[str] = Query(alias="assetIds"),
        limit: int = 100,
        cursor: str | None = None,
        actor: Access = Depends(access),
    ):
        try:
            requested = [json.loads(decoded(v)) for v in asset_ids]
            if any(v.get("name") != "globalAssetId" for v in requested):
                raise ValueError()
            values = {v["value"] for v in requested}
        except (ValueError, KeyError, TypeError) as exc:
            raise Problem(400, "INVALID_ASSET_IDS", "globalAssetId 검색 객체가 필요합니다") from exc
        ids = [a.aas["aasId"] for a in aas.assets(actor) if a.aas.get("globalAssetId") in values]
        return aas.page(actor, ids, f"lookup:{sorted(values)}", limit, cursor)

    @app.get("/shell-descriptors", tags=["aas"])
    def descriptors(limit: int = 100, cursor: str | None = None, actor: Access = Depends(access)):
        values = [aas.descriptor(actor, a) for a in aas.assets(actor)]
        return aas.page(actor, values, "descriptors", limit, cursor)

    @app.get("/shell-descriptors/{aas_id}", tags=["aas"])
    def descriptor(aas_id: str, actor: Access = Depends(access)):
        return aas.descriptor(actor, aas.find(actor, decoded(aas_id)))

    @app.get("/shells/{aas_id}", tags=["aas"])
    def shell(aas_id: str, actor: Access = Depends(access)):
        return aas.shell(actor, aas.find(actor, decoded(aas_id)))

    @app.get("/shells/{aas_id}/submodel-refs", tags=["aas"])
    def refs(
        aas_id: str, limit: int = 100, cursor: str | None = None, actor: Access = Depends(access)
    ):
        asset = aas.find(actor, decoded(aas_id))
        values = [
            reference("Submodel", asset.aas["submodels"][k]["id"]) for k in aas.kinds(actor, asset)
        ]
        return aas.page(actor, values, f"refs:{aas_id}", limit, cursor)

    @app.get("/submodel-descriptors/{submodel_id}", tags=["aas"])
    def sub_descriptor(submodel_id: str, actor: Access = Depends(access)):
        asset, kind = aas.find(actor, decoded(submodel_id), submodel=True)
        return aas.descriptor(actor, asset, kind)

    @app.get("/submodels/{submodel_id}", tags=["aas"])
    def submodel(submodel_id: str, actor: Access = Depends(access)):
        asset, kind = aas.find(actor, decoded(submodel_id), submodel=True)
        return aas.submodel(actor, asset, kind)

    @app.get("/submodels/{submodel_id}/$value", tags=["aas"])
    def values(submodel_id: str, actor: Access = Depends(access)):
        asset, kind = aas.find(actor, decoded(submodel_id), submodel=True)
        return aas.submodel(actor, asset, kind, value_only=True)

    return app
