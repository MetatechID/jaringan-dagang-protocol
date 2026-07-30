"""Shared fakes for Jubelio webhook unit tests.

Mirror of ``tests/_oy_fakes.py`` with a Jubelio-specific ``sign()`` helper
that matches contract v1.8 §7 webhook signature:

    signature = HMAC_SHA256(secret_bytes, body_bytes + secret_bytes).hexdigest()

(Compare OY, which signs ``body_bytes`` alone — Jubelio concatenates the
secret onto the payload before HMAC. See API Contract Jubelio Shipment
v1.8 page 21 for the reference Node.js snippet.)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from types import SimpleNamespace

# Make the BAP package importable so ``from routers import webhooks_jubelio``
# works from any test file under ``tests/``. One place, one file.
_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


def sign(body_bytes: bytes, secret: str) -> str:
    """Jubelio webhook signature: HMAC-SHA256(secret, body || secret).hexdigest().

    The contract example (Node.js):
        crypto.createHmac("sha256", secret)
            .update(payload + secret)
            .digest("hex")

    We match bytestring-for-bystring (use ``secret.encode("utf-8")`` to
    exactly mirror the ``crypto.createHmac(..., secret)`` semantics).
    """
    secret_bytes = secret.encode("utf-8")
    return hmac.new(
        secret_bytes, body_bytes + secret_bytes, hashlib.sha256
    ).hexdigest()


class FakeExecute:
    """Async SQL result shim — same shape as ``_oy_fakes.FakeExecute``."""

    def __init__(self, value):
        self._value = value

    def scalars(self):
        return SimpleNamespace(first=lambda: self._value)

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Async session whose ``execute`` returns results from a queue.

    Mirrors ``_oy_fakes.FakeSession``. Tests choose how many results the
    webhook handler will see by passing tuples at the start of the list;
    remaining calls return ``None``.
    """

    def __init__(self, results):
        self._results = list(results)
        self.committed = False
        self.added: list = []

    async def execute(self, *_args, **_kwargs):
        if not self._results:
            return FakeExecute(None)
        nxt = self._results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeExecute(nxt)

    async def commit(self):
        self.committed = True

    def add(self, obj):
        self.added.append(obj)
        return None


def StubOrder(**overrides) -> SimpleNamespace:
    """Minimal Order-shape for webhook tests.

    Only attributes the Jubelio webhook actually reads from the Order:
    ``id``, ``state``, ``fulfillment_status``, ``fulfillment_awb``,
    ``fulfillment_tracking_url``, ``jubelio_shipment_id``,
    ``fulfillment_last_event_at``, ``delivered_at``, ``auto_release_at``,
    plus the mutation surface (``state`` setter + Order enum).
    """
    from models.order import OrderState

    base = dict(
        id="order-abc",
        state=OrderState.FULFILLING,
        fulfillment_status=None,
        fulfillment_awb=None,
        fulfillment_tracking_url=None,
        jubelio_shipment_id=None,
        fulfillment_last_event_at=None,
        delivered_at=None,
        auto_release_at=None,
        shipping_address=None,
        items=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)
