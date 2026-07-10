"""Shared fakes for OY Indonesia unit tests.

Centralises the SQLAlchemy-result + Cart/Brand stub helpers used by both
``tests/test_oy_invoices.py`` and ``tests/test_webhooks_oy.py``. Import
from here in both files — no inline duplicates.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from types import SimpleNamespace

# Make the BAP package importable so ``from services import oy_invoices``
# works from any test file under ``tests/``. One place, both files.
_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


def sign(body_bytes: bytes, secret: str) -> str:
    """HMAC SHA-256 of ``body_bytes`` keyed by ``secret``."""
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


class FakeExecute:
    """Async SQL result shim supporting both consumer styles.

    - ``.scalars().first()`` — used by `cart_q` / `order_q` lookups
      that walk a list of rows.
    - ``.scalar_one_or_none()`` — used by lookups expected to return
      at-most-one row directly from ``execute()``.
    """

    def __init__(self, value):
        self._value = value

    def scalars(self):
        return SimpleNamespace(first=lambda: self._value)

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Async session whose ``execute`` returns results from a queue.

    Each ``await session.execute(...)`` pops the next item; raise
    ``Exception`` to make the next call raise. Also stubs ``commit``
    and ``add`` no-ops (used in cart-path escrow insert).
    """

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
    """Minimal Cart-shape.

    Defaults match the existing webhook tests' StubCart. Pass overrides
    for any extra attribute the test sets (e.g. order_id, status).
    """
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
    payment_provider: str = "oy",
    oy_api_key: str = "brand-key",
    oy_username: str = "brand-user",
    oy_callback_secret: str = "topsecret",
    slug: str = "safiya",
    bpp_id: str = "safiya.bpp.jaringan-dagang.id",
    xendit_sub_account_id: str | None = None,
    **overrides,
) -> SimpleNamespace:
    """Minimal Brand-shape with sane defaults for OY-happy-path tests."""
    base = dict(
        id="brand-id",
        slug=slug,
        name="Safiya",
        bpp_id=bpp_id,
        payment_provider=payment_provider,
        oy_api_key=oy_api_key,
        oy_username=oy_username,
        oy_callback_secret=oy_callback_secret,
        oy_store_id="store-1",
        xendit_sub_account_id=xendit_sub_account_id,
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
