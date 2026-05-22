# Tests

Tests are split into three tiers by **what they require to run**:

| Tier | Dir | Marker | Requires | When |
|---|---|---|---|---|
| `unit` | `tests/unit/` (or any other `tests/` w/o a marker) | _(none)_ | Nothing — pure logic + mocks | Every PR (default in CI) |
| `browser` | `tests/browser/` | `@pytest.mark.browser` / Playwright | A real browser binary (long-running) | Manual / on demand |
| `llm` | `tests/llm/` | `@pytest.mark.llm` | A live LLM provider | Manual / on demand (costs money) |

## Python

```bash
pytest                           # unit tier (default — excludes browser+llm)
pytest -m browser                # browser tier
pytest -m llm                    # llm tier
pytest -m ""                     # everything
```

Markers are registered in the root `pyproject.toml`. Most existing tests
live under `apps/beli-aman-bap/tests/` — those are unit-tier by default
(integration via FastAPI TestClient + in-memory SQLite, no real network).

## JavaScript / TypeScript

```bash
pnpm test                        # vitest — unit only
pnpm test:browser                # playwright (needs `pnpm exec playwright install` once)
pnpm test:llm                    # vitest — llm tier
```

CI runs `pnpm test` only. Browser + llm tiers are run manually until populated.
