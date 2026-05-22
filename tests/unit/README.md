# `unit` tier

Short, isolated tests. No real network, browser, DB, or LLM provider.
Mocks are fine. Anything in this tier should finish in milliseconds.

This is the **default tier**: `pytest` and `pnpm test` run it.

Most existing pytest tests live in `apps/beli-aman-bap/tests/` and
`packages/beckn-protocol-py/tests/`. Those are unit-tier by default —
no marker required.
