import asyncio
import inspect
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError

from dsc.backend.errors import Problem
from dsc.shared.observability import (
    JsonFormatter,
    configure_logging,
    contextual,
    emit_event,
    log_context,
    logged,
)
from tests.conftest import headers


def test_advice_preserves_signature_return_and_nested_spans(operation_logs):
    class PrivateValue:
        def __repr__(self):
            raise AssertionError("arguments and results must not be inspected")

    @logged
    def child(secret, *, event_id: str):
        """An operation with a keyword-only contract."""
        return secret

    @logged
    def parent(secret):
        return child(secret, event_id="event-1")

    original = inspect.signature(child.__wrapped__)
    assert inspect.signature(child) == original
    assert child.__name__ == "child" and child.__doc__ == child.__wrapped__.__doc__
    value = PrivateValue()
    with log_context(traceId="trace-1"):
        assert parent(value) is value
    start, nested_start, nested_end, end = operation_logs
    assert [entry["event"] for entry in operation_logs] == [
        "operation_started",
        "operation_started",
        "operation_succeeded",
        "operation_succeeded",
    ]
    assert nested_start["parentSpanId"] == start["spanId"]
    assert nested_end["spanId"] == nested_start["spanId"]
    assert nested_end["eventId"] == "event-1"
    assert end["spanId"] == start["spanId"]
    assert all(entry["traceId"] == "trace-1" for entry in operation_logs)
    assert end["durationMs"] >= nested_end["durationMs"] >= 0
    emit_event("outside")
    assert "traceId" not in operation_logs[-1]


def test_original_exception_is_preserved_without_message_or_arguments(operation_logs):
    secret = "password=do-not-log https://s3.test/clip?signature=private"
    error = Problem(409, "EVENT_ID_CONFLICT", secret)

    @logged
    def operation(body):
        raise error

    with pytest.raises(Problem) as caught:
        operation({"token": secret})
    assert caught.value is error
    failed = operation_logs[-1]
    assert failed["event"] == "operation_failed"
    assert failed["errorType"] == "Problem" and failed["errorCode"] == "EVENT_ID_CONFLICT"
    assert failed["level"] == "WARNING" and failed["status"] == 409
    assert secret not in json.dumps(operation_logs)


def test_async_concurrency_and_cancellation_do_not_share_context(operation_logs):
    @logged
    async def task(value):
        await asyncio.sleep(0)
        emit_event("task_running")
        return value

    async def run_one(value):
        with log_context(inherit=False, traceId=value):
            return await task(value)

    async def run():
        assert await asyncio.gather(run_one("first"), run_one("second")) == ["first", "second"]
        with log_context(traceId="cancelled"):
            future = asyncio.create_task(task("unused"))
            await asyncio.sleep(0)
            future.cancel()
            with pytest.raises(asyncio.CancelledError):
                await future

    asyncio.run(run())
    for trace_id in ("first", "second"):
        entries = [entry for entry in operation_logs if entry["traceId"] == trace_id]
        assert len(entries) == 3
        assert len({entry["spanId"] for entry in entries}) == 1
    cancelled = [entry for entry in operation_logs if entry["traceId"] == "cancelled"]
    assert cancelled[-1]["event"] == "operation_cancelled"
    assert not any(entry["event"] == "operation_succeeded" for entry in cancelled)


def test_generator_advice_covers_actual_resource_lifetime(operation_logs):
    cleaned = []

    @contextmanager
    @logged
    def resource():
        try:
            yield "open"
        finally:
            cleaned.append(True)

    manager = resource()
    assert operation_logs == []
    with pytest.raises(ValueError), manager as value:
        assert value == "open" and len(operation_logs) == 1
        raise ValueError("private-file.mp4")
    assert cleaned == [True]
    assert operation_logs[-1]["event"] == "operation_failed"
    assert "private-file.mp4" not in json.dumps(operation_logs)


