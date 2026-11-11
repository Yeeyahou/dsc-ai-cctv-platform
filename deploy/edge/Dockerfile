FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements-dev.lock requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.lock
COPY src ./src
RUN pip wheel --no-build-isolation --no-deps --wheel-dir /wheels .

FROM python:3.12-slim
ARG DSC_UID=10001
ARG DSC_GID=10001
RUN groupadd --gid ${DSC_GID} dsc-edge \
    && useradd --uid ${DSC_UID} --gid ${DSC_GID} --no-create-home dsc-edge \
    && mkdir -p /var/lib/dsc/office /mnt/dsc-archive /etc/dsc \
    && chown -R dsc-edge:dsc-edge /var/lib/dsc
COPY requirements.lock /opt/dsc/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/dsc/requirements.lock
COPY --from=builder /wheels /wheels
RUN pip install --no-deps /wheels/*.whl && rm -r /wheels
USER dsc-edge
CMD ["dsc-edge-server", "--env-file", "/etc/dsc/office.env"]
