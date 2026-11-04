# Fixed inputs: never build on an older Gateway image.
FROM python@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2 AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 SOURCE_DATE_EPOCH=1793793600
WORKDIR /src
COPY locks /locks
RUN python -m pip install --no-cache-dir --require-hashes -r /locks/build.txt
RUN python -m pip download --no-cache-dir --no-deps --no-build-isolation \
      --only-binary=:all: --no-binary=http-ece,pyaes --require-hashes \
      -r /locks/runtime.txt -d /inputs
COPY release/build-wheels.py /build-wheels.py
RUN python /build-wheels.py dependencies
COPY pyproject.toml README.md MANIFEST.in ./
COPY gateway*.py ./
COPY gateway_mcp ./gateway_mcp
COPY release/fetch-sdk.py /fetch-sdk.py
COPY docs/bitrix-sdk-provenance.json /src/release-sdk-provenance.json
RUN python /fetch-sdk.py
COPY gateway-tools.json gateway-policy.json gateway-company-indexes.json gateway-factory-projects.json gateway-platforma-tools.json ./
COPY migrations ./migrations
RUN python /build-wheels.py application

FROM python@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GATEWAY_TRANSPORT=streamable-http GATEWAY_HOST=0.0.0.0 GATEWAY_PORT=8000 \
    GATEWAY_AUTH_ENABLED=true GATEWAY_LOGIN_PROVIDER=bitrix24
COPY locks/debian.sources /etc/apt/sources.list.d/debian.sources
COPY locks/os.txt /tmp/gateway-os.txt
COPY locks/apt-build.conf /etc/apt/apt.conf.d/80gateway-build
RUN echo 'gateway-build: Debian indexes (240s limit)' \
    && timeout --signal=TERM --kill-after=5s 240s apt-get update \
    && echo 'gateway-build: pinned Debian packages (360s limit)' \
    && DEBIAN_FRONTEND=noninteractive timeout --signal=TERM --kill-after=5s 360s \
       xargs -r apt-get install -y --no-install-recommends < /tmp/gateway-os.txt \
    && echo 'gateway-build: Debian packages installed' \
    && rm -rf /var/lib/apt/lists/* /tmp/gateway-os.txt /etc/apt/apt.conf.d/80gateway-build
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --no-deps --require-hashes -r /wheels/requirements.txt \
    && python -m pip check \
    && install -d /usr/local/share/gateway-release \
    && cp /wheels/manifest.json /usr/local/share/gateway-release/wheels.json \
    && rm -rf /wheels
COPY release/baseline.json /usr/local/share/gateway-release/baseline.json
COPY release/verify-image.py /usr/local/share/gateway-release/verify-image.py
WORKDIR /app
COPY gateway-tools.json gateway-policy.json gateway-company-indexes.json gateway-factory-projects.json gateway-platforma-tools.json ./
COPY ops/gateway-entrypoint.sh /usr/local/bin/gateway-private-entrypoint
COPY ops/fix-image-permissions.py /tmp/gateway-fix-permissions.py
RUN groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /home/gateway --shell /usr/sbin/nologin gateway \
    && install -d -o 0 -g 10001 -m 0750 /home/gateway \
    && chown 0:10001 /usr/local/bin/gateway-private-entrypoint \
    && chmod 0750 /usr/local/bin/gateway-private-entrypoint \
    && python /tmp/gateway-fix-permissions.py \
    && rm /tmp/gateway-fix-permissions.py
USER 10001:10001
RUN python /usr/local/share/gateway-release/verify-image.py
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/gateway-private-entrypoint"]
CMD ["gateway-mcp", "--transport", "streamable-http"]
