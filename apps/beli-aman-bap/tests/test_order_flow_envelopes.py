"""Task A4 — buyer order_flow envelope builders + flag resolution.

Covers the pure envelope-building functions in ``services/order_flow.py``
without doing real network IO. The send_beckn_request layer is exercised
through the existing A2b/A3 tests.
"""

from __future__ import annotations

import os
import sys

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

from services import order_flow  # noqa: E402


class TestFlagResolver:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("BECKN_ORDER_FLOW", raising=False)
        assert order_flow.beckn_order_flow_mode() == "off"

    def test_off_explicit(self, monkeypatch):
        monkeypatch.setenv("BECKN_ORDER_FLOW", "off")
        assert order_flow.beckn_order_flow_mode() == "off"

    def test_shadow(self, monkeypatch):
        monkeypatch.setenv("BECKN_ORDER_FLOW", "shadow")
        assert order_flow.beckn_order_flow_mode() == "shadow"

    def test_on(self, monkeypatch):
        monkeypatch.setenv("BECKN_ORDER_FLOW", "on")
        assert order_flow.beckn_order_flow_mode() == "on"

    def test_bogus_falls_back_to_off(self, monkeypatch):
        monkeypatch.setenv("BECKN_ORDER_FLOW", "always-please")
        assert order_flow.beckn_order_flow_mode() == "off"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("BECKN_ORDER_FLOW", "SHADOW")
        assert order_flow.beckn_order_flow_mode() == "shadow"


class TestSelectEnvelope:
    def test_includes_canonical_action_and_bpp(self):
        env = order_flow.build_select_envelope(
            cart_items=[{"sku_id": "sku-A", "qty": 2}],
            bpp_id="safiyafood.jaringan-dagang.id",
            bpp_uri="https://safiya.example.id/beckn",
        )
        ctx = env["context"]
        assert ctx["action"] == "select"
        assert ctx["bpp_id"] == "safiyafood.jaringan-dagang.id"
        assert ctx["bap_id"] == "beli-aman.bap.jaringan-dagang.id"
        assert ctx["bpp_uri"] == "https://safiya.example.id/beckn"
        # ONDC domain for Safiya is RET11 (A2b).
        assert ctx["domain"] == "ONDC:RET11"

    def test_translates_cart_items_to_beckn_shape(self):
        env = order_flow.build_select_envelope(
            cart_items=[{"sku_id": "sku-A", "qty": 3}],
            bpp_id="safiyafood.jaringan-dagang.id",
            bpp_uri="https://safiya.example.id/beckn",
        )
        items = env["message"]["order"]["items"]
        assert items == [
            {"id": "sku-A", "quantity": {"selected": {"count": 3}}}
        ]


class TestInitEnvelope:
    def test_carries_billing_and_address(self):
        env = order_flow.build_init_envelope(
            cart_items=[{"sku_id": "sku-A", "qty": 1}],
            bpp_id="safiyafood.jaringan-dagang.id",
            bpp_uri="https://safiya.example.id/beckn",
            transaction_id="t-1",
            billing={"name": "Sari", "email": "s@s.id"},
            shipping_address={"city": "Jakarta", "postal_code": "12345"},
        )
        order = env["message"]["order"]
        assert order["billing"] == {"name": "Sari", "email": "s@s.id"}
        ship_end = order["fulfillments"][0]["end"]
        assert ship_end == {
            "location": {"address": {"city": "Jakarta", "postal_code": "12345"}}
        }
        assert env["context"]["action"] == "init"
        assert env["context"]["transaction_id"] == "t-1"

    def test_no_shipping_address_keeps_empty_end(self):
        env = order_flow.build_init_envelope(
            cart_items=[{"sku_id": "sku-A", "qty": 1}],
            bpp_id="safiyafood.jaringan-dagang.id",
            bpp_uri="https://safiya.example.id/beckn",
            transaction_id="t-2",
            billing={"name": "Sari"},
            shipping_address=None,
        )
        assert env["message"]["order"]["fulfillments"][0]["end"] == {}