def test_thread_context_is_copied_and_reset_when_executor_reuses_thread(operation_logs):
    @logged
    def work():
        emit_event("thread_work")

    with ThreadPoolExecutor(max_workers=1) as workers:
        with log_context(traceId="worker-trace", requestId="job-1", eventId="event-1"):
            workers.submit(contextual(work)).result()
        workers.submit(emit_event, "outside_worker").result()
    assert all(entry["requestId"] == "job-1" for entry in operation_logs[:-1])
    assert all(entry["traceId"] == "worker-trace" for entry in operation_logs[:-1])
    assert "requestId" not in operation_logs[-1] and "traceId" not in operation_logs[-1]


def test_broken_log_sink_does_not_change_success_or_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("log sink unavailable")

    monkeypatch.setattr(logging.getLogger("dsc.operations"), "log", fail)

    @logged
    def success():
        return 201

    error = ValueError("original error")

    @logged
    def failure():
        raise error

    assert success() == 201
    with pytest.raises(ValueError) as caught:
        failure()
    assert caught.value is error


def test_json_log_fields_are_allowlisted_and_not_serialized_by_repr():
    record = logging.LogRecord(
        "dsc.operations", logging.ERROR, __file__, 1, "test_failed", (), None
    )
    record.fields = {
        "eventId": "event-1\nforged-line",
        "requestId": "https://s3.test/?secret=private",
        "clipId": "/nas/private.mp4",
        "token": "bearer-private",
        "body": {"private": True},
        "route": "/events/{event_id}",
        "durationMs": float("nan"),
        "status": 503,
    }
    payload = json.loads(JsonFormatter().format(record))
    assert payload["route"] == "/events/{event_id}" and payload["status"] == 503
    assert not {"eventId", "requestId", "clipId", "token", "body", "durationMs"} & payload.keys()


def test_configuration_is_idempotent_and_keeps_warm_invocation_level():
    root_handlers = logging.getLogger().handlers[:]
    configure_logging("backend", "DEBUG")
    logger = logging.getLogger("dsc")
    original_handlers = logger.handlers[:]
    configure_logging("backend")
    assert logger.level == logging.DEBUG
    assert logger.handlers == original_handlers
    assert logging.getLogger().handlers == root_handlers
    assert all(logging.getLogger(name).level >= logging.WARNING for name in ("botocore", "httpx"))
    configure_logging("backend", "INFO")


def test_lambda_invocations_have_separate_context_without_event_dump(monkeypatch, operation_logs):
    from dsc.backend import lambda_handler

    monkeypatch.setattr(
        lambda_handler, "adapter", lambda: lambda event, context: {"statusCode": 200}
    )
    for value in ("aws-invocation-1", "aws-invocation-2"):
        assert lambda_handler.handler(
            {"authorization": "private-access-token"}, SimpleNamespace(aws_request_id=value)
        ) == {"statusCode": 200}
    assert {entry["awsRequestId"] for entry in operation_logs} == {
        "aws-invocation-1",
        "aws-invocation-2",
    }
    assert len({entry["traceId"] for entry in operation_logs}) == 2
    assert "private-access-token" not in json.dumps(operation_logs)
    emit_event("after_invocations")
    assert "awsRequestId" not in operation_logs[-1]


def test_maintenance_has_invocation_context_and_result_counts(system, monkeypatch, operation_logs):
    from dsc.backend import runtime

    monkeypatch.setattr(runtime, "platform", lambda: system.platform)
    assert runtime.maintenance_handler({}, SimpleNamespace(aws_request_id="aws-maintenance")) == {
        "jobsChecked": 0,
        "objectsDeleted": 0,
    }
    assert all(entry["awsRequestId"] == "aws-maintenance" for entry in operation_logs)
    result = [entry for entry in operation_logs if entry["event"] == "maintenance_completed"][0]
    assert result["jobsChecked"] == 0 and result["objectsDeleted"] == 0


