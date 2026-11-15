import json

import httpx
import pytest
from pydantic import ValidationError

from dsc.devtools.aas_consumer import Consumer, ConsumerSettings


def configuration(tmp_path):
    token = tmp_path / "token"
    token.write_text("partner")
    token.chmod(0o600)
    return ConsumerSettings(
        base_url="https://api.test",
        aas_id="urn:dsc:aas:camera-1",
        global_asset_id="urn:dsc:asset:camera-1",
        submodel_ids=[
            "urn:dsc:submodel:camera-1:Nameplate",
            "urn:dsc:submodel:camera-1:TechnicalData",
        ],
        forbidden_aas_id="urn:dsc:aas:other-camera",
        forbidden_submodel_id="urn:dsc:submodel:camera-1:TimeSeries",
        access_token_file=token,
    )


def test_consumer_exercises_backend_registry_discovery_and_permissions(system, tmp_path):
    def handle(request):
        response = system.client.get(str(request.url), headers=request.headers)
        return httpx.Response(response.status_code, content=response.content)

    consumer = Consumer(configuration(tmp_path), httpx.MockTransport(handle))
    try:
        report = consumer.verify()
        assert report["status"] == "PASS"
        assert sum(item["status"] == 404 for item in report["requests"]) == 6
        assert report["submodelsChecked"] == 2
        assert "partner" not in json.dumps(report)
    finally:
        consumer.close()


def test_consumer_does_not_follow_redirect_or_print_body(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            302, headers={"Location": "https://evil.test/token"}, text="secret diagnostic data"
        )

    consumer = Consumer(configuration(tmp_path), httpx.MockTransport(handle))
    try:
        with pytest.raises(ValueError, match="HTTP 302") as exc:
            consumer.verify()
        assert "secret" not in str(exc.value)
        assert len(calls) == 1
        assert calls[0].url.host == "api.test"
    finally:
        consumer.close()


def test_consumer_bounds_pagination_and_response_size(tmp_path):
    consumer = Consumer(
        configuration(tmp_path),
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"result": [], "paging_metadata": {"cursor": "same"}}
            )
        ),
    )
    try:
        with pytest.raises(ValueError, match="repeated"):
            consumer.pages("/shell-descriptors")
    finally:
        consumer.close()
    consumer = Consumer(
        configuration(tmp_path),
        httpx.MockTransport(lambda _: httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1))),
    )
    try:
        with pytest.raises(ValueError, match="2 MiB"):
            consumer.get("/description")
    finally:
        consumer.close()


def test_credentials_are_private_and_http_is_loopback_opt_in(tmp_path):
    cfg = configuration(tmp_path)
    cfg.access_token_file.chmod(0o644)
    consumer = Consumer(cfg)
    try:
        with pytest.raises(PermissionError):
            consumer.authenticate()
    finally:
        consumer.close()
    with pytest.raises(ValidationError):
        ConsumerSettings.model_validate({**cfg.model_dump(), "base_url": "http://api.test"})
    with pytest.raises(ValidationError):
        ConsumerSettings.model_validate(
            {**cfg.model_dump(), "base_url": "http://evil.test", "allow_local_http": True}
        )


def test_client_credentials_use_only_configured_token_endpoint(tmp_path):
    cfg = configuration(tmp_path)
    secret = tmp_path / "secret"
    secret.write_text("test-only-secret")
    secret.chmod(0o600)
    cfg = ConsumerSettings.model_validate(
        {
            **cfg.model_dump(),
            "access_token_file": None,
            "token_url": "https://auth.test/token",
            "client_id": "partner",
            "client_secret_file": secret,
        }
    )
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "test-only-token"})

    consumer = Consumer(cfg, httpx.MockTransport(handle))
    try:
        consumer.authenticate()
        assert consumer.token == "test-only-token"
        assert requests[0].url == "https://auth.test/token"
        assert b"grant_type=client_credentials" in requests[0].content
        assert requests[0].headers["Authorization"].startswith("Basic ")
    finally:
        consumer.close()
