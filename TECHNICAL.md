# jaringan-dagang-protocol — technical architecture

> Apache-2.0. For runnable Indonesian-commerce reference deployments
> built on this, see [`MetatechID/jaringan-dagang`](https://github.com/MetatechID/jaringan-dagang)
> (private).

## What's in here

This repo defines a **Beckn-protocol network localized for Indonesia**
plus reference implementations of the four roles you need to operate
one.

### packages/ (libraries)

| Package | Lang | npm / PyPI | What it does |
|---|---|---|---|
| `beckn-protocol-js` | TypeScript | `@jaringan-dagang/beckn-protocol` | Beckn 1.1 context builders, Ed25519 signing, envelope (de)serialization |
| `beckn-protocol-py` | Python | `jaringan-dagang-beckn` | Same as above, for Python BAPs/BPPs |
| `network-extension` | YAML + codegen | `@jaringan-dagang/network-extension` | ONDC-style localization: domain codes (`ONDC:RET`, `RET10`, `RET11`…), enum catalogs (fulfillment states, return reasons), Indonesian city codes (`std:021` Jakarta), payment methods (QRIS, VA, e-wallet) |
| `channel-adapter` | TypeScript | `@jaringan-dagang/channel-adapter` | Pure-interface contract: `IncomingMessage`, `OutgoingMessage`, `Renderer<C>` for web/WA/FB Messenger/Telegram/IG DM |

### apps/ (deployable reference implementations)

| App | Stack | Beckn role | Hostname (production reference) |
|---|---|---|---|
| `registry` | Next.js | Registry | (deployed at <https://jaringan-dagang.metatech.id> by MetatechID) |
| `gateway` | Next.js | Gateway | (BG / fan-out — deployed by MetatechID) |
| `beli-aman-bap` | FastAPI | BAP (buyer-protection) | <https://api.beli-aman.metatech.id> |
| `onboarding-portal` | Next.js | Subscriber-onboarding UI | <https://jaringan-dagang.metatech.id> |

Each app is deployable to Vercel / a container runtime out of the box.
The reference deployment is configured + run by MetatechID; this repo
is the source of truth for the code.

## Domain conventions (ONDC for Indonesia)

| Domain code | Maps to | Used by |
|---|---|---|
| `ONDC:RET` | retail (umbrella) | All retail sellers |
| `ONDC:RET10` | Grocery (general) | Mini-marts, supermarkets |
| `ONDC:RET11` | Packaged F&B | Safiya, Matchamu (DTC pantry) |
| `ONDC:RET12` | F&B (prepared) | Restaurants, kitchens |
| `ONDC:LOG10` | Logistics (P2P) | Courier integrations |
| `ONDC:LOG11` | Logistics (Mile + Drop) | |

Under the hood, the Beckn `domain` field carries
`nic2004:52110` (legacy Beckn-retail) — the ONDC code is conveyed in
context tags. See `packages/network-extension/domains/retail.yaml`.

## Cities

Subset of Indonesian cities supported in v1. Full list in
`packages/network-extension/cities.yaml`:

| Code | City |
|---|---|
| `std:021` | Jakarta |
| `std:022` | Bandung |
| `std:031` | Surabaya |
| `std:061` | Medan |
| `std:024` | Semarang |
| `std:0274` | Yogyakarta |

Schema follows ONDC India's `std:NNN` pattern, but the numeric body is
the Indonesian area code (the digits that come after `+62` on a phone
number).

## Payment methods

| Code | Method |
|---|---|
| `BAP/QRIS` | QRIS (universal Indonesian QR) |
| `BAP/VA/<BANK>` | Bank-issued Virtual Accounts (BCA, BNI, BRI, Mandiri, …) |
| `BAP/WALLET/<PROVIDER>` | E-wallets (GoPay, OVO, Dana, ShopeePay) |
| `BAP/COD` | Cash on delivery |

The BAP holds funds in escrow until D+3 after delivery (or earlier on
customer confirm). See `apps/beli-aman-bap/services/escrow.py`.

## Subscriber identity

Following ONDC India's pattern: every Beckn participant has a
`subscriber_id` of the form `<host>.jaringan-dagang.id`. The
network-extension package's `subscribers.yaml` registers static known
subscribers; `registry/` serves the dynamic lookup endpoint.

Reserved subscriber_ids:
- `beli-aman.bap.jaringan-dagang.id` — Beli Aman BAP (this repo's reference BAP)
- `<brand>.jaringan-dagang.id` — per-tenant BPP (Safiya = `safiyafood.jaringan-dagang.id`)
- `gateway.jaringan-dagang.id` — Beckn gateway
- `registry.jaringan-dagang.id` — Beckn registry

## Signing

Ed25519. Each subscriber holds a `signing_private_key` (base64) and
publishes the matching public key via the registry. The
`beckn-protocol-py.signer` / `beckn-protocol-js/signer.ts` modules
sign outbound envelopes and verify inbound ones.

## How to use these packages

### Build a BPP

```python
from jaringan_dagang_beckn import BecknContext, Signer
from jaringan_dagang_network_extension import resolve_ondc_domain

ctx = BecknContext(
    domain=resolve_ondc_domain(store="my-shop", category="packaged-food"),  # → ONDC:RET11
    country="IDN",
    city="std:021",
    action="on_search",
    core_version="1.1.0",
    bap_id="some-bap.jaringan-dagang.id",
    bap_uri="https://some-bap.example.com/beckn",
    bpp_id="my-shop.jaringan-dagang.id",
    bpp_uri="https://my-shop.example.com/beckn",
    transaction_id="...",
    message_id="...",
)
envelope = Signer(my_keys).sign({"context": ctx.dict(), "message": {...}})
```

### Build a BAP

Same shape; just emit `action=search` / `select` / `init` / `confirm`
and consume callbacks at your `/on_search` etc. endpoints. The
`apps/beli-aman-bap/` reference impl shows the full flow including
escrow.

### Run the registry locally

```bash
docker run -p 3030:3030 ghcr.io/metatechid/jaringan-dagang-registry:latest
# Or:
pnpm --filter @jaringan-dagang/registry dev
```

## Versioning

Each package versions independently via Changesets. PRs include a
`.changeset/<name>.md` describing the change. Merging triggers a
"Version Packages" PR; merging *that* publishes to npm + PyPI.

`apps/*` are containerized references, not npm/PyPI publishables.
Their tags + Docker images follow the latest stable
`@jaringan-dagang/beckn-protocol` version.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Short version: small PRs,
include a changeset, no tenant-specific code in the OSS tree.
