# jaringan-dagang-beckn

[Beckn protocol](https://becknprotocol.io/) library for Python.

Context builders, Ed25519 signing, envelope (de)serialization. Pair
with [`jaringan-dagang-network-extension`](https://github.com/MetatechID/jaringan-dagang-protocol/tree/main/packages/network-extension)
for ONDC-style Indonesian commerce localization.

## Install

```bash
uv add jaringan-dagang-beckn      # uv
pip install jaringan-dagang-beckn # pip
```

## Quickstart

```python
from jaringan_dagang_beckn import BecknContext

ctx = BecknContext(
    domain="ONDC:RET11",      # Packaged F&B
    country="IDN",
    city="std:021",            # Jakarta
    action="search",
    core_version="1.1.0",
    bap_id="my-bap.example.com",
    bap_uri="https://my-bap.example.com/beckn",
)
```

See the [reference BAP](https://github.com/MetatechID/jaringan-dagang-protocol/tree/main/apps/beli-aman-bap)
for an end-to-end example.

## License

Apache-2.0.
