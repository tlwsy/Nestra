# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.11.31 AS uv

FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project \
    --extra crawl --extra web --extra notify
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    --extra crawl --extra web --extra notify \
    && find /opt/venv -type f \( -name '*.abi3.so' -o -name '*.cpython-*.so' \) \
        -exec strip --strip-unneeded {} +

# Heavy dependencies are explicit build targets; default `runtime` stays 2C2G-friendly.
FROM builder AS builder-render
RUN uv sync --frozen --no-dev --no-editable \
    --extra crawl --extra web --extra notify --extra render \
    && find /opt/venv -type f \( -name '*.abi3.so' -o -name '*.cpython-*.so' \) \
        -exec strip --strip-unneeded {} +

FROM builder AS builder-local
RUN uv sync --frozen --no-dev --no-editable \
    --extra crawl --extra web --extra notify --extra local \
    && find /opt/venv -type f \( -name '*.abi3.so' -o -name '*.cpython-*.so' \) \
        -exec strip --strip-unneeded {} +

FROM builder AS builder-full
RUN uv sync --frozen --no-dev --no-editable \
    --extra crawl --extra web --extra notify --extra render --extra local \
    && find /opt/venv -type f \( -name '*.abi3.so' -o -name '*.cpython-*.so' \) \
        -exec strip --strip-unneeded {} +

FROM python:3.12-slim-bookworm AS runtime-base
ARG NESTRA_UID=1000
ARG NESTRA_GID=1000
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NESTRA_CONFIG=/app/config/config.yaml \
    TZ=Asia/Shanghai
RUN test "${NESTRA_UID}" -gt 0 && test "${NESTRA_GID}" -gt 0 \
    || (echo "NESTRA_UID/NESTRA_GID must be non-zero" >&2; exit 1); \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --non-unique --gid "${NESTRA_GID}" nestra \
    && useradd --uid "${NESTRA_UID}" --gid nestra --home-dir /app --shell /usr/sbin/nologin nestra
COPY --from=builder /opt/venv /opt/venv
COPY --chown=nestra:nestra deploy/entrypoint.sh /usr/local/bin/nestra-entrypoint
COPY --chown=nestra:nestra scripts/*.py /app/scripts/
WORKDIR /app
RUN mkdir -p data/db data/attachments data/models/tagsets config \
    && chown -R nestra:nestra /app \
    && chmod 0700 data data/db data/attachments data/models data/models/tagsets \
    && chmod 0755 /usr/local/bin/nestra-entrypoint
USER nestra
EXPOSE 8080
ENTRYPOINT ["nestra-entrypoint"]
CMD ["nestra", "serve"]

FROM runtime-base AS runtime-render
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY --from=builder-render /opt/venv /opt/venv
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*
USER nestra

FROM runtime-base AS runtime-local
COPY --from=builder-local /opt/venv /opt/venv

FROM runtime-base AS runtime-full
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY --from=builder-full /opt/venv /opt/venv
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*
USER nestra

# Last stage is deliberately the small default for plain `docker build` too.
FROM runtime-base AS runtime
