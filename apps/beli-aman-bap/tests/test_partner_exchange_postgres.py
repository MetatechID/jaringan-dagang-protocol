"""Opt-in disposable Postgres checks. Never set AUDIT_TEST_DATABASE_URL to a live DB."""
import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
URL = os.getenv("AUDIT_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not URL, reason="requires disposable AUDIT_TEST_DATABASE_URL")]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, APP / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD_DDL = """CREATE TABLE webhook_audits (
 id VARCHAR(36) PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ, duration_ms INTEGER,
 direction VARCHAR(16) NOT NULL, integration VARCHAR(32) NOT NULL,
 operation VARCHAR(64) NOT NULL, method VARCHAR(16) NOT NULL,
 endpoint VARCHAR(2048) NOT NULL, status INTEGER, outcome VARCHAR(32) NOT NULL,
 error VARCHAR(64), attempt INTEGER, delivery_id VARCHAR(36),
 brand_id VARCHAR(36), partner_order_id VARCHAR(36), external_order_id VARCHAR(255),
 dipay_reference VARCHAR(255), request JSONB NOT NULL, response JSONB NOT NULL,
 request_body_raw BYTEA, response_body_raw BYTEA, request_body_json JSONB, response_body_json JSONB
);
CREATE INDEX ix_webhook_audits_started_at ON webhook_audits (started_at);"""


def test_migration_rename_schema_match_writes_and_retention(monkeypatch):
    import services.partner_exchange as audit
    from services.partner_exchange import Capture
    migration = load_script("add-partner-exchanges")
    pruning = load_script("prune-partner-exchanges")

    async def run():
        engine = create_async_engine(URL, pool_size=1, max_overflow=2)
        # The table as installed on staging today, with one row; upgrade twice.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS webhook_audits, partner_exchanges, obs_spans"))
            for statement in OLD_DDL.split(";"):
                if statement.strip():
                    await conn.execute(text(statement))
            await conn.execute(text(
                "INSERT INTO webhook_audits (id, started_at, direction, integration, operation, method,"
                " endpoint, outcome, request, response) VALUES ('old-1', now(), 'inbound', 'dipay',"
                " 'qris_notify', 'POST', '/x', 'response', '{}', '{}')"))
        await migration.apply_migration(URL)
        await migration.apply_migration(URL)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('public.webhook_audits')")) is None
            assert await conn.scalar(text("SELECT id FROM partner_exchanges")) == "old-1"
            assert await conn.scalar(text("SELECT redacted FROM partner_exchanges")) is False
            indexes = set((await conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename='partner_exchanges'"))).scalars())
            assert "ix_partner_exchanges_started_at" in indexes and "partner_exchanges_pkey" in indexes
            assert not any("webhook_audits" in name for name in indexes)

        # /ready compares the ORM models with the live schema: they must match.
        from types import SimpleNamespace
        import docker_schema
        from models import Base
        tables = [Base.metadata.tables["partner_exchanges"], Base.metadata.tables["obs_spans"]]
        monkeypatch.setattr(docker_schema, "Base", SimpleNamespace(metadata=SimpleNamespace(sorted_tables=tables)))
        async with engine.connect() as conn:
            await conn.run_sync(docker_schema._verify)

        monkeypatch.setattr(audit, "evidence_session", async_sessionmaker(engine))
        now = datetime.now(timezone.utc)
        record = dict(started_at=now, completed_at=now, duration_ms=1, direction="inbound",
                      integration="partner", operation="test", method="POST", endpoint="/test",
                      status=200, outcome="response", request=Capture().snapshot(), response=Capture().snapshot(),
                      request_body_raw=b'\xff\x00{malformed', response_body_raw=b'{"unknown":"secret"}',
                      trace_id="a" * 32, span_id="b" * 16, error_class="ok", order_id="o-1",
                      unknown_key="dropped")
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE partner_exchanges"))
        async with async_sessionmaker(engine)() as handler:
            await handler.execute(text("SELECT 1"))
            await audit.record_exchange(record)
            await handler.rollback()  # evidence survives the business rollback
        await asyncio.gather(*(audit.record_exchange(record) for _ in range(4)))
        await audit.record_span({"trace_id": "a" * 32, "span_id": "c" * 16, "parent_span_id": None,
                                 "name": "http.server POST /x", "kind": "server", "service": "bap",
                                 "start_time": now, "end_time": now, "status": "ok", "error": None,
                                 "attributes": {"when": now}, "order_id": "o-1", "store_id": "s-1"})
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM partner_exchanges WHERE trace_id=:t"),
                                     {"t": "a" * 32}) == 5
            raw = (await conn.execute(text("SELECT request_body_raw FROM partner_exchanges LIMIT 1"))).one()
            assert raw.request_body_raw == record["request_body_raw"]
            span = (await conn.execute(text("SELECT order_id, attributes FROM obs_spans"))).one()
            assert span.order_id == "o-1" and span.attributes["store_id"] == "s-1"

        # Retention: bodies stripped after 30 days, rows deleted after 180, spans after 30.
        async with engine.begin() as conn:
            ids = (await conn.execute(text("SELECT id FROM partner_exchanges ORDER BY id"))).scalars().all()
            await conn.execute(text("UPDATE partner_exchanges SET started_at=:t WHERE id=:id"),
                               {"t": now - timedelta(days=31), "id": ids[0]})
            await conn.execute(text("UPDATE partner_exchanges SET started_at=:t WHERE id=:id"),
                               {"t": now - timedelta(days=181), "id": ids[1]})
            await conn.execute(text("UPDATE obs_spans SET start_time=:t"), {"t": now - timedelta(days=31)})
        result = await pruning.prune(URL, batch=1, now=now)
        assert result == {"rows_deleted": 1, "bodies_stripped": 1, "spans_deleted": 1}
        async with engine.connect() as conn:
            kept = (await conn.execute(text(
                "SELECT request_body_raw, response_body_json, trace_id FROM partner_exchanges WHERE id=:id"),
                {"id": ids[0]})).one()
            assert kept.request_body_raw is None and kept.response_body_json is None
            assert kept.trace_id == "a" * 32  # metadata stays
            assert await conn.scalar(text("SELECT count(*) FROM partner_exchanges")) == 4
        assert await pruning.prune(URL, batch=1, now=now) == {
            "rows_deleted": 0, "bodies_stripped": 0, "spans_deleted": 0}

        # Projection edge cases keep exact raw bytes and JSON null semantics.
        samples = [b'null', b'true', b'{"unknown":"secret","x":1,"x":2}', b'NaN', b'\xff', b'{bad']
        for raw_bytes in samples:
            capture = Capture()
            capture.feed(raw_bytes)
            capture.complete = True
            projected, status = capture.parsed_json()
            await audit.record_exchange(dict(record, request=capture.snapshot(), request_body_raw=raw_bytes,
                                             request_body_json=projected))
            async with engine.connect() as conn:
                result = (await conn.execute(text(
                    "SELECT request_body_json IS NULL AS sql_null, jsonb_typeof(request_body_json) AS json_type "
                    "FROM partner_exchanges WHERE request_body_raw=:raw"), {"raw": raw_bytes})).one()
                if status == "parsed_null":
                    assert not result.sql_null and result.json_type == "null"
                elif status == "parsed":
                    assert not result.sql_null
                else:
                    assert result.sql_null
        await engine.dispose()
    asyncio.run(run())
