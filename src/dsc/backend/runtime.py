from functools import lru_cache

from dsc.backend.aws import AwsQueue, AwsStorage, load_secrets
from dsc.backend.config import Settings
from dsc.backend.db import database
from dsc.backend.service import Platform


@lru_cache
def platform() -> Platform:
    settings = load_secrets(Settings())
    _, sessions = database(settings.database_url.get_secret_value())
    return Platform(sessions, settings, AwsStorage(settings), AwsQueue(settings))


def maintenance_handler(event, context) -> dict:
    return platform().maintain()
