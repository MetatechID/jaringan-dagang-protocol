# OITO protocol

Source repository: `MetatechID/jaringan-dagang-protocol`. Existing package names
and network subscriber identifiers remain unchanged.

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

## BAP callback authorization

BAP callbacks require a verified subscriber signature, even when the legacy
`BECKN_REQUIRE_SIGNATURE` switch is false. The signer must match `context.bpp_id`,
`context.bap_id` must address this BAP, and the context action must match the
callback route. Include nonempty transaction and message IDs.

Order callbacks use the local order ID as the transaction ID and must reference
that order's local ID or persisted seller reference. Cart confirmations must
match the persisted cart transaction, BPP and confirmed order reference. Issue
and rating callbacks resolve their parent order before changing records. A QR or
invoice URL is not evidence of payment on any payment rail.

Catalog callbacks may publish only the signing subscriber's provider. Emit
separate per-provider signed envelopes; a shared process signature is not
implicit delegation. Subscriber rotation or colliding storefront slugs require
an explicit trusted migration, not a callback-driven ownership change. Catalog
pushes may use fresh transaction IDs without an outbound search request.

Completed retries are scoped by verified signer, action, transaction and message
ID. Stored inbound message keys are hashes of that tuple, retaining the existing
schema. Legacy unscoped receipts are not reused. Authorization is rechecked before
returning a cached success, and failures roll back without a success receipt.

Run the focused offline regression job with Python 3.12 and the canonical protocol
wheel installed in the BAP environment:

```sh
apps/beli-aman-bap/.venv/bin/python -I apps/beli-aman-bap/scripts/test-beckn-ownership-security.py
```

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

## Authoritative checkout shipping

`POST /api/v1/orders` requires `destination_postal_code` (five ASCII digits),
cart SKUs/quantities and a selected `shipping` courier/service. The submitted
`shipping.price_idr` is only confirmation of the displayed quote: the server
resolves the persisted brand's catalog prices and weights, configured origin,
cart and destination and obtains a fresh provider rate. Client item weights,
values, courier labels and delivery estimates are not authoritative.

The PRE_AUTH order binds the tenant/BPP, catalog snapshots, destination, origin
fingerprint, carrier and selected rate. Attaching either an owned saved address
or an inline address revalidates these inputs and fetches a fresh rate before
AUTHED. A changed destination/cart/catalog/origin/service/rate (including
provider labels or estimates) requires refreshing rates and creating a new
order; stale selections return 409. Legacy PRE_AUTH orders without a binding
must also be recreated. Missing/invalid input returns 422; unavailable provider
rates return 503. No database migration is needed for the JSON binding.

Omitting shipping does **not** grant free shipping. Only the operator-managed
`SHIPPING_FREE_BRAND_SLUGS` comma-separated allowlist permits zero-price shipping
(default empty); browser input cannot enable it. This exemption still requires
a valid destination/cart and is checked again when attaching the address.
Nonlocal environments fail closed on missing credentials, provider errors and
empty responses. Fabricated rate fallbacks are limited to explicit
`development`/`test`; malformed rate rows are rejected even there.

**Client rollout order:** merge/release the destination-aware client contract
([public SDK companion PR #5](https://github.com/MetatechID/beli-aman-sdk/pull/5))
and update every other checkout client **before** deploying the strict server.
Clients must submit the destination used for quoting and handle 409 by
requoting/recreating, not by removing shipping. Older clients that omit the
destination are intentionally incompatible.

The fatal `Shipping security / authoritative-shipping` workflow runs synthetic
route/provider regressions with Python 3.12 and the canonical protocol wheel.
Repository branch protection must require that check to block merging. To run
locally after installing app/test dependencies and the canonical wheel:

```sh
python -I apps/beli-aman-bap/scripts/test-shipping-security.py \
  tests/test_shipping_security.py tests/test_shipping_proxy.py \
  tests/test_carriers_passthrough.py
```

The runner clears inherited configuration before app imports, disables dotenv
and plugin autoload, and rejects unexpected outbound sockets. Tests use fake
DB/provider boundaries, not live provider or database integration.

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

### Dipay callback verification configuration (JD-004)

Before enabling Dipay outside explicit `ENVIRONMENT=development` or `test`, set
`DIPAY_CALLBACK_PUBLIC_KEY` to **Dipay's callback-signing RSA public key**, not
our merchant access-token signing private key. The existing receiver contract
accepts inline PEM, base64-encoded PEM/DER, or a readable local public-key file
path. No new RSA bit-size restriction is imposed. Do not put key contents in
logs or use a production key for tests.

Startup rejects a missing, blank, malformed, unreadable, private, or non-RSA
callback key when `DIPAY_CLIENT_KEY` enables the environment integration. A
brand-only integration may start without environment credentials, but cart,
SDK order, and partner invoice paths validate the platform callback key again
before invoice mutations, reservation writes, or provider calls. Partner order
registration also checks before creating its row. Key-file changes are not
cached; an invalid rotation blocks subsequent invoice creation. Staging,
unknown, blank and missing runtime modes never opt into local fixtures or
unsigned callback bypass. Existing explicit development/test fixtures remain
compatible, including omitted callback keys.

Parsing proves usability for the configured SHA256withRSA verifier, **not key
provenance**: an unrelated RSA public key cannot be distinguished offline.
Operators must obtain the callback key through Dipay's trusted channel and
confirm it with an authentic signed sandbox callback before enabling payments.
There is no automatic key discovery, outbound lookup, or comparison with the
merchant signing key (these keys serve different roles). Existing callback
status-query/fallback behavior is unchanged by this completeness check.

Focused blocking CI installs the canonical protocol wheel and runs offline with
inherited app settings stripped before import, dotenv and third-party pytest
plugin autoload disabled, and real socket/DNS access forbidden. Locally, using
the Python 3.12 BAP virtualenv with that wheel and test dependencies installed:

```sh
apps/beli-aman-bap/.venv/bin/python -I apps/beli-aman-bap/scripts/test-dipay-callback-key.py
```

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
