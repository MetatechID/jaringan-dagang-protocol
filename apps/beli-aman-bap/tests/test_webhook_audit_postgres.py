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
pytestmark = pytest.mark.skipif(not URL, reason="requires disposable AUDIT_TEST_DATABASE_URL")


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, APP / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_independent_commit_concurrency_and_cutoff(monkeypatch):
    import database
    from services.webhook_audit import Capture, write_audit
    migration = load_script("add-webhook-audit")
    pruning = load_script("prune-webhook-audit")

    async def run():
        engine = create_async_engine(URL, pool_size=1, max_overflow=2)
        # Simulate the previously installed metadata-only schema, then upgrade twice.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS webhook_audits"))
            await conn.execute(text(migration.DDL))
        await migration.apply_migration(URL)
        await migration.apply_migration(URL)
        monkeypatch.setattr(database, "async_session", async_sessionmaker(engine))
        now = datetime.now(timezone.utc)
        record = dict(started_at=now, completed_at=now, duration_ms=1, direction="inbound",
                      integration="partner", operation="test", method="POST", endpoint="/test",
                      status=200, outcome="response", request=Capture().snapshot(), response=Capture().snapshot(),
                      request_body_raw=b'\xff\x00{malformed', response_body_raw=b'{"unknown":"secret"}')
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE webhook_audits"))
        async with database.async_session() as handler:
            await handler.execute(text("SELECT 1"))
            await write_audit(record)
            await handler.rollback()
        await asyncio.gather(*(write_audit(record) for _ in range(4)))
        cutoff = now - timedelta(days=30)
        async with engine.begin() as conn:
            count = await conn.scalar(text("SELECT count(*) FROM webhook_audits"))
            assert count == 5
            raw = (await conn.execute(text("SELECT request_body_raw, response_body_raw FROM webhook_audits LIMIT 1"))).one()
            assert raw.request_body_raw == record["request_body_raw"]
            assert raw.response_body_raw == record["response_body_raw"]
            indexes = (await conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='webhook_audits'"))).scalars().all()
            assert len(indexes) == 7
            # Exact boundary retained; strictly older deleted in batches of one.
            await conn.execute(text("UPDATE webhook_audits SET started_at=:cutoff"), {"cutoff": cutoff})
            ids = (await conn.execute(text("SELECT id FROM webhook_audits ORDER BY id"))).scalars().all()
            await conn.execute(text("UPDATE webhook_audits SET started_at=:older WHERE id=:id"),
                               {"older": cutoff - timedelta(microseconds=1), "id": ids[0]})
            deleted = (await conn.execute(pruning.DELETE_BATCH, {"cutoff": cutoff, "batch": 1})).all()
            assert len(deleted) == 1
            assert not (await conn.execute(pruning.DELETE_BATCH, {"cutoff": cutoff, "batch": 1})).all()
        assert await pruning.prune(URL, batch=1) == 4
        # Projection failures must not prevent exact raw evidence persistence.
        samples = [b'null', b'true', b'42', b'1.25', b'"text"',
                   b'{"unknown":"secret","x":1,"x":2}', b'[1,false,null]',
                   b'NaN', b'Infinity', b'1e9999', b'"\\u0000"', b'"\\ud800"',
                   b'{"x":"\\u0000","x":1}', b'\xff', b'{bad',
                   b'[' * 70 + b'0' + b']' * 70]
        for raw in samples:
            capture = Capture()
            capture.feed(raw)
            capture.complete = True
            projected, status = capture.parsed_json()
            item = dict(record, request=capture.snapshot(), request_body_raw=raw,
                        request_body_json=projected, response_body_json={"unknown": "secret"})
            await write_audit(item)
            async with engine.connect() as conn:
                result = (await conn.execute(text(
                    "SELECT request_body_raw, request_body_json, request_body_json IS NULL AS sql_null, "
                    "jsonb_typeof(request_body_json) AS json_type, response_body_json "
                    "FROM webhook_audits WHERE request_body_raw=:raw"
                ), {"raw": raw})).one()
                assert result.request_body_raw == raw
                assert result.response_body_json == {"unknown": "secret"}
                if status == "parsed_null":
                    assert not result.sql_null and result.json_type == "null"
                elif status == "parsed":
                    assert not result.sql_null and result.request_body_json == projected
                else:
                    assert result.sql_null
        for reason in ("truncated", "incomplete", "not_consumed", "empty"):
            capture = Capture(limit=1 if reason == "truncated" else 65536)
            if reason != "not_consumed":
                capture.feed(b'' if reason == "empty" else b'{}')
            capture.complete = reason != "incomplete"
            assert capture.parsed_json() == (None, reason)
            await write_audit(dict(record, operation=reason, request=capture.snapshot(),
                                   request_body_raw=capture.raw_body(), request_body_json=None))
            async with engine.connect() as conn:
                assert await conn.scalar(text(
                    "SELECT request_body_json IS NULL FROM webhook_audits WHERE operation=:reason"
                ), {"reason": reason})
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM webhook_audits")) == len(samples) + 4
        await engine.dispose()
    asyncio.run(run())
