# docker buildx build -f infra/docker/protocol-wheel.Dockerfile --target artifact --output type=local,dest=artifacts .
FROM python:3.12-slim AS builder
WORKDIR /build
COPY packages/beckn-protocol-py ./package
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /dist ./package
FROM scratch AS artifact
COPY --from=builder /dist/ /
