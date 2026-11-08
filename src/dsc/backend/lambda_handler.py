from functools import lru_cache
from uuid import uuid4

from mangum import Mangum

from dsc.backend.api import create_app
from dsc.shared.observability import configure_logging, log_context, logged


@lru_cache
@logged()
def adapter():
    return Mangum(create_app(), lifespan="off")


def handler(event, context):
    configure_logging("backend")
    with log_context(
        inherit=False, traceId=str(uuid4()), awsRequestId=getattr(context, "aws_request_id", None)
    ):
        return invoke(event, context)


@logged()
def invoke(event, context):
    return adapter()(event, context)
