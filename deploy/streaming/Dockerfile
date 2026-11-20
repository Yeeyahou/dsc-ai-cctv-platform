FROM bluenviron/mediamtx:1.21.4@sha256:c730bccdbc8739f8f0c13cefe9bea8b61926f599b88fa4d0d229baa91846c7bc AS media
FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements-dev.lock requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.lock
COPY src ./src
RUN pip wheel --no-build-isolation --no-deps --wheel-dir /wheels .

FROM python:3.12-slim
RUN groupadd --gid 10001 dsc-live && useradd --uid 10001 --gid 10001 --no-create-home dsc-live \
    && mkdir -p /var/lib/dsc/live && chown dsc-live:dsc-live /var/lib/dsc/live
COPY --from=media /mediamtx /usr/local/bin/mediamtx
COPY requirements.lock /opt/dsc/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/dsc/requirements.lock
COPY --from=builder /wheels /wheels
RUN pip install --no-deps /wheels/*.whl && rm -r /wheels
USER dsc-live
CMD ["dsc-live-gateway", "--env-file", "/etc/dsc/live.env"]