class TestConfirmEnvelope:
    def test_echoes_quote_token_when_provided(self):
        env = order_flow.build_confirm_envelope(
            order_dict={
                "order_id": "ord-1",
                "items": [{"sku_id": "sku-A", "qty": 1}],
                "buyer": {"email": "b@b.id"},
                "total_idr": 25000,
                "shipping_address": {"city": "Jakarta"},
                "escrow_status": "held",
            },
            bpp_id="safiyafood.jaringan-dagang.id",
            bpp_uri="https://safiya.example.id/beckn",
            transaction_id="t-3",
            quote_token="QUOTE-OPAQUE-STRING",
        )
        tag_codes = {t["code"]: t for t in env["message"]["order"]["tags"]}
        assert "quote_token" in tag_codes
        assert (
            tag_codes["quote_token"]["list"][0]["value"]
            == "QUOTE-OPAQUE-STRING"
        )

    def test_omits_quote_token_tag_when_none(self):
        env = order_flow.build_confirm_envelope(
            order_dict={
                "order_id": "ord-1",
                "items": [{"sku_id": "sku-A", "qty": 1}],
                "buyer": {},
                "total_idr": 25000,
                "shipping_address": None,
                "escrow_status": "held",
            },
            bpp_id="safiyafood.jaringan-dagang.id",
            bpp_uri="https://safiya.example.id/beckn",
            transaction_id="t-4",
            quote_token=None,
        )
        tag_codes = {t["code"] for t in env["message"]["order"]["tags"]}
        assert "quote_token" not in tag_codes
        assert "escrow_status" in tag_codes  # baseline tag still emitted

    def test_total_and_currency_serialized(self):
        env = order_flow.build_confirm_envelope(
            order_dict={
                "order_id": "ord-1",
                "items": [],
                "total_idr": 35000,
                "escrow_status": "held",
            },
            bpp_id="safiyafood.jaringan-dagang.id",
            bpp_uri="https://safiya.example.id/beckn",
            transaction_id="t-5",
        )
        order = env["message"]["order"]
        assert order["quote"]["price"]["value"] == "35000"
        assert order["quote"]["price"]["currency"] == "IDR"
        assert order["payments"][0]["params"]["amount"] == "35000"


class TestResponseExtraction:
    def test_extract_quote_token_from_payload(self):
        payload = {
            "message": {
                "order": {
                    "tags": [
                        {
                            "code": "quote_token",
                            "list": [{"code": "value", "value": "QT-TOKEN-XYZ"}],
                        }
                    ]
                }
            }
        }
        assert order_flow.extract_quote_token(payload) == "QT-TOKEN-XYZ"

    def test_extract_quote_token_missing(self):
        assert order_flow.extract_quote_token({"message": {"order": {}}}) is None
        assert order_flow.extract_quote_token({}) is None

    def test_extract_qr_image_url_prefers_qr_image_url(self):
        payload = {
            "message": {
                "order": {
                    "payments": [
                        {
                            "params": {
                                "qr_image_url": "https://qr.example.id/1.png",
                                "invoice_url": "https://invoice.example.id/1",
                            }
                        }
                    ]
                }
            }
        }
        assert (
            order_flow.extract_qr_image_url(payload)
            == "https://qr.example.id/1.png"
        )

    def test_extract_qr_image_url_falls_through_to_invoice_url(self):
        payload = {
            "message": {
                "order": {
                    "payments": [
                        {"params": {"invoice_url": "https://invoice.example.id/1"}}
                    ]
                }
            }
        }
        assert (
            order_flow.extract_qr_image_url(payload)
            == "https://invoice.example.id/1"
        )

    def test_extract_qr_image_url_missing(self):
        assert order_flow.extract_qr_image_url({"message": {"order": {}}}) is None
