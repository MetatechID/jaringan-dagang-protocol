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
