#!/usr/bin/env bash
# Destructive only to this dedicated CI project; never run against a shared stack.
set -euo pipefail
export COMPOSE_PROJECT_NAME=protocol-docker-smoke
export POSTGRES_PASSWORD=local-smoke-password
export BAP_ADMIN_TOKEN=local-smoke-admin-token
export BAP_SELLER_BRIDGE_TOKEN=local-smoke-seller-bridge-token
cleanup() {
  docker compose logs --no-color
  docker compose down --volumes --remove-orphans
}
trap cleanup EXIT
docker compose config --quiet
docker compose build
docker compose up -d --wait registry-db bap-db redis
for app in registry bap; do
  # A new volume must fail verification, not report false readiness.
  if docker compose run --rm --no-deps "$app" python scripts/docker_schema.py verify; then
    printf 'ERROR: empty %s schema unexpectedly verified\n' "$app" >&2
    exit 1
  fi
  docker compose run --rm --no-deps "$app" python scripts/docker_schema.py init
  docker compose run --rm --no-deps "$app" python scripts/docker_schema.py init
  docker compose run --rm --no-deps "$app" python scripts/docker_schema.py verify
done
docker compose up -d --wait --wait-timeout 180
for port in 3030 4030 8003; do curl --fail --silent "http://localhost:$port/ready"; done
curl --fail --silent http://localhost:3030/subscribers
curl --fail --silent http://localhost:3001/ > /dev/null
curl --fail --silent http://localhost:3001/api/registry/subscribers
# Liveness isn't dependency readiness. Check database loss reaches the gateway.
docker compose stop registry-db bap-db
sleep 2
for port in 3030 4030 8003; do
  code=$(curl --silent --output /dev/null --write-out '%{http_code}' "http://localhost:$port/ready")
  test "$code" = 503
  curl --fail --silent "http://localhost:$port/health"
done