def test_api_logs_share_trace_and_avoid_payloads(system, event_body, operation_logs):
    response = system.client.post("/ingest", json=event_body, headers=headers("edge"))
    assert response.status_code == 201
    trace_id = response.headers["X-Request-Id"]
    assert all(entry["traceId"] == trace_id for entry in operation_logs)
    ingests = [
        entry for entry in operation_logs if entry.get("operation", "").endswith("Platform.ingest")
    ]
    assert len(ingests) == 2
    assert all(entry["eventId"] == event_body["eventId"] for entry in ingests)
    assert all(entry["principal"] == "client:edge" for entry in ingests)
    assert operation_logs[-1]["status"] == 201
    operation_logs.clear()
    changed = {**event_body, "occurredAt": "2026-11-07T10:00:01.000Z"}
    conflict = system.client.post("/ingest", json=changed, headers=headers("edge"))
    assert conflict.status_code == 409
    assert conflict.headers["X-Request-Id"] != trace_id
    assert any(entry.get("errorCode") == "EVENT_ID_CONFLICT" for entry in operation_logs)
    assert "Authorization" not in json.dumps(operation_logs)
    assert "occurredAt" not in json.dumps(operation_logs)


def test_http_uses_trusted_invocation_context_and_ignores_incoming_trace_header(
    system, operation_logs
):
    with log_context(traceId="trusted-lambda-trace", awsRequestId="aws-1"):
        response = system.client.get(
            "/description", headers={**headers(), "X-Request-Id": "untrusted-caller-value"}
        )
    assert response.headers["X-Request-Id"] == "trusted-lambda-trace"
    assert all(entry["traceId"] == "trusted-lambda-trace" for entry in operation_logs)
    assert all(entry["awsRequestId"] == "aws-1" for entry in operation_logs)
    assert "untrusted-caller-value" not in json.dumps(operation_logs)


def test_rejected_requests_and_oversized_body_are_also_logged(system, operation_logs):
    response = system.client.post("/ingest", content=b"x" * (256 * 1024 + 1))
    assert response.status_code == 413
    assert response.json()["error"]["traceId"] == response.headers["X-Request-Id"]
    assert operation_logs[-1]["status"] == 413
    response = system.client.post(
        "/ingest",
        content="private-broken-json",
        headers={**headers("edge"), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert any(entry.get("errorCode") == "INVALID_JSON" for entry in operation_logs)
    response = system.client.get("/description", headers=headers("invalid-private-token"))
    assert response.status_code == 401
    system.client.get("/private-file.mp4?password=query-secret")
    assert operation_logs[-1]["route"] == "unmatched"
    text = json.dumps(operation_logs)
    assert all(
        value not in text
        for value in (
            "private-broken-json",
            "invalid-private-token",
            "query-secret",
            "private-file.mp4",
        )
    )


def test_new_sync_async_routes_and_unhandled_errors_are_automatically_logged(
    system, operation_logs
):
    app = system.client.app

    @app.get("/test/async/{event_id}")
    async def added(event_id: str, request: Request):
        await asyncio.sleep(0)
        return {"eventId": event_id, "traceId": request.state.trace_id}

    @app.get("/test/fail")
    def failure():
        raise ValueError("private exception with https://s3.test/?signature=private")

    response = system.client.get("/test/async/event-1")
    assert response.status_code == 200
    assert response.json()["traceId"] == response.headers["X-Request-Id"]
    assert any(entry.get("eventId") == "event-1" for entry in operation_logs)
    assert any(entry.get("operation", "").endswith(".added") for entry in operation_logs)
    operation_logs.clear()
    response = system.client.get("/test/fail")
    assert response.status_code == 500
    assert any(entry["event"] == "operation_failed" for entry in operation_logs)
    assert operation_logs[-1]["errorType"] == "ValueError" and operation_logs[-1]["status"] == 500
    assert "signature=private" not in json.dumps(operation_logs)


def test_database_failure_has_safe_structured_logs(system, event_body, operation_logs, monkeypatch):
    def fail(*args):
        raise SQLAlchemyError("postgresql://user:password@database sql-parameters-private")

    monkeypatch.setattr(system.platform, "ingest", fail)
    response = system.client.post("/ingest", json=event_body, headers=headers("edge"))
    assert response.status_code == 503
    assert any(entry.get("errorCode") == "STORAGE_UNAVAILABLE" for entry in operation_logs)
    assert any(entry.get("errorType") == "SQLAlchemyError" for entry in operation_logs)
    assert "sql-parameters-private" not in json.dumps(operation_logs)
