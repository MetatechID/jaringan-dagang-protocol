"""Shared fakes for Sento payment gateway unit tests.

Centralises the SQLAlchemy-result + Cart/Brand stub helpers used by both
``tests/test_sento_invoices.py`` and ``tests/test_webhooks_sento.py``.
Import from here in both files — no inline duplicates.

Mirror of ``tests/_oy_fakes.py`` with sento_* defaults instead of oy_*.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

# Make the BAP package importable so ``from services import sento_invoices``
# works from any test file under ``tests/``. One place, both files.
_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


class FakeExecute:
    """Async SQL result shim supporting both consumer styles."""

    def __init__(self, value):
        self._value = value

    def scalars(self):
        return SimpleNamespace(first=lambda: self._value)

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Async session whose ``execute`` returns results from a queue."""

    def __init__(self, results):
        self._results = list(results)
        self.committed = False
        self.added: list = []

    async def execute(self, *_args, **_kwargs):
        nxt = self._results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeExecute(nxt)

    async def commit(self):
        self.committed = True

    def add(self, obj):
        self.added.append(obj)
        return None


def StubCart(*, payment_state: str = "pending", status=None, **overrides):
    """Minimal Cart-shape. Pass overrides for any extra attribute."""
    if status is None:
        from models.bot_rest import CartStatus
        status = CartStatus.CONFIRMED
    base = dict(
        id="cart-123",
        bpp_id="safiya.bpp.jaringan-dagang.id",
        order_id="order-abc",
        payment_state=payment_state,
        status=status,
        invoice_id=None,
        invoice_provider=None,
        qr_image_url=None,
        quote_json={"total_idr": 100_000},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def StubBrand(
    *,
    payment_provider: str = "sento",
    sento_api_key: str = "brand-key",
    sento_username: str = "brand-user",
    sento_callback_secret: str = "topsecret",
    slug: str = "safiya",
    bpp_id: str = "safiya.bpp.jaringan-dagang.id",
    **overrides,
) -> SimpleNamespace:
    """Minimal Brand-shape with sane defaults for Sento-happy-path tests."""
    base = dict(
        id="brand-id",
        slug=slug,
        name="Safiya",
        bpp_id=bpp_id,
        payment_provider=payment_provider,
        sento_api_key=sento_api_key,
        sento_username=sento_username,
        sento_callback_secret=sento_callback_secret,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def StubOrder(*, total_idr: int = 750_000, **overrides) -> SimpleNamespace:
    """Minimal Order-shape for create_invoice_for_order tests."""
    base = dict(
        id="order-test-id",
        brand_id="brand-id",
        total_idr=total_idr,
        items=[{"name": "Test", "qty": 1, "unit_price_idr": total_idr}],
        shipping_address={"email": "buyer@example.com", "recipient_name": "Buyer"},
        payment_method_snapshot=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)