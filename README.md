# jaringan-dagang-protocol

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Open commerce network protocol for Indonesia — built on
[Beckn](https://becknprotocol.io/) with [ONDC](https://ondc.org/)
localization conventions.

This repo is the **canonical home** of:

- The Beckn base library, in TypeScript and Python (signing, context,
  envelopes).
- ONDC-style **network extension** (domain codes, enums, error
  catalogue, Indonesian city/payment localization).
- A **channel-adapter contract** so commerce chatbots speak the same
  language across web, WhatsApp, Messenger, Telegram.
- Reference implementations of the four roles you need to run a
  network: `registry`, `gateway`, `beli-aman-bap`, `onboarding-portal`.

If you're building an Indonesian DTC commerce network, this is your
starting point.

## Status

Pre-1.0. APIs may break between minor versions. Once we tag `1.0`,
breaking changes get a deprecation cycle.

## Layout

```
packages/
├─ beckn-protocol-js/     @jaringan-dagang/beckn-protocol (npm)
├─ beckn-protocol-py/     jaringan-dagang-beckn (PyPI)
├─ network-extension/     @jaringan-dagang/network-extension (npm)
└─ channel-adapter/       @jaringan-dagang/channel-adapter (npm)

apps/                     deployable reference implementations
├─ registry/              subscriber registry
├─ gateway/               Beckn gateway / fan-out
├─ beli-aman-bap/         buyer-protection BAP (FastAPI)
└─ onboarding-portal/     subscriber-onboarding UI (Next.js)

examples/                 runnable how-tos
docs/                     specs + ADRs
tests/contract/           Beckn protocol contract tests
```

## Quickstart

```bash
# Use the JS protocol library
pnpm add @jaringan-dagang/beckn-protocol

# Or the Python one
uv add jaringan-dagang-beckn

# Run the reference registry locally
docker run -p 3030:3030 ghcr.io/metatechid/jaringan-dagang-registry:latest
```

See [`examples/`](examples/) for end-to-end scenarios:
quote-a-product, confirm-an-order, dispute-a-fulfillment.

## Who this is for

- **DTC brands**: skip building Beckn plumbing — install the SDK,
  add your catalog, you're on the network.
- **Operators**: deploy `registry` + `gateway` to run your own
  Indonesian commerce network.
- **Researchers / regulators**: read [`docs/specs/`](docs/specs/)
  to understand the protocol shape.

## License

Apache-2.0. See [LICENSE](LICENSE). Contributions welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md).

