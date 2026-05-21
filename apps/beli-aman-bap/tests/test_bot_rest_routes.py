"""Task B3a — bot-facing REST surface for search / cart / checkout.

Covers:
  - ``auth.bot_auth.require_bot`` accepts the configured Bearer token and
    rejects every other input (missing, malformed, wrong, empty).
  - The three router modules import cleanly.
  - End-to-end happy paths for search / cart-select / cart-init /
    checkout-confirm / checkout-status via the FastAPI TestClient with an
    in-memory SQLite DB.
  - Error paths (404 for unknown cart/session, 409 for invalid state
    transitions, 410 for expired cart).

These tests deliberately avoid real network IO — every call to
``services.order_flow.send_search / select_cart / init_order /
confirm_order_v2`` is monkeypatched to capture the envelope (when
relevant) and return success.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


# --- 1. require_bot ----------------------------------------------------


class TestRequireBot:
    """Behaviour of ``auth.bot_auth.require_bot`` as a pure function."""

    def test_rejects_missing_token_when_configured(self, monkeypatch):
        from auth.bot_auth import require_bot
        from fastapi import HTTPException

        monkeypatch.setenv("BOT_API_TOKEN", "secret-A")

        with pytest.raises(HTTPException) as ei:
            require_bot(authorization=None)
        assert ei.value.status_code == 401

    def test_rejects_empty_bearer(self, monkeypatch):
        from auth.bot_auth import require_bot
        from fastapi import HTTPException

        monkeypatch.setenv("BOT_API_TOKEN", "secret-A")

        with pytest.raises(HTTPException) as ei:
            require_bot(authorization="Bearer ")
        assert ei.value.status_code == 401

    def test_rejects_wrong_scheme(self, monkeypatch):
        from auth.bot_auth import require_bot
        from fastapi import HTTPException

        monkeypatch.setenv("BOT_API_TOKEN", "secret-A")

        with pytest.raises(HTTPException) as ei:
            require_bot(authorization="Token secret-A")
        assert ei.value.status_code == 401

    def test_rejects_wrong_token(self, monkeypatch):
        from auth.bot_auth import require_bot
        from fastapi import HTTPException

        monkeypatch.setenv("BOT_API_TOKEN", "secret-A")

        with pytest.raises(HTTPException) as ei:
            require_bot(authorization="Bearer secret-B")
        assert ei.value.status_code == 401

    def test_rejects_when_server_token_unset(self, monkeypatch):
        from auth.bot_auth import require_bot
        from fastapi import HTTPException

        monkeypatch.delenv("BOT_API_TOKEN", raising=False)

        with pytest.raises(HTTPException) as ei:
            require_bot(authorization="Bearer anything")
        assert ei.value.status_code == 401

    def test_accepts_correct_bearer(self, monkeypatch):
        from auth.bot_auth import require_bot

        monkeypatch.setenv("BOT_API_TOKEN", "secret-A")

        # Should not raise.
        result = require_bot(authorization="Bearer secret-A")
        assert result is None

    def test_token_rotation_picked_up_without_restart(self, monkeypatch):
        from auth.bot_auth import require_bot
        from fastapi import HTTPException

        monkeypatch.setenv("BOT_API_TOKEN", "token-old")
        # require_bot reads env at call time.
        require_bot(authorization="Bearer token-old")

        monkeypatch.setenv("BOT_API_TOKEN", "token-new")
        with pytest.raises(HTTPException):
            require_bot(authorization="Bearer token-old")
        require_bot(authorization="Bearer token-new")

    def test_uses_constant_time_comparison(self):
        """Source-level guarantee: bot_auth references ``hmac.compare_digest``.

        We can't directly observe timing in a unit test (it's a side-channel
        property), but we CAN assert the constant-time primitive is in use.
        If a refactor accidentally reverts to ``==`` this test fires.
        """
        import inspect

        from auth import bot_auth

        src = inspect.getsource(bot_auth)
        assert "compare_digest" in src, (
            "auth.bot_auth must use hmac.compare_digest, not == for token "
            "comparison (constant-time, defeats byte-by-byte timing probe)."
        )
        # Also assert ``hmac`` is imported, not just mentioned in a comment.
        assert "import hmac" in src

    def test_wrong_byte_at_any_position_returns_401(self, monkeypatch):
        """Strict equivalence: any deviation from the configured token → 401.

        Doesn't prove constant-time, but proves the comparison is in place
        and the byte-by-byte attacker can't sneak through with a partial
        match by exploiting a buggy short-circuit.
        """
        from auth.bot_auth import require_bot
        from fastapi import HTTPException

        token = "s3cr3t-token-9-chars-long-abc"
        monkeypatch.setenv("BOT_API_TOKEN", token)

        # Sanity: correct token works.
        require_bot(authorization=f"Bearer {token}")

        # Flip each byte, one at a time — all must 401.
        for i in range(len(token)):
            # Change one char to something definitely not equal.
            replacement = "X" if token[i] != "X" else "Y"
            wrong = token[:i] + replacement + token[i + 1 :]
            assert wrong != token, "test setup error"
            with pytest.raises(HTTPException) as ei:
                require_bot(authorization=f"Bearer {wrong}")
            assert ei.value.status_code == 401, (
                f"expected 401 for wrong byte at position {i}, got "
                f"{ei.value.status_code}"
            )

        # And shorter / longer than expected — both must 401.
        with pytest.raises(HTTPException):
            require_bot(authorization=f"Bearer {token[:-1]}")
        with pytest.raises(HTTPException):
            require_bot(authorization=f"Bearer {token}-extra")


# --- 2. Routers import + integrate -------------------------------------


class TestRouterImports:
    """The new routers must import + expose their expected routes."""

    def test_search_router_imports(self):
        from routers.search import router as search_router
        paths = {r.path for r in search_router.routes}
        assert "/search" in paths
        assert "/search/{session_id}/results" in paths

    def test_cart_router_imports(self):
        from routers.cart import router as cart_router
        paths = {r.path for r in cart_router.routes}
        assert "/cart/select" in paths
        assert "/cart/{cart_id}" in paths
        assert "/cart/{cart_id}/init" in paths
        assert "/cart/{cart_id}/order-draft" in paths

    def test_checkout_router_imports(self):
        from routers.checkout import router as checkout_router
        paths = {r.path for r in checkout_router.routes}
        assert "/checkout/{cart_id}/confirm" in paths
        assert "/checkout/{cart_id}/status" in paths


# --- 3. Integration via TestClient + sqlite ----------------------------


def _build_test_client(monkeypatch):
    """Build an isolated FastAPI app with the new routers + in-memory DB.

    We avoid importing main.py (which spins up real Beckn workers / hits
    the Postgres URL on lifespan). Instead we wire a minimal app with the
    three new routers plus a sqlite-backed override for ``get_db``.
    """
    import database
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Fresh in-memory sqlite for this test.
    sqlite_url = "sqlite+aiosqlite:///:memory:"
    test_engine = create_async_engine(sqlite_url)
    TestSession = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _get_db():
        async with TestSession() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Create only the tables this test surface needs. The buyer's full
    # Base.metadata includes Postgres-only types (JSONB on orders/brands)
    # that don't compile under sqlite — we don't need those tables here.
    from models.base import Base
    import models  # noqa: F401  (registers all models)
    from models.bot_rest import Cart, SearchSession
    from models.mirror import (
        MirrorProduct,
        MirrorProductImage,
        MirrorSKU,
        MirrorSKUImage,
        MirrorStore,
    )

    _TABLES_FOR_TESTS = [
        SearchSession.__table__,
        Cart.__table__,
        MirrorStore.__table__,
        MirrorProduct.__table__,
        MirrorSKU.__table__,
        MirrorProductImage.__table__,
        MirrorSKUImage.__table__,
    ]

    async def _create():
        async with test_engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=_TABLES_FOR_TESTS,
                checkfirst=True,
            )

    asyncio.run(_create())

    # Set the bot token before importing routers.
    monkeypatch.setenv("BOT_API_TOKEN", "test-bot-secret")

    from routers.search import router as search_router
    from routers.cart import router as cart_router
    from routers.checkout import router as checkout_router

    app = FastAPI()
    app.include_router(search_router, prefix="/api/v1")
    app.include_router(cart_router, prefix="/api/v1")
    app.include_router(checkout_router, prefix="/api/v1")

    app.dependency_overrides[database.get_db] = _get_db

    return TestClient(app), TestSession


class TestSearchEndpoints:
    def test_post_search_requires_bot_token(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)
        resp = client.post("/api/v1/search", json={"query": "matcha"})
        assert resp.status_code == 401

    def test_post_search_happy_path_captures_envelope(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)

        captured: dict = {}

        async def fake_send_search(
            *, query, category=None, city=None, bpp_id=None, bpp_uri=None, transaction_id=None
        ):
            captured["query"] = query
            captured["category"] = category
            captured["city"] = city
            captured["bpp_id"] = bpp_id
            captured["transaction_id"] = transaction_id
            return transaction_id or "txn-captured", True

        import services.order_flow as of
        monkeypatch.setattr(of, "send_search", fake_send_search)
        # The router does `from services import order_flow` and calls
        # `order_flow.send_search`; monkeypatching the attr on the module
        # is sufficient.

        resp = client.post(
            "/api/v1/search",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={"query": "matcha", "category": "beverages"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        assert body["session_id"]
        assert body["transaction_id"]
        assert captured["query"] == "matcha"
        assert captured["category"] == "beverages"

    def test_get_search_results_unknown_session_404(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)
        resp = client.get(
            "/api/v1/search/00000000-0000-0000-0000-000000000000/results",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp.status_code == 404

    def test_search_then_results_returns_accumulated_mirror(self, monkeypatch):
        client, TestSession = _build_test_client(monkeypatch)

        async def noop_send_search(**kwargs):
            return kwargs.get("transaction_id") or "txn", True

        import services.order_flow as of
        monkeypatch.setattr(of, "send_search", noop_send_search)

        # Seed a mirror store + product BEFORE the search call so the
        # /on_search-accumulated view has something to read.
        from models.mirror import (
            MirrorProduct,
            MirrorProductImage,
            MirrorSKU,
            MirrorStore,
        )

        async def seed():
            async with TestSession() as db:
                store = MirrorStore(
                    bpp_id="safiyafood.jaringan-dagang.id",
                    slug="safiyafood",
                    name="Safiya Food",
                    bpp_uri="https://safiya.example.id/beckn",
                )
                db.add(store)
                await db.flush()
                product = MirrorProduct(
                    store_id=store.id,
                    bpp_product_id="prod-1",
                    sku="prod-1",
                    name="Matcha Latte",
                    status="ACTIVE",
                )
                db.add(product)
                await db.flush()
                db.add(MirrorProductImage(
                    product_id=product.id, url="https://img.example/m.png",
                    position=0, is_primary=True,
                ))
                db.add(MirrorSKU(
                    product_id=product.id,
                    bpp_sku_id="sku-1",
                    sku_code="sku-1",
                    price=25000.0,
                    original_price=30000.0,
                    stock=10,
                ))
                await db.commit()

        asyncio.run(seed())

        resp = client.post(
            "/api/v1/search",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={"query": "matcha", "bpp_id": "safiyafood.jaringan-dagang.id"},
        )
        assert resp.status_code == 200, resp.text
        session_id = resp.json()["session_id"]

        resp2 = client.get(
            f"/api/v1/search/{session_id}/results",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp2.status_code == 200, resp2.text
        body = resp2.json()
        assert body["status"] == "results"
        assert len(body["results"]) == 1
        store_block = body["results"][0]
        assert store_block["bpp_id"] == "safiyafood.jaringan-dagang.id"
        assert len(store_block["products"]) == 1
        assert store_block["products"][0]["name"] == "Matcha Latte"
        assert store_block["products"][0]["skus"][0]["price_idr"] == 25000


class TestCartEndpoints:
    def test_cart_select_writes_row_and_calls_order_flow(self, monkeypatch):
        client, TestSession = _build_test_client(monkeypatch)

        captured: dict = {}

        async def fake_select_cart(
            *, cart_items, bpp_id, bpp_uri, transaction_id
        ):
            captured["cart_items"] = cart_items
            captured["bpp_id"] = bpp_id
            captured["transaction_id"] = transaction_id
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "select_cart", fake_select_cart)

        resp = client.post(
            "/api/v1/cart/select",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "bpp_uri": "https://safiya.example.id/beckn",
                "items": [{"item_id": "sku-1", "qty": 2}],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cart_id"]
        assert body["status"] == "open"
        assert captured["cart_items"] == [{"sku_id": "sku-1", "qty": 2}]
        assert captured["bpp_id"] == "safiyafood.jaringan-dagang.id"

    def test_cart_select_requires_bot(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)
        resp = client.post(
            "/api/v1/cart/select",
            json={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "items": [{"item_id": "sku-1", "qty": 1}],
            },
        )
        assert resp.status_code == 401

    def test_get_cart_unknown_404(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)
        resp = client.get(
            "/api/v1/cart/00000000-0000-0000-0000-000000000000",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp.status_code == 404

    def test_cart_init_persists_billing_shipping_and_calls_init_order(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)

        async def fake_select_cart(**_):
            return True

        captured: dict = {}

        async def fake_init_order(
            *, cart_items, bpp_id, bpp_uri, transaction_id, billing, shipping_address
        ):
            captured["billing"] = billing
            captured["shipping_address"] = shipping_address
            captured["transaction_id"] = transaction_id
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "select_cart", fake_select_cart)
        monkeypatch.setattr(of, "init_order", fake_init_order)

        # First /select
        resp = client.post(
            "/api/v1/cart/select",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "items": [{"item_id": "sku-1", "qty": 1}],
            },
        )
        cart_id = resp.json()["cart_id"]

        # Then /init
        resp = client.post(
            f"/api/v1/cart/{cart_id}/init",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "billing": {"name": "Sari", "email": "s@s.id"},
                "shipping": {"city": "Jakarta", "postal_code": "12345"},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "drafted"
        assert captured["billing"]["name"] == "Sari"
        assert captured["shipping_address"]["postal_code"] == "12345"

    def test_get_cart_order_draft_assembled(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)

        async def noop(**_):
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "select_cart", noop)

        resp = client.post(
            "/api/v1/cart/select",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "items": [{"item_id": "sku-1", "qty": 1}],
            },
        )
        cart_id = resp.json()["cart_id"]

        resp = client.get(
            f"/api/v1/cart/{cart_id}/order-draft",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["bpp_id"] == "safiyafood.jaringan-dagang.id"
        assert body["items"] == [{"sku_id": "sku-1", "qty": 1}]

    def test_cart_items_merges_into_existing_cart(self, monkeypatch):
        """POST /cart/{id}/items appends/merges into the existing cart row
        (Beckn-compliant: re-fires /select with the union under the same
        transaction_id, instead of creating a fresh cart)."""
        client, _ = _build_test_client(monkeypatch)

        select_calls: list[dict] = []

        async def fake_select_cart(
            *, cart_items, bpp_id, bpp_uri, transaction_id
        ):
            select_calls.append({
                "cart_items": cart_items,
                "transaction_id": transaction_id,
            })
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "select_cart", fake_select_cart)

        # First /select to bootstrap the cart (500g).
        resp = client.post(
            "/api/v1/cart/select",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "items": [{"item_id": "saf-suk-500", "qty": 1}],
            },
        )
        cart_id = resp.json()["cart_id"]
        transaction_id = resp.json()["transaction_id"]

        # Now add the 1kg variant via the new merge endpoint.
        resp = client.post(
            f"/api/v1/cart/{cart_id}/items",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={"items": [{"item_id": "saf-suk-1000", "qty": 1}]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Same cart_id (stable across the buyer journey).
        assert body["cart_id"] == cart_id
        assert body["transaction_id"] == transaction_id
        assert body["status"] == "open"
        # Both items present (merged, not replaced).
        skus = sorted(it["sku_id"] for it in body["items"])
        assert skus == ["saf-suk-1000", "saf-suk-500"]

        # Beckn /select was re-fired with the union and same transaction_id.
        assert len(select_calls) == 2
        assert select_calls[1]["transaction_id"] == transaction_id
        merged = sorted(
            (it["sku_id"], it["qty"]) for it in select_calls[1]["cart_items"]
        )
        assert merged == [("saf-suk-1000", 1), ("saf-suk-500", 1)]

    def test_cart_items_sums_qty_for_duplicate_sku(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)

        async def noop(**_):
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "select_cart", noop)

        resp = client.post(
            "/api/v1/cart/select",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "items": [{"item_id": "sku-1", "qty": 2}],
            },
        )
        cart_id = resp.json()["cart_id"]

        resp = client.post(
            f"/api/v1/cart/{cart_id}/items",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={"items": [{"item_id": "sku-1", "qty": 3}]},
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["sku_id"] == "sku-1"
        assert items[0]["qty"] == 5

    def test_cart_items_clears_stale_quote(self, monkeypatch):
        """Adding items invalidates the previous /on_select quote — the BPP
        will provide a fresh one in response to the re-fired /select."""
        client, TestSession = _build_test_client(monkeypatch)

        async def noop(**_):
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "select_cart", noop)

        resp = client.post(
            "/api/v1/cart/select",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "items": [{"item_id": "sku-1", "qty": 1}],
            },
        )
        cart_id = resp.json()["cart_id"]

        # Pretend /on_select already populated a quote.
        from models.bot_rest import Cart
        from sqlalchemy import select as sa_select

        async def _stamp_quote():
            async with TestSession() as s:
                cart = (
                    await s.execute(sa_select(Cart).where(Cart.id == cart_id))
                ).scalar_one()
                cart.quote_json = {"total_idr": 104000}
                cart.quote_token = "stale-token"
                await s.commit()

        asyncio.run(_stamp_quote())

        resp = client.post(
            f"/api/v1/cart/{cart_id}/items",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={"items": [{"item_id": "sku-2", "qty": 1}]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # quote_json is rebuilt from items (fallback) but quote_token must be
        # cleared so the bot/UI knows the seller hasn't re-quoted yet.
        assert body["quote_token"] is None

    def test_cart_items_404_when_unknown(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)
        resp = client.post(
            "/api/v1/cart/00000000-0000-0000-0000-000000000000/items",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={"items": [{"item_id": "sku-1", "qty": 1}]},
        )
        assert resp.status_code == 404

    def test_cart_items_410_when_expired(self, monkeypatch):
        client, TestSession = _build_test_client(monkeypatch)

        async def noop(**_):
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "select_cart", noop)

        resp = client.post(
            "/api/v1/cart/select",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "items": [{"item_id": "sku-1", "qty": 1}],
            },
        )
        cart_id = resp.json()["cart_id"]

        from models.bot_rest import Cart
        from sqlalchemy import select as sa_select

        async def _expire():
            async with TestSession() as s:
                cart = (
                    await s.execute(sa_select(Cart).where(Cart.id == cart_id))
                ).scalar_one()
                cart.expires_at = datetime.now(timezone.utc) - timedelta(
                    minutes=1
                )
                await s.commit()

        asyncio.run(_expire())

        resp = client.post(
            f"/api/v1/cart/{cart_id}/items",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={"items": [{"item_id": "sku-2", "qty": 1}]},
        )
        assert resp.status_code == 410

    def test_cart_items_409_when_drafted_or_confirmed(self, monkeypatch):
        client, TestSession = _build_test_client(monkeypatch)

        async def noop(**_):
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "select_cart", noop)
        monkeypatch.setattr(of, "init_order", noop)

        resp = client.post(
            "/api/v1/cart/select",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "items": [{"item_id": "sku-1", "qty": 1}],
            },
        )
        cart_id = resp.json()["cart_id"]

        # Move cart to DRAFTED via /init.
        resp = client.post(
            f"/api/v1/cart/{cart_id}/init",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "billing": {"name": "Sari"},
                "shipping": {"city": "Jakarta"},
            },
        )
        assert resp.status_code == 200

        resp = client.post(
            f"/api/v1/cart/{cart_id}/items",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={"items": [{"item_id": "sku-2", "qty": 1}]},
        )
        # Once /init has been fired, mutating items requires a fresh
        # buyer-side decision (cancel + re-select) — guard with 409.
        assert resp.status_code == 409


class TestCheckoutEndpoints:
    def _make_cart(self, monkeypatch, client):
        async def noop(**_):
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "select_cart", noop)
        monkeypatch.setattr(of, "init_order", noop)

        resp = client.post(
            "/api/v1/cart/select",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "items": [{"item_id": "sku-1", "qty": 1}],
            },
        )
        cart_id = resp.json()["cart_id"]

        client.post(
            f"/api/v1/cart/{cart_id}/init",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={
                "billing": {"name": "Sari"},
                "shipping": {"city": "Jakarta"},
            },
        )
        return cart_id

    def test_checkout_confirm_creates_order_and_calls_confirm_order_v2(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)
        cart_id = self._make_cart(monkeypatch, client)

        captured: dict = {}

        async def fake_confirm(*, order_dict, quote_token=None):
            captured["order_dict"] = order_dict
            captured["quote_token"] = quote_token
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "confirm_order_v2", fake_confirm)

        resp = client.post(
            f"/api/v1/checkout/{cart_id}/confirm",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={"quote_token": "QT-1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cart_id"] == cart_id
        assert body["order_id"]
        assert body["status"] == "confirmed"
        assert captured["quote_token"] == "QT-1"
        assert captured["order_dict"]["bpp_id"] == "safiyafood.jaringan-dagang.id"

    def test_checkout_confirm_unknown_cart_404(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)
        resp = client.post(
            "/api/v1/checkout/00000000-0000-0000-0000-000000000000/confirm",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={},
        )
        assert resp.status_code == 404

    def test_checkout_confirm_already_confirmed_idempotent(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)
        cart_id = self._make_cart(monkeypatch, client)

        async def fake_confirm(**_):
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "confirm_order_v2", fake_confirm)

        r1 = client.post(
            f"/api/v1/checkout/{cart_id}/confirm",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={},
        )
        r2 = client.post(
            f"/api/v1/checkout/{cart_id}/confirm",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["order_id"] == r2.json()["order_id"]

    def test_checkout_status_pending(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)
        cart_id = self._make_cart(monkeypatch, client)

        async def fake_confirm(**_):
            return True

        import services.order_flow as of
        monkeypatch.setattr(of, "confirm_order_v2", fake_confirm)

        client.post(
            f"/api/v1/checkout/{cart_id}/confirm",
            headers={"Authorization": "Bearer test-bot-secret"},
            json={},
        )

        resp = client.get(
            f"/api/v1/checkout/{cart_id}/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["payment_state"] == "pending"
        assert body["status"] == "confirmed"

    def test_checkout_status_unknown_404(self, monkeypatch):
        client, _ = _build_test_client(monkeypatch)
        resp = client.get(
            "/api/v1/checkout/00000000-0000-0000-0000-000000000000/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp.status_code == 404


class _FakeSellerResp:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stand-in for ``httpx.AsyncClient`` that records the URL hit and
    returns a configured payload. Used to exercise the /status endpoint's
    lazy backfill without doing real network IO.
    """

    captured: dict = {}

    def __init__(
        self,
        payload: dict,
        status_code: int = 200,
        raises: Exception | None = None,
        **_,
    ):
        self._payload = payload
        self._status_code = status_code
        self._raises = raises

    @classmethod
    def factory(
        cls,
        payload: dict,
        status_code: int = 200,
        raises: Exception | None = None,
    ):
        cls.captured = {"calls": 0}

        def _ctor(*args, **kwargs):
            return cls(payload, status_code, raises, **kwargs)

        return _ctor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, url: str, *_, **__):
        self.__class__.captured["url"] = url
        self.__class__.captured["calls"] = (
            self.__class__.captured.get("calls", 0) + 1
        )
        if self._raises is not None:
            raise self._raises
        return _FakeSellerResp(self._payload, self._status_code)


