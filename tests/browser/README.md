# `browser` tier

Long-running browser/UI-automation tests. Real Chromium via Playwright (JS)
or pytest-playwright (Python). Not run in CI by default — invoke manually.

```bash
# JS (from repo root)
pnpm exec playwright install            # one-time: download browser binaries
pnpm test:browser                       # runs playwright.config.ts

# Python
pytest -m browser
```

For onboarding-portal / registry / gateway UI flows, add `*.spec.ts` files
here once those apps stabilize.
