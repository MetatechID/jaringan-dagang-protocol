# docker buildx build -f infra/docker/protocol-wheel.Dockerfile --target artifact --output type=local,dest=artifacts .
FROM python:3.12-slim AS builder
WORKDIR /build
COPY packages/beckn-protocol-py ./package
COPY packages/ops-py ./ops-py
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /dist ./package ./ops-py
FROM scratch AS artifact
COPY --from=builder /dist/ /
