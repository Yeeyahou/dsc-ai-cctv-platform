import json
import logging
from importlib.resources import files

import boto3
from botocore.config import Config
from pydantic import SecretStr

from dsc.backend.config import Settings
from dsc.backend.errors import Problem
from dsc.shared.observability import log_methods, logged

CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"mode": "standard", "total_max_attempts": 3},
)


def client(service: str, region: str):
    config = (
        CLIENT_CONFIG.merge(Config(signature_version="s3v4")) if service == "s3" else CLIENT_CONFIG
    )
    return boto3.client(service, region_name=region, config=config)


@logged()
def load_secrets(settings: Settings) -> Settings:
    if not settings.database_secret_arn and not settings.runtime_secret_arn:
        return settings
    secrets = client("secretsmanager", settings.aws_region)
    if settings.database_secret_arn:
        from sqlalchemy.engine import URL

        data = json.loads(
            secrets.get_secret_value(SecretId=settings.database_secret_arn)["SecretString"]
        )
        settings.database_url = SecretStr(
            URL.create(
                "postgresql+psycopg",
                username=data["username"],
                password=data["password"],
                host=data["host"],
                port=int(data.get("port", 5432)),
                database=data["dbname"],
                query={
                    "sslmode": "verify-full",
                    "sslrootcert": str(files("dsc.backend").joinpath("certs/global-bundle.pem")),
                },
            ).render_as_string(hide_password=False)
        )
    if settings.runtime_secret_arn:
        data = json.loads(
            secrets.get_secret_value(SecretId=settings.runtime_secret_arn)["SecretString"]
        )
        settings.cursor_secret = SecretStr(data["cursorSecret"])
    if len(settings.cursor_secret.get_secret_value()) < 32:
        raise ValueError("cursorSecret must contain at least 32 characters")
    return settings


@log_methods(level=logging.DEBUG)
class AwsStorage:
    def __init__(self, settings: Settings):
        self.s3 = client("s3", settings.aws_region)
        self.bucket = settings.clip_bucket

    def upload_url(self, key: str, size: int, checksum: str, seconds: int) -> tuple[str, dict]:
        headers = {"Content-Type": "video/mp4", "x-amz-checksum-sha256": checksum}
        url = self.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": "video/mp4",
                "ContentLength": size,
                "ChecksumSHA256": checksum,
            },
            ExpiresIn=seconds,
        )
        return url, headers

    def verify(self, key: str, size: int, checksum: str) -> None:
        head = self.s3.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        if (
            head["ContentLength"] != size
            or head.get("ChecksumSHA256") != checksum
            or head.get("ContentType") != "video/mp4"
        ):
            raise Problem(409, "UPLOAD_VERIFICATION_FAILED", "업로드 크기 또는 checksum이 다릅니다")

    def download_url(self, key: str, seconds: int) -> str:
        return self.s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": "attachment",
                "ResponseCacheControl": "no-store",
            },
            ExpiresIn=seconds,
        )

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)


@log_methods(level=logging.DEBUG, fields={"requestId": "request_id"})
class AwsQueue:
    def __init__(self, settings: Settings):
        self.sqs = client("sqs", settings.aws_region)

    def send(self, queue: str, request_id: str, version: int) -> None:
        self.sqs.send_message(
            QueueUrl=queue,
            MessageBody=json.dumps({"requestId": request_id, "dispatchVersion": version}),
        )

    def receive(self, queue: str) -> list[dict]:
        return self.sqs.receive_message(
            QueueUrl=queue, MaxNumberOfMessages=1, WaitTimeSeconds=0, VisibilityTimeout=300
        ).get("Messages", [])

    def extend(self, queue: str, receipt: str, seconds: int) -> None:
        self.sqs.change_message_visibility(
            QueueUrl=queue, ReceiptHandle=receipt, VisibilityTimeout=seconds
        )

    def delete(self, queue: str, receipt: str) -> None:
        self.sqs.delete_message(QueueUrl=queue, ReceiptHandle=receipt)