Maintained by [@MetatechID](https://github.com/MetatechID). Our
production deployment is documented at <https://safiya.beliaman.com>.

## Docker: standalone protocol stack

All app Dockerfiles use the **repository root** build context. Python images use
Python 3.12, portal uses Node 24 / pnpm 9.12.0, and runtime processes run as UID
10001 (not root). Internal ports: registry 3030, gateway 4030, BAP 8003, portal
3001. Compose publishes these only on loopback; databases/Redis are not published.
This is a local reference stack, not a production deployment/security profile.

```sh
cp .env.example .env
# Edit local passwords/tokens (URL-safe alphanumeric DB password).
docker compose config --quiet
docker compose build
docker compose up -d --wait registry-db bap-db redis
# Explicit, repeatable fresh-schema initialization; run from app image /app.
docker compose run --rm --no-deps registry python scripts/docker_schema.py init
docker compose run --rm --no-deps registry python scripts/docker_schema.py init
docker compose run --rm --no-deps registry python scripts/docker_schema.py verify
docker compose run --rm --no-deps bap python scripts/docker_schema.py init
docker compose run --rm --no-deps bap python scripts/docker_schema.py init
docker compose run --rm --no-deps bap python scripts/docker_schema.py verify
docker compose up -d --wait --wait-timeout 180
curl --fail http://localhost:3030/ready
curl --fail http://localhost:4030/ready
curl --fail http://localhost:8003/ready
# Portal: http://localhost:3001
docker compose down
```

Normal startup **does not mutate schema** (`SKIP_CREATE_ALL=true` by default).
`init` creates missing tables from the complete ORM metadata, then verifies;
`verify` never mutates data and checks connectivity, required tables, columns,
types, nullability and primary keys. It is a structural baseline, not an audit
of all constraints/indexes/defaults. Running init twice must succeed on a fresh
schema. Existing deployment upgrades need separately reviewed migrations: this
command deliberately does not blindly execute historical `scripts/add-*.py`,
rename/drop columns, repair drift, or seed users. Back up existing databases
before any migration; `create_all` cannot upgrade existing tables. Do not grant
runtime DB roles DDL permission in production. Development-only implicit creation
requires explicitly setting `SKIP_CREATE_ALL=false`.

`/health` is liveness. Registry/BAP `/ready` require their database and schema;
gateway `/ready` requires registry `/ready`. Redis is an optional runtime cache,
not a readiness requirement. Compose starts dependencies using readiness gates.
BAP catalog workers and seller bridge are disabled for this isolated stack.
To remove local data intentionally, use `docker compose down --volumes`.

### BAP startup safety (JD-026)

The BAP image defaults to `ENVIRONMENT=production`, `DEBUG=false`, and the
independent process marker `BAP_PRODUCTION_DEPLOYMENT=true`. `ENVIRONMENT` is
trimmed and case-normalized; only `production`, `staging`, `development`, and
`test` are accepted. Production and staging require `DEBUG=false` and nonblank,
nondefault `ADMIN_TOKEN` and `SELLER_BRIDGE_TOKEN`, even with the bridge disabled.
Compose requires `BAP_ADMIN_TOKEN` and `BAP_SELLER_BRIDGE_TOKEN` and maps them to
those application settings. Replace both root example placeholders with distinct,
random credentials; do not commit actual values or place secrets in build args.

Validation runs when settings load and again before database/worker startup.
A true deployment marker requires `ENVIRONMENT=production`; malformed marker
values fail closed. The marker is read only from the process environment, never
from application dotenv settings. For non-container production launchers, load
application configuration first, then set/export `BAP_PRODUCTION_DEPLOYMENT=true`
in the deployment-owned launcher before starting Python. Do not permit mutable
application configuration to override that signal. This prevents accidental mode
downgrades, not an operator with control over the launcher/container environment.

Direct local Python runs without the marker retain development/test defaults.
To run staging with this image, a separate, explicitly non-production deployment
must override the marker to `false` and set `ENVIRONMENT=staging`; staging still
requires secure tokens and disabled debug. Never downgrade a production launcher
to bypass validation: correct its configuration instead. These checks do not
provision external integrations or replace a complete deployment security review.

### Portal and external services

Firebase `NEXT_PUBLIC_*` client values are **public build-time configuration**.
Blank values disable sign-in; there is no implicit production Firebase fallback.
Set all client values in `.env` before building to enable login and authorize
localhost in your Firebase project. Never pass Firebase Admin/service-account
JSON, credentials, private keys or tokens as Docker build arguments. BAP Admin
credentials, when needed, belong in runtime environment/secrets only.

Portal proxy destinations (`REGISTRY_URL`, `GATEWAY_URL`, `BAP_URL`, `BPP_URL`)
are also resolved at build time. The Compose build uses internal DNS; changing
these or public Firebase values requires rebuilding the portal. Images built
with these defaults expect the matching service names on their Docker network.

**Limitations:** this repo contains Beli Aman BAP, not the legacy JD BAP. The
portal demo still requests legacy `/api/search` endpoints, which this BAP does
not implement; live legacy search is not a success criterion for this stack.
There is **no seller BPP in this repository**. `BPP_URL` defaults to an unreachable
`.invalid` host; set it to an externally reachable BPP and rebuild for BPP proxy
features. No subscriber, real payment/shipping provider, Firebase Admin identity,
or complete signed commerce flow is provisioned by this stack. Set public
callback URLs and real integration configuration explicitly before enabling any
external integration; container localhost is not a callback address.

### Canonical Python wheel and validation

```sh
docker buildx build -f infra/docker/protocol-wheel.Dockerfile \
  --target artifact --output type=local,dest=artifacts .
# Exports jaringan_dagang_beckn-*.whl, built from packages/beckn-protocol-py.
# App builds never require a pre-existing host wheel.
bash infra/docker/smoke.sh
```

The smoke script owns the dedicated `protocol-docker-smoke` Compose project and
**deletes its volumes** on exit. Run it only on a disposable CI/local stack.
It checks empty-schema rejection, init twice, schema verification, service/proxy
readiness, and readiness failure when databases stop. The Docker workflow builds
all four images and exports/installs the canonical wheel; failures are fatal and
nothing is published. A separate blocking Python 3.12 CI job builds/installs the
canonical wheel alongside the BAP requirements and runs only the isolated
startup-guard tests with synthetic configuration and no services or real dotenv.
The broader non-Docker CI bootstrap behavior is unchanged.

Python requirements retain upstream constraints; transitive dependencies are
not fully locked yet. The local implementation environment has no Python 3.12
resolver/pip tooling or Docker daemon access, so no unverified pins were added.
Before reproducible production promotion, generate reviewed Python 3.12 locks
(e.g. `pip-compile --generate-hashes` in `python:3.12-slim`), and use those locks
in image builds. The portal already installs the workspace pnpm lock frozen.
