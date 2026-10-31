# syntax=docker/dockerfile:1
# SPIKE image. Build from the repo root:
#   docker build -f scripts/container-tests/image/Dockerfile .
ARG PYTHON=3.13

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:${PYTHON}-alpine AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY ble_service/pyproject.toml ble_service/pyproject.toml
COPY ble_service/src ble_service/src
COPY src src
RUN apk add --no-cache build-base libffi-dev \
 && uv sync --frozen --no-dev --no-editable --package mcapp \
      --no-install-package timezonefinder --no-install-package timezonefinder-data \
      --no-install-package numpy --no-install-package flatbuffers

FROM alpine:3 AS webapp
ARG WEBAPP_TARBALL=scripts/container-tests/image/.cache/mcapp-v2.1.22.tar.gz
COPY ${WEBAPP_TARBALL} /tmp/release.tar.gz
RUN mkdir /x && tar -xzf /tmp/release.tar.gz -C /x && mv /x/*/webapp /webapp

FROM python:${PYTHON}-alpine
RUN apk add --no-cache tzdata \
 && addgroup -g 10001 mcapp \
 && adduser -D -H -u 10001 -G mcapp -s /sbin/nologin mcapp \
 && install -d -o mcapp -g mcapp /var/lib/mcapp
COPY --from=build /opt/venv /opt/venv
COPY --from=webapp /webapp /app/webapp
COPY scripts/container-tests/image/launcher.py /app/launcher.py
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MCAPP_BIND_HOST=0.0.0.0 \
    MCAPP_WEBAPP_DIR=/app/webapp \
    MCAPP_BLE_MODE=disabled \
    MCAPP_SECRET_KEY_PATH=/var/lib/mcapp/secret.key \
    MESHCOM_VAPID_PATH=/var/lib/mcapp/vapid.json \
    MCAPP_RUNTIME_STATE=/var/lib/mcapp/runtime.json \
    TZ=UTC
USER 10001:10001
VOLUME /var/lib/mcapp
EXPOSE 2981 1799/udp
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=5s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1:2981/health >/dev/null || exit 1
ENTRYPOINT ["python", "/app/launcher.py"]
