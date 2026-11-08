"""Application logging advice. Arguments, return values and exception text stay private."""

from __future__ import annotations

import inspect
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from datetime import UTC, datetime
from functools import wraps
from typing import Any
from uuid import UUID, uuid4

_context: ContextVar[dict[str, Any] | None] = ContextVar("dsc_log_context", default=None)
_lock = threading.Lock()
_handler: logging.Handler | None = None
_component = "dsc"
_identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}\Z")
_fields = frozenset(
    {
        "traceId",
        "spanId",
        "parentSpanId",
        "awsRequestId",
        "backendTraceId",
        "principal",
        "siteId",
        "agentId",
        "assetId",
        "eventId",
        "requestId",
        "clipId",
        "outboxId",
        "operation",
        "errorType",
        "errorCode",
        "state",
        "nasStatus",
        "method",
        "route",
        "status",
        "durationMs",
        "retryMs",
        "attempt",
        "acceptedCount",
        "outboxPendingCount",
        "oldestPendingAt",
        "diskFreeBytes",
        "receivedAt",
        "jobsChecked",
        "objectsDeleted",
    }
)
_argument_fields = {
    "eventId": "event_id",
    "requestId": "job_id",
    "clipId": "clip_id",
    "assetId": "asset_id",
    "outboxId": "item_id",
}


def _safe_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in values.items():
        if key not in _fields or value is None:
            continue
        if isinstance(value, UUID):
            value = str(value)
        if type(value) in (int, float, bool):
            if not isinstance(value, float) or math.isfinite(value):
                result[key] = value
        elif isinstance(value, str) and (
            _identifier.fullmatch(value)
            or (
                key == "route" and len(value) <= 256 and re.fullmatch(r"[/A-Za-z0-9_{}$.-]+", value)
            )
        ):
            result[key] = value
    return result


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "component": _component,
            "logger": record.name,
            "event": record.getMessage(),
            **_safe_fields(getattr(record, "fields", {})),
        }
        # Exception text can contain SQL parameters, credentials, URLs or local paths.
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def configure_logging(component: str, level: str | None = None) -> None:
    """Own only the dsc logger; leave the hosting server/Lambda handlers intact."""
    global _handler, _component
    with _lock:
        _component = component
        logger = logging.getLogger("dsc")
        if level is not None or _handler is None:
            logger.setLevel((level or "INFO").upper())
        logger.propagate = False
        if _handler is None:
            _handler = logging.StreamHandler()
            _handler.setFormatter(JsonFormatter())
            logger.addHandler(_handler)
        for name in ("httpx", "httpcore", "boto3", "botocore", "urllib3", "sqlalchemy.engine"):
            logging.getLogger(name).setLevel(logging.WARNING)


@contextmanager
def log_context(*, inherit: bool = True, **fields: Any) -> Iterator[None]:
    values = {**((_context.get() or {}) if inherit else {}), **_safe_fields(fields)}
    token = _context.set(values)
    try:
        yield
    finally:
        _context.reset(token)


def current_trace_id() -> str | None:
    return (_context.get() or {}).get("traceId")


def emit_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    try:
        logging.getLogger("dsc.operations").log(
            level, event, extra={"fields": _safe_fields({**(_context.get() or {}), **fields})}
        )
    except Exception:
        # A broken log sink must not change ACKs, transactions or retries.
        pass


def _exception_fields(exc: BaseException) -> dict[str, Any]:
    values: dict[str, Any] = {"errorType": type(exc).__name__}
    for field, attr in (("errorCode", "code"), ("status", "status_code"), ("status", "status")):
        value = getattr(exc, attr, None)
        if value is not None:
            values[field] = value
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        values["errorCode"] = response.get("Error", {}).get("Code")
        values["status"] = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    elif response is not None:
        values["status"] = getattr(response, "status_code", None)
    return _safe_fields(values)


def _bound_fields(signature: inspect.Signature, args, kwargs, paths) -> dict[str, Any]:
    bound = signature.bind_partial(*args, **kwargs).arguments
    values = {}
    for field, path in paths.items():
        try:
            parts = path.split(".")
            value = bound[parts[0]]
            for part in parts[1:]:
                value = value[part] if isinstance(value, dict) else getattr(value, part)
            values[field] = value
        except Exception:
            # Optional identifiers must not make an otherwise valid operation fail.
            continue
    return _safe_fields(values)


@contextmanager
def _execution(name: str, level: int, fields: dict) -> Iterator[None]:
    parent = _context.get() or {}
    with log_context(
        **{
            **fields,
            "traceId": parent.get("traceId") or str(uuid4()),
            "spanId": uuid4().hex,
            "parentSpanId": parent.get("spanId"),
            "operation": name,
        }
    ):
        started = time.perf_counter()
        emit_event("operation_started", level=level)
        try:
            yield
        except BaseException as exc:
            try:
                error = _exception_fields(exc)
            except Exception:
                error = {"errorType": type(exc).__name__}
            cancelled = not isinstance(exc, Exception)
            severity = (
                logging.WARNING
                if cancelled or 400 <= error.get("status", 0) < 500
                else logging.ERROR
            )
            emit_event(
                "operation_cancelled" if cancelled else "operation_failed",
                level=severity,
                durationMs=round((time.perf_counter() - started) * 1000, 3),
                **error,
            )
            raise
        else:
            emit_event(
                "operation_succeeded",
                level=level,
                durationMs=round((time.perf_counter() - started) * 1000, 3),
            )


def logged(
    function=None,
    *,
    level: int = logging.INFO,
    fields: Mapping[str, str] | None = None,
    name: str | None = None,
):
    """Around advice for sync, async and generator functions, with explicit safe metadata."""

    def decorate(func):
        if getattr(func, "__dsc_logged__", False):
            return func
        signature = inspect.signature(func, eval_str=True)
        paths = {**_argument_fields, **(fields or {})}
        operation = name or f"{func.__module__}.{func.__qualname__.replace('.<locals>.', '.')}"

        def execution(args, kwargs):
            return _execution(operation, level, _bound_fields(signature, args, kwargs, paths))

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def wrapper(*args, **kwargs):
                with execution(args, kwargs):
                    return await func(*args, **kwargs)
        elif inspect.isgeneratorfunction(func):

            @wraps(func)
            def wrapper(*args, **kwargs):
                with execution(args, kwargs):
                    return (yield from func(*args, **kwargs))
        else:

            @wraps(func)
            def wrapper(*args, **kwargs):
                with execution(args, kwargs):
                    return func(*args, **kwargs)

        wrapper.__signature__ = signature
        wrapper.__dsc_logged__ = True
        return wrapper

    return decorate(function) if function is not None else decorate


def log_methods(
    *,
    level: int = logging.INFO,
    fields: Mapping[str, str] | None = None,
    include: tuple[str, ...] = (),
):
    """Pointcut: a class's public methods, plus explicitly named internal boundaries."""

    def decorate(cls):
        for name, value in vars(cls).copy().items():
            if name.startswith("_") and name not in include:
                continue
            if isinstance(value, (staticmethod, classmethod)):
                wrapped = logged(value.__func__, level=level, fields=fields)
                setattr(cls, name, type(value)(wrapped))
            elif inspect.isfunction(value):
                setattr(cls, name, logged(value, level=level, fields=fields))
        return cls

    return decorate


def contextual(function: Callable, /, *args, **kwargs) -> Callable[[], Any]:
    """Capture before submitting work; ContextVars do not cross raw thread boundaries."""
    context = copy_context()
    return lambda: context.run(function, *args, **kwargs)