class TestCheckoutStatusLazyBackfill:
    """The /status endpoint must keep BAP cart fields in sync with the
    BPP's authoritative payment view. Two drift sources matter:

      1. ``qr_image_url`` — fill on first /status hit after /confirm if the
         post-confirm backchannel poll missed it.
      2. ``payment_state`` — promote to ``"paid"`` whenever the seller's
         bot-facing payment endpoint reports ``payment_status="paid"``.
         The BPP learns of actual payment via Xendit webhook; without this
         pull, the BAP cart sits at ``"pending"`` forever.
    """

    def _seed_confirmed_cart(
        self,
        TestSession,
        *,
        payment_state: str = "pending",
        qr_image_url: str | None = None,
    ) -> str:
        import asyncio
        from models.bot_rest import Cart, CartStatus

        async def _seed():
            async with TestSession() as db:
                cart = Cart(
                    id=str(uuid.uuid4()),
                    bpp_id="safiyafood.jaringan-dagang.id",
                    items_json=[{"sku_id": "sku-1", "qty": 1}],
                    transaction_id=str(uuid.uuid4()),
                    order_id=str(uuid.uuid4()),
                    status=CartStatus.CONFIRMED,
                    payment_state=payment_state,
                    qr_image_url=qr_image_url,
                )
                db.add(cart)
                await db.commit()
                return cart.id

        return asyncio.run(_seed())

    def _read_cart(self, TestSession, cart_id: str):
        import asyncio
        from sqlalchemy import select
        from models.bot_rest import Cart

        async def _read():
            async with TestSession() as db:
                got = (
                    await db.execute(select(Cart).where(Cart.id == cart_id))
                ).scalar_one()
                return got.payment_state, got.qr_image_url

        return asyncio.run(_read())

    def test_promotes_payment_state_to_paid_when_seller_reports_paid(
        self, monkeypatch
    ):
        """Regression for the original bug report: BPP says paid but BAP
        cart stuck at pending. /status must pick up ``payment_status="paid"``
        from the seller and flip ``cart.payment_state``.
        """
        import httpx

        client, TestSession = _build_test_client(monkeypatch)
        cart_id = self._seed_confirmed_cart(
            TestSession,
            payment_state="pending",
            qr_image_url="https://existing.example.id/qr.png",
        )

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _FakeAsyncClient.factory(
                {
                    "invoice_url": "https://existing.example.id/qr.png",
                    "payment_status": "paid",
                }
            ),
        )

        resp = client.get(
            f"/api/v1/checkout/{cart_id}/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["payment_state"] == "paid"

        state, _ = self._read_cart(TestSession, cart_id)
        assert state == "paid"

    def test_polls_seller_even_when_qr_image_already_present(self, monkeypatch):
        """The old gate ``not cart.qr_image_url`` caused the seller poll
        to skip once the QR was filled — locking ``payment_state`` at
        whatever value was there. The gate must also fire while
        payment_state is still ``pending``.
        """
        import httpx

        client, TestSession = _build_test_client(monkeypatch)
        cart_id = self._seed_confirmed_cart(
            TestSession,
            payment_state="pending",
            qr_image_url="https://already-filled.example.id/qr.png",
        )

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _FakeAsyncClient.factory({"payment_status": "paid"}),
        )

        client.get(
            f"/api/v1/checkout/{cart_id}/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert "url" in _FakeAsyncClient.captured, (
            "Seller payment endpoint must be polled even when qr_image_url "
            "is already filled, otherwise payment_state stays stuck."
        )

    def test_keeps_pending_when_seller_reports_pending(self, monkeypatch):
        """If the BPP hasn't seen payment yet, BAP must not flip to paid."""
        import httpx

        client, TestSession = _build_test_client(monkeypatch)
        cart_id = self._seed_confirmed_cart(TestSession, payment_state="pending")

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _FakeAsyncClient.factory(
                {
                    "invoice_url": "https://seller.example.id/qr.png",
                    "payment_status": "pending",
                }
            ),
        )

        resp = client.get(
            f"/api/v1/checkout/{cart_id}/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp.json()["payment_state"] == "pending"

    def test_does_not_overwrite_terminal_states(self, monkeypatch):
        """A cart that's already ``cancelled`` or ``expired`` must stay
        terminal even if the seller (erroneously) reports paid.
        """
        import httpx

        client, TestSession = _build_test_client(monkeypatch)
        cart_id = self._seed_confirmed_cart(
            TestSession,
            payment_state="expired",
        )

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _FakeAsyncClient.factory({"payment_status": "paid"}),
        )

        resp = client.get(
            f"/api/v1/checkout/{cart_id}/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        # /status doesn't issue 4xx for terminal carts; it just reports.
        assert resp.status_code == 200
        state, _ = self._read_cart(TestSession, cart_id)
        assert state == "expired"

    def test_fills_qr_image_url_when_missing(self, monkeypatch):
        """The original lazy-backfill behaviour still works: a cart with
        no qr_image_url gets it filled from the seller payload.
        """
        import httpx

        client, TestSession = _build_test_client(monkeypatch)
        cart_id = self._seed_confirmed_cart(
            TestSession,
            payment_state="pending",
            qr_image_url=None,
        )

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _FakeAsyncClient.factory(
                {
                    "invoice_url": "https://seller.example.id/qr.png",
                    "payment_status": "pending",
                }
            ),
        )

        resp = client.get(
            f"/api/v1/checkout/{cart_id}/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp.json()["qr_image_url"] == "https://seller.example.id/qr.png"

    def test_seller_network_error_does_not_500(self, monkeypatch):
        """If the seller endpoint is unreachable or times out, /status must
        still return 200 with whatever local state we have. The backfill is
        best-effort — a flaky BPP must not break the bot's status poll.
        """
        import httpx

        client, TestSession = _build_test_client(monkeypatch)
        cart_id = self._seed_confirmed_cart(TestSession, payment_state="pending")

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _FakeAsyncClient.factory(
                {}, raises=httpx.ConnectError("simulated network failure")
            ),
        )

        resp = client.get(
            f"/api/v1/checkout/{cart_id}/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["payment_state"] == "pending"

    def test_missing_payment_status_field_leaves_state_pending(self, monkeypatch):
        """The seller may return a payload without ``payment_status`` (older
        BPP version, partial response). We must not crash and must not
        promote ``pending`` → ``paid`` based on absence.
        """
        import httpx

        client, TestSession = _build_test_client(monkeypatch)
        cart_id = self._seed_confirmed_cart(TestSession, payment_state="pending")

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _FakeAsyncClient.factory(
                {"invoice_url": "https://seller.example.id/qr.png"},
            ),
        )

        resp = client.get(
            f"/api/v1/checkout/{cart_id}/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["payment_state"] == "pending"

    def test_idempotent_no_repoll_once_paid(self, monkeypatch):
        """Once payment_state has been promoted to ``paid``, subsequent
        /status hits must NOT keep polling the seller (the gate condition
        ``needs_url or needs_paid`` is both false). This bounds the load
        the bot's poller puts on the BPP's payment endpoint.
        """
        import httpx

        client, TestSession = _build_test_client(monkeypatch)
        cart_id = self._seed_confirmed_cart(
            TestSession,
            payment_state="pending",
            qr_image_url="https://seller.example.id/qr.png",
        )

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _FakeAsyncClient.factory({"payment_status": "paid"}),
        )

        # First hit: should promote pending → paid (1 seller call).
        client.get(
            f"/api/v1/checkout/{cart_id}/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert _FakeAsyncClient.captured["calls"] == 1

        # Second hit: cart is now paid + qr_image_url present, so the
        # gate is false on both legs — no further seller poll.
        client.get(
            f"/api/v1/checkout/{cart_id}/status",
            headers={"Authorization": "Bearer test-bot-secret"},
        )
        assert _FakeAsyncClient.captured["calls"] == 1, (
            "Expected no extra seller poll once payment_state=paid and "
            "qr_image_url is present; got "
            f"{_FakeAsyncClient.captured['calls']} total calls."
        )


# --- 4. Search envelope (pure builder) ---------------------------------


class TestCartOrderIdNoForeignKey:
    """The synthetic ``Cart.order_id`` MUST NOT have a FK to ``orders.id``.

    The bot doesn't materialize a real ``models.order.Order`` row (no
    Firebase ``profile_id``); the synthetic uuid we mint at /confirm time
    would trip ``IntegrityError`` on Postgres if the FK were enforced.
    SQLite (used in tests) silently ignores FK constraints by default,
    which is what masked the bug in the original B3a landing.
    """

    def test_cart_order_id_has_no_foreign_key(self):
        from models.bot_rest import Cart

        col = Cart.__table__.columns["order_id"]
        # No FK constraints at all on this column.
        assert list(col.foreign_keys) == [], (
            "Cart.order_id MUST NOT have a ForeignKey — it's a synthetic "
            "bot-side id, no matching orders.id row exists. "
            f"Got: {list(col.foreign_keys)!r}"
        )

    def test_migration_script_drops_legacy_fk_and_omits_new_fk(self):
        """The dry-run output must NOT create the FK and MUST drop it.

        Forward-deploy idempotency: ``ALTER TABLE ... DROP CONSTRAINT
        IF EXISTS bot_carts_order_id_fkey`` is a no-op on fresh installs
        but cleans up environments where a prior script run created it.
        """
        import importlib.util
        import io
        import contextlib

        # The migration script lives at ``scripts/add-bot-rest-tables.py``
        # — the dash means we can't ``import scripts.add_bot_rest_tables``
        # directly. Use importlib so the test does the same dry-run an
        # operator invokes from the CLI.
        script_path = os.path.join(
            _BAP_DIR, "scripts", "add-bot-rest-tables.py"
        )
        spec = importlib.util.spec_from_file_location(
            "_bot_rest_migration_module", script_path
        )
        assert spec is not None and spec.loader is not None
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mig.print_dry_run_sql()
        out = buf.getvalue()

        assert "REFERENCES orders(id)" not in out, (
            "Migration must not declare a FK from bot_carts.order_id to "
            "orders.id. The bot mints synthetic order ids; there is no "
            "matching orders row."
        )
        assert (
            "DROP CONSTRAINT IF EXISTS bot_carts_order_id_fkey" in out
        ), (
            "Migration must drop a legacy bot_carts_order_id_fkey if it "
            "exists, so environments where a prior script created it "
            "don't keep tripping IntegrityError."
        )


# --- 5. handle_on_confirm linkage to bot Cart --------------------------


class TestHandleOnConfirmCartLinkage:
    """Critical #2: ``handle_on_confirm`` MUST update the bot Cart row
    so the MCP polling ``GET /api/v1/checkout/{cart_id}/status`` sees the
    QR + ``payment_state="paid"`` instead of stuck-pending forever.
    """

    def _setup_db(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        sqlite_url = "sqlite+aiosqlite:///:memory:"
        engine = create_async_engine(sqlite_url)
        Session = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

        from models.base import Base
        import models  # noqa: F401  (registers all models)
        from models.bot_rest import Cart, SearchSession

        async def _create():
            async with engine.begin() as conn:
                await conn.run_sync(
                    Base.metadata.create_all,
                    tables=[SearchSession.__table__, Cart.__table__],
                    checkfirst=True,
                )
                # The on_confirm handler also queries ``orders`` (storefront
                # path). The real Order model uses Postgres-only JSONB, so we
                # can't ``create_all`` it under sqlite. We only need the
                # SELECT ... WHERE seller_order_ref = ? path to NOT crash —
                # the handler treats "no row" as "no storefront order to
                # update, only bot cart". A minimal stub table is enough.
                await conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS orders ("
                    "  id VARCHAR(36) PRIMARY KEY, "
                    "  profile_id VARCHAR(36), "
                    "  brand_id VARCHAR(36), "
                    "  state VARCHAR(40), "
                    "  items TEXT, "
                    "  subtotal_idr BIGINT, "
                    "  shipping_idr BIGINT, "
                    "  fee_idr BIGINT, "
                    "  total_idr BIGINT, "
                    "  shipping_address TEXT, "
                    "  payment_method_snapshot TEXT, "
                    "  bap_id VARCHAR(255), "
                    "  bpp_id VARCHAR(255), "
                    "  seller_order_ref VARCHAR(255), "
                    "  shipped_simulated_at TIMESTAMP, "
                    "  delivered_simulated_at TIMESTAMP, "
                    "  auto_release_at TIMESTAMP, "
                    "  released_at TIMESTAMP, "
                    "  fulfillment_status VARCHAR(40), "
                    "  fulfillment_awb VARCHAR(255), "
                    "  fulfillment_tracking_url VARCHAR(512), "
                    "  fulfillment_last_event_at TIMESTAMP, "
                    # A6 RSP settlement columns on Order
                    "  settlement_status VARCHAR(40), "
                    "  settlement_basis VARCHAR(40), "
                    "  settlement_window VARCHAR(20), "
                    "  settlement_reference VARCHAR(255), "
                    "  created_at TIMESTAMP, "
                    "  updated_at TIMESTAMP"
                    ")"
                ))

        asyncio.run(_create())
        return Session

    def test_on_confirm_with_qr_updates_matching_cart(self):
        from models.bot_rest import Cart, CartStatus
        from routers.beckn_handlers import handle_on_confirm

        Session = self._setup_db()
        txn_id = "txn-confirm-1"

        async def seed():
            async with Session() as db:
                cart = Cart(
                    id=str(uuid.uuid4()),
                    bpp_id="safiyafood.jaringan-dagang.id",
                    items_json=[{"sku_id": "sku-1", "qty": 1}],
                    transaction_id=txn_id,
                    status=CartStatus.CONFIRMED,
                    payment_state="pending",
                )
                db.add(cart)
                await db.commit()
                return cart.id

        cart_id = asyncio.run(seed())

        payload_message = {
            "order": {
                "id": "seller-order-AAA",
                "payments": [
                    {
                        "params": {
                            "qr_image_url": "https://qr.example.id/abc.png",
                        }
                    }
                ],
            }
        }
        context = {"transaction_id": txn_id, "bpp_id": "x"}

        async def call_and_read():
            async with Session() as db:
                await handle_on_confirm(context, payload_message, db)
                await db.commit()
            async with Session() as db:
                from sqlalchemy import select
                got = (
                    await db.execute(select(Cart).where(Cart.id == cart_id))
                ).scalar_one()
                return got.qr_image_url, got.payment_state

        qr, state = asyncio.run(call_and_read())
        assert qr == "https://qr.example.id/abc.png"
        assert state == "paid"

    def test_on_confirm_no_matching_cart_does_not_error(self):
        """Firebase-storefront flow: customer ordered direct, no Cart row.

        Handler must succeed and just be a no-op on the Cart side.
        """
        from routers.beckn_handlers import handle_on_confirm

        Session = self._setup_db()

        payload_message = {
            "order": {
                "id": "seller-order-no-cart",
                "payments": [
                    {"params": {"qr_image_url": "https://qr.example.id/x.png"}}
                ],
            }
        }
        context = {"transaction_id": "txn-no-cart", "bpp_id": "x"}

        async def call():
            async with Session() as db:
                # Must not raise.
                result = await handle_on_confirm(context, payload_message, db)
                await db.commit()
                return result

        assert asyncio.run(call()) is None

    def test_on_confirm_does_not_reflip_expired_cart(self):
        """Idempotency / safety: once payment_state has moved to a terminal
        non-paid state (e.g. ``expired``), a late /on_confirm retry must
        NOT flip it back to ``paid``.
        """
        from models.bot_rest import Cart, CartStatus
        from routers.beckn_handlers import handle_on_confirm

        Session = self._setup_db()
        txn_id = "txn-expired-1"

        async def seed():
            async with Session() as db:
                cart = Cart(
                    id=str(uuid.uuid4()),
                    bpp_id="x",
                    items_json=[{"sku_id": "sku-1", "qty": 1}],
                    transaction_id=txn_id,
                    status=CartStatus.EXPIRED,
                    payment_state="expired",
                )
                db.add(cart)
                await db.commit()
                return cart.id

        cart_id = asyncio.run(seed())

        payload_message = {
            "order": {
                "id": "seller-order-late",
                "payments": [
                    {"params": {"qr_image_url": "https://qr.example.id/late.png"}}
                ],
            }
        }
        context = {"transaction_id": txn_id, "bpp_id": "x"}

        async def call_and_read():
            async with Session() as db:
                await handle_on_confirm(context, payload_message, db)
                await db.commit()
            async with Session() as db:
                from sqlalchemy import select
                got = (
                    await db.execute(select(Cart).where(Cart.id == cart_id))
                ).scalar_one()
                return got.qr_image_url, got.payment_state

        qr, state = asyncio.run(call_and_read())
        # payment_state stays expired, NOT re-flipped to "paid".
        assert state == "expired"
        # qr_image_url was not touched because we early-return on EXPIRED
        # CartStatus.
        assert qr is None

    def test_on_confirm_idempotent_on_paid_cart(self):
        """A second /on_confirm for the same txn must be a no-op flip
        (re-setting the same paid state with same QR is fine).
        """
        from models.bot_rest import Cart, CartStatus
        from routers.beckn_handlers import handle_on_confirm

        Session = self._setup_db()
        txn_id = "txn-retry-1"

        async def seed():
            async with Session() as db:
                cart = Cart(
                    id=str(uuid.uuid4()),
                    bpp_id="x",
                    items_json=[{"sku_id": "sku-1", "qty": 1}],
                    transaction_id=txn_id,
                    status=CartStatus.CONFIRMED,
                    payment_state="paid",
                    qr_image_url="https://qr.example.id/first.png",
                )
                db.add(cart)
                await db.commit()
                return cart.id

        cart_id = asyncio.run(seed())

        payload_message = {
            "order": {
                "id": "seller-order-AAA",
                "payments": [
                    {
                        "params": {
                            "qr_image_url": "https://qr.example.id/first.png",
                        }
                    }
                ],
            }
        }
        context = {"transaction_id": txn_id, "bpp_id": "x"}

        async def call_and_read():
            async with Session() as db:
                await handle_on_confirm(context, payload_message, db)
                await db.commit()
            async with Session() as db:
                from sqlalchemy import select
                got = (
                    await db.execute(select(Cart).where(Cart.id == cart_id))
                ).scalar_one()
                return got.qr_image_url, got.payment_state

        qr, state = asyncio.run(call_and_read())
        assert qr == "https://qr.example.id/first.png"
        assert state == "paid"


class TestSearchEnvelopeBuilder:
    def test_search_envelope_includes_bpp_and_action(self):
        from services.order_flow import build_search_envelope

        env = build_search_envelope(
            bpp_id="safiyafood.jaringan-dagang.id",
            bpp_uri="https://safiya.example.id/beckn",
            query="matcha",
            transaction_id="txn-search-1",
        )
        ctx = env["context"]
        assert ctx["action"] == "search"
        assert ctx["bpp_id"] == "safiyafood.jaringan-dagang.id"
        assert ctx["bap_id"] == "beli-aman.bap.jaringan-dagang.id"
        assert ctx["domain"] == "ONDC:RET11"
        assert env["message"]["intent"]["item"]["descriptor"]["name"] == "matcha"

    def test_search_envelope_carries_category(self):
        from services.order_flow import build_search_envelope

        env = build_search_envelope(
            bpp_id="safiyafood.jaringan-dagang.id",
            bpp_uri="https://safiya.example.id/beckn",
            query="latte",
            category="beverages",
        )
        intent = env["message"]["intent"]
        assert intent["category"]["descriptor"]["name"] == "beverages"

    def test_search_envelope_city_override(self):
        from services.order_flow import build_search_envelope

        env = build_search_envelope(
            bpp_id="safiyafood.jaringan-dagang.id",
            bpp_uri="https://safiya.example.id/beckn",
            query="bread",
            city="std:022",
        )
        assert env["context"]["city"] == "std:022"
