from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from dsc.backend.aws import AwsStorage
from dsc.backend.config import Settings
from dsc.backend.errors import Problem


@pytest.fixture
def storage(monkeypatch):
    s3 = boto3.client(
        "s3",
        region_name="ap-northeast-2",
        aws_access_key_id="test-only",
        aws_secret_access_key="test-only",
        endpoint_url="https://storage.test",
        config=Config(signature_version="s3v4"),
    )
    monkeypatch.setattr("dsc.backend.aws.client", lambda *_: s3)
    return AwsStorage(Settings(environment="test", clip_bucket="private-clips"))


def test_put_signs_length_type_and_checksum(storage):
    url, headers = storage.upload_url("clips/request/1/test.mp4", 4, "a" * 44, 900)
    signed = parse_qs(urlparse(url).query)["X-Amz-SignedHeaders"][0].split(";")
    assert {"content-length", "content-type", "x-amz-checksum-sha256"}.issubset(signed)
    assert "Authorization" not in headers


def test_presigned_clip_urls_use_the_bucket_region(monkeypatch):
    session = boto3.Session(aws_access_key_id="test-only", aws_secret_access_key="test-only")
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", session)
    storage = AwsStorage(Settings(environment="test", clip_bucket="private-clips"))
    put, _ = storage.upload_url("clips/request/1/test.mp4", 4, "a" * 44, 900)
    get = storage.download_url("clips/request/1/test.mp4", 300)
    assert urlparse(put).hostname == "private-clips.s3.ap-northeast-2.amazonaws.com"
    assert urlparse(get).hostname == "private-clips.s3.ap-northeast-2.amazonaws.com"


@pytest.mark.parametrize(
    "checksum,valid", [("expected", True), ("different", False), (None, False)]
)
def test_head_verification_requires_sha256_not_etag(storage, checksum, valid):
    response = {"ContentLength": 4, "ContentType": "video/mp4", "ETag": '"expected"'}
    if checksum is not None:
        response["ChecksumSHA256"] = checksum
    with Stubber(storage.s3) as stubber:
        stubber.add_response(
            "head_object",
            response,
            {"Bucket": "private-clips", "Key": "clips/x", "ChecksumMode": "ENABLED"},
        )
        if valid:
            storage.verify("clips/x", 4, "expected")
        else:
            with pytest.raises(Problem):
                storage.verify("clips/x", 4, "expected")
        stubber.assert_no_pending_responses()
