# syntax=docker/dockerfile:1

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    GLM_USAGE_HOST=0.0.0.0 \
    GLM_USAGE_PORT=8000

WORKDIR /app

# pyproject declares the dependencies (sanic/httpx/orjson/uvloop) and the
# `glm-usage` console script, so installing the project is all that is needed.
COPY pyproject.toml README.md ./
COPY glm_usage/ ./glm_usage/
RUN pip install .

RUN groupadd --gid 10001 glm \
    && useradd --uid 10001 --gid glm --create-home --shell /usr/sbin/nologin glm
USER glm

EXPOSE 8000

# /healthz does not touch the upstream: this is a liveness probe, not a
# "is bigmodel reachable" probe.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["glm-usage"]
