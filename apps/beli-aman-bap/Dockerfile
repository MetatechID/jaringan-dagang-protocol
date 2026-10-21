# Build from the repository root, not the app directory.
FROM python:3.12-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY apps/beli-aman-bap/requirements.txt ./requirements.txt
RUN pip wheel --wheel-dir /wheels -r requirements.txt
COPY packages/beckn-protocol-py /src/beckn-protocol-py
RUN pip wheel --no-deps --wheel-dir /wheels /src/beckn-protocol-py

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SKIP_CREATE_ALL=true DEBUG=false
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl && rm -rf /wheels
COPY --chown=app:app apps/beli-aman-bap/ ./
USER 10001:10001
EXPOSE 8003
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8003/ready', timeout=4)"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
