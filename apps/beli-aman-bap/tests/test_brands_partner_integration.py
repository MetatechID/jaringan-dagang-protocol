"""Internal partner credential reads; all credentials below are placeholders."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import deps  # noqa: E402
from database import get_db  # noqa: E402
from routers.brands import router  # noqa: E402

PATH = "/api/v1/brands/internal/partner-integration"
TOKEN = "placeholder-admin-token"
BPP_ID = "placeholder.bpp.example"
HEADERS = {"X-Admin-Token": TOKEN}
METADATA = {
    "brand_id": "placeholder-brand-id",
    "brand_slug": "placeholder-brand",
    "bpp_id": BPP_ID,
    "payment_provider": "dipay",
    "callback_url": "https://partner.example/callback",
    "api_key_configured": True,
    "webhook_secret_configured": True,
}


def brand(**overrides):
    values = dict(
        id=METADATA["brand_id"], slug=METADATA["brand_slug"],
        name="Placeholder", bpp_id=BPP_ID, payment_provider="dipay",
        partner_callback_url=METADATA["callback_url"],
        partner_api_key="placeholder-partner-key",
        partner_callback_secret="placeholder-webhook-secret",
        dipay_client_secret="placeholder-unrelated-secret",
        default_warehouse_address=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeDB:
    def __init__(self, brands):
        self.brands = brands
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        params = statement.compile().params
        bpp_id = params.get("bpp_id_1")
        rows = self.brands if bpp_id is None else [
            b for b in self.brands if b.bpp_id == bpp_id
        ][:2]
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            scalar_one_or_none=lambda: rows[0] if rows else None,
        )


@pytest.fixture
def client_factory(monkeypatch):
    monkeypatch.setattr(deps.settings, "admin_token", TOKEN)

    def build(brands=None):
        db = FakeDB([brand()] if brands is None else brands)
        app = FastAPI()
        app.include_router(router)

        async def override_db():
            yield db

        app.dependency_overrides[get_db] = override_db
        return TestClient(app), db

    return build


def request(client, method, *, bpp_id=BPP_ID, fields=None, headers=HEADERS):
    if method == "GET":
        return client.get(PATH, params={"bpp_id": bpp_id}, headers=headers)
    return client.post(PATH + "/reveal", json={
        "bpp_id": bpp_id, "fields": fields or ["api_key"],
    }, headers=headers)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong-placeholder"}])
def test_auth_rejects_before_lookup(client_factory, method, headers):
    client, db = client_factory()
    response = request(client, method, headers=headers)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert not db.statements


def test_guard_uses_constant_time_comparison(client_factory, monkeypatch):
    client, _ = client_factory()
    compare = Mock(wraps=deps.hmac.compare_digest)
    monkeypatch.setattr(deps.hmac, "compare_digest", compare)
    assert request(client, "GET").status_code == 200
    compare.assert_called_once_with(TOKEN.encode(), TOKEN.encode())


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unconfigured_admin_token_fails_closed(client_factory, monkeypatch, method):
    client, db = client_factory()
    monkeypatch.setattr(deps.settings, "admin_token", "")
    assert request(client, method).status_code == 403
    assert not db.statements


def test_metadata_is_allowlisted_and_masks_secrets(client_factory):
    client, _ = client_factory()
    response = request(client, "GET")
    assert response.status_code == 200
    assert response.json() == METADATA
    assert response.headers["cache-control"] == "no-store"
    assert "placeholder-partner-key" not in response.text
    assert "placeholder-webhook-secret" not in response.text


@pytest.mark.parametrize("fields", [["api_key"], ["webhook_secret"],
                                    ["api_key", "webhook_secret"]])
def test_reveal_only_requested_fields(client_factory, fields):
    client, _ = client_factory()
    response = request(client, "POST", fields=fields)
    expected = dict(METADATA)
    secrets = {"api_key": "placeholder-partner-key",
               "webhook_secret": "placeholder-webhook-secret"}
    expected.update({field: secrets[field] for field in fields})
    assert response.status_code == 200
    assert response.json() == expected
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("absent", [None, ""])
def test_missing_secrets_are_null_and_unconfigured(client_factory, absent):
    client, _ = client_factory([brand(
        partner_api_key=absent, partner_callback_secret=absent,
        partner_callback_url=None,
    )])
    response = request(client, "POST", fields=["api_key", "webhook_secret"])
    assert response.json() == {
        **METADATA, "callback_url": None, "api_key_configured": False,
        "webhook_secret_configured": False, "api_key": None, "webhook_secret": None,
    }


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("bpp_id", ["missing.example", "placeholder-brand",
                                    BPP_ID.upper(), BPP_ID + " ", "placeholder"])
def test_mapping_requires_exact_bpp_id(client_factory, method, bpp_id):
    client, db = client_factory()
    response = request(client, method, bpp_id=bpp_id)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    statement = db.statements[0]
    assert statement.compile().params["bpp_id_1"] == bpp_id
    assert "brands.bpp_id =" in str(statement)
    assert "LIMIT" in str(statement)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_ambiguous_mapping_fails_closed(client_factory, method):
    client, _ = client_factory([brand(), brand(id="other-placeholder-id")])
    response = request(client, method)
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert "placeholder-partner-key" not in response.text


@pytest.mark.parametrize("body", [
    {}, {"bpp_id": BPP_ID}, {"fields": ["api_key"]},
    {"bpp_id": "", "fields": ["api_key"]},
    {"bpp_id": 123, "fields": ["api_key"]},
    {"bpp_id": BPP_ID, "fields": []},
    {"bpp_id": BPP_ID, "fields": ["api_key", "api_key"]},
    {"bpp_id": BPP_ID, "fields": ["api_key", "webhook_secret", "api_key"]},
    {"bpp_id": BPP_ID, "fields": ["partner_api_key"]},
    {"bpp_id": BPP_ID, "fields": ["dipay_client_secret"]},
    {"bpp_id": BPP_ID, "fields": "api_key"},
    {"bpp_id": BPP_ID, "fields": None},
    {"bpp_id": BPP_ID, "fields": ["api_key"], "extra": True},
])
def test_invalid_reveal_body_rejected(client_factory, body):
    client, db = client_factory()
    response = client.post(PATH + "/reveal", json=body, headers=HEADERS)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert not db.statements


def test_validation_error_does_not_echo_submitted_values(client_factory):
    client, db = client_factory()
    submitted_secret = "placeholder-sensitive-submitted-value"
    response = client.post(PATH + "/reveal", json={
        "bpp_id": BPP_ID, "fields": [submitted_secret],
        "api_key": submitted_secret,
    }, headers=HEADERS)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request"}
    assert submitted_secret not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert not db.statements


@pytest.mark.parametrize("params", [{}, {"bpp_id": ""}])
def test_missing_query_rejected(client_factory, params):
    client, db = client_factory()
    response = client.get(PATH, params=params, headers=HEADERS)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert not db.statements


@pytest.mark.parametrize("path", ["/api/v1/brands", "/api/v1/brands/placeholder-brand"])
def test_public_brand_serialization_does_not_expose_secrets(client_factory, path):
    client, _ = client_factory()
    response = client.get(path)
    assert response.status_code == 200
    rows = response.json() if isinstance(response.json(), list) else [response.json()]
    for row in rows:
        assert set(row) <= {"id", "slug", "name", "bpp_id", "default_warehouse_address"}
    assert "placeholder-partner-key" not in response.text
    assert "placeholder-webhook-secret" not in response.text
