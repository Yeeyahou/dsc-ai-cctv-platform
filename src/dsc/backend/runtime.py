from functools import lru_cache
from uuid import uuid4

from dsc.backend.aws import AwsQueue, AwsStorage, load_secrets
from dsc.backend.config import Settings
from dsc.backend.db import database
from dsc.backend.service import Platform
from dsc.shared.observability import configure_logging, log_context, logged


@lru_cache
@logged()
def platform() -> Platform:
    settings = load_secrets(Settings())
    _, sessions = database(settings.database_url.get_secret_value())
    return Platform(sessions, settings, AwsStorage(settings), AwsQueue(settings))


def maintenance_handler(event, context) -> dict:
    configure_logging("backend-maintenance")
    with log_context(
        inherit=False, traceId=str(uuid4()), awsRequestId=getattr(context, "aws_request_id", None)
    ):
        service = platform()
        configure_logging("backend-maintenance", service.settings.log_level)
        return service.maintain()
