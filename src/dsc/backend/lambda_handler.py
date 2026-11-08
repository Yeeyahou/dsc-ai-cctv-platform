import logging
from functools import lru_cache

from mangum import Mangum

from dsc.backend.api import create_app


@lru_cache
def adapter():
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return Mangum(create_app(), lifespan="off")


def handler(event, context):
    return adapter()(event, context)
