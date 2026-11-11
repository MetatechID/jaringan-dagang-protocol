"""Shared test setup for the BAP suite."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _evidence_stays_in_process(monkeypatch):
    """Spans and partner-exchange rows go to a separate pool in production.
    In tests, never open a database connection for them: whatever Postgres
    happens to listen on localhost is not ours. Tests that assert on evidence
    monkeypatch ``record_exchange`` / ``record_span`` / ``evidence_session``."""
    import services.partner_exchange as evidence

    def no_database():
        raise RuntimeError("evidence database disabled in tests")

    monkeypatch.setattr(evidence, "evidence_session", no_database)
