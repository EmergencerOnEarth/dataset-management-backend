# Default: DaoCloud mirror (docker.io often times out from CN networks).
# Official image: docker build --build-arg PY_IMAGE=python:3.11-slim-bookworm .
ARG PY_IMAGE=docker.m.daocloud.io/library/python:3.11-slim-bookworm
FROM ${PY_IMAGE}

WORKDIR /app

ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DEFAULT_TIMEOUT=120

COPY pyproject.toml ./
COPY backend ./backend
COPY static ./static

RUN pip install --no-cache-dir --no-build-isolation .

RUN useradd --system --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /app/.runtime \
    && chown -R app:app /app

ENV PYTHONPATH=/app

USER app
EXPOSE 8091

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/health', timeout=5).read()" || exit 1

CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8091"]
