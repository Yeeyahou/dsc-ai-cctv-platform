"""HTTP join points shared by every registered endpoint, including future routes."""

import logging
import time
from collections.abc import Callable
from uuid import uuid4

from fastapi.routing import APIRoute
from starlette.datastructures import MutableHeaders

from dsc.shared.observability import current_trace_id, emit_event, log_context, logged


class LoggedRoute(APIRoute):
    def __init__(self, path: str, endpoint: Callable, **kwargs):
        super().__init__(
            path,
            logged(
                endpoint,
                fields={
                    "requestId": "request_id",
                    "principal": "actor.principal.id",
                    "edgeServerId": "actor.edge_server.id",
                    "siteId": "actor.edge_server.site_id",
                },
            ),
            **kwargs,
        )


class RequestLogging:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        trace_id = current_trace_id() or str(uuid4())
        state = scope.setdefault("state", {})
        state["trace_id"] = trace_id
        status = 500
        started = time.perf_counter()
        error_type = None

        async def traced_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-Id"] = trace_id
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        with log_context(traceId=trace_id):
            try:
                await self.app(scope, receive, traced_send)
            except BaseException as exc:
                error_type = type(exc).__name__
                status = 500 if isinstance(exc, Exception) else 499
                raise
            finally:
                route = scope.get("route")
                params = scope.get("path_params", {})
                emit_event(
                    "http_request_completed",
                    level=logging.ERROR
                    if status >= 500
                    else logging.WARNING
                    if status >= 400
                    else logging.INFO,
                    method=scope["method"],
                    route=getattr(route, "path", "unmatched"),
                    status=status,
                    durationMs=round((time.perf_counter() - started) * 1000, 3),
                    errorType=error_type,
                    principal=state.get("principal"),
                    siteId=state.get("site_id"),
                    eventId=state.get("event_id") or params.get("event_id"),
                    requestId=params.get("request_id"),
                )
