"""Task A5 — ``scripts/add-dispute-issue-columns.py`` adds the IGM
tracking columns to the ``disputes`` table (idempotently, via Postgres
``ADD COLUMN IF NOT EXISTS``).

Default mode is dry-run: prints the SQL and exits. ``--apply`` requires
``DATABASE_URL``.

This mirrors the pattern of the seller's ``add-image-base-url-column``
migration: the no-Alembic BAP schema creation is via
``Base.metadata.create_all`` (only handles tables), so this one-shot
ALTER is needed when extending an existing live ``disputes`` table.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "add-dispute-issue-columns.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "add_dispute_issue_columns", _SCRIPT_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["add_dispute_issue_columns"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_script_exists():
    assert _SCRIPT_PATH.exists()


def test_script_importable_without_side_effects():
    mod = _load_module()
    assert hasattr(mod, "print_dry_run_sql")
    assert hasattr(mod, "apply_migration")


def test_dry_run_adds_all_three_columns_idempotently():
    mod = _load_module()
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.print_dry_run_sql()
    out = buf.getvalue()
    # All three IGM-tracking columns must be added.
    assert "bpp_issue_id" in out
    assert "bpp_resolution_note" in out
    assert "resolved_at" in out
    # Must use Postgres idempotency clause.
    assert "ADD COLUMN IF NOT EXISTS" in out, (
        "ALTER TABLE must use 'ADD COLUMN IF NOT EXISTS'"
    )


def test_dry_run_creates_index_on_bpp_issue_id():
    """Reconciling /on_issue -> Dispute lookup is by bpp_issue_id so an
    index is required for non-trivial dispute volumes."""
    mod = _load_module()
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.print_dry_run_sql()
    out = buf.getvalue()
    assert "CREATE INDEX IF NOT EXISTS" in out
    assert "ix_disputes_bpp_issue_id" in out


def test_apply_requires_database_url(monkeypatch):
    mod = _load_module()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["add-dispute-issue-columns.py", "--apply"]
    )
    rc = mod.main()
    assert rc != 0
