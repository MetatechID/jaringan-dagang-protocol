"""Payments router — legacy.

The mock ``POST /api/v1/orders/{id}/confirm-payment`` endpoint that this
file used to expose has been retired. Real payment is captured by Xendit:

  - SDK flow → ``POST /api/v1/orders/{id}/invoice`` mints a Xendit hosted
    invoice (see ``routers/orders.py:create_invoice``).
  - Bot flow → ``POST /api/v1/checkout/{cart_id}/confirm`` mints the same
    (see ``routers/checkout.py``).

In both paths, the ``invoice.paid`` Xendit webhook
(``routers/webhooks_xendit.py``) is what flips the order to ESCROW_HELD
and writes the HOLD ledger entry.

The module is kept as an empty router export so ``main.py``'s
``include_router(payments_router)`` line doesn't error during a rolling
deploy. Safe to delete once main.py is updated.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/orders", tags=["payments"])
