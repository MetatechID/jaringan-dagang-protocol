# Contributing

Thanks for your interest in `jaringan-dagang-protocol`. This file
covers the basics; for deeper docs see [`docs/`](docs/).

## Before you start

- This repo lives under the Apache-2.0 license (see [LICENSE](LICENSE)).
  By contributing, you agree your contribution is licensed under
  those same terms — no separate CLA required.
- Read the [Code of Conduct](CODE_OF_CONDUCT.md). It's short.
- Check [open issues](https://github.com/MetatechID/jaringan-dagang-protocol/issues)
  before starting non-trivial work. We're happy to discuss before you write code.

## Dev setup

```bash
# 1. Install pnpm (Node 24+) and uv (Python 3.12+)
corepack enable
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone + install all workspace packages
git clone https://github.com/MetatechID/jaringan-dagang-protocol.git
cd jaringan-dagang-protocol
pnpm install        # JS workspaces
uv sync             # Python workspace

# 3. Run tests
pnpm test           # JS
uv run pytest       # Python
```

## Making a change

1. Branch from `main`.
2. Make your change in the smallest possible diff.
3. **Add a changeset** describing what changed and why:
   ```bash
   pnpm changeset
   ```
   This writes a `.changeset/<random>.md` file. Pick the smallest bump
   that fits (patch / minor / major).
4. Open a PR. CI runs lint + tests + contract tests.
5. On merge, the changesets GitHub Action proposes a "Release" PR
   that bumps versions + updates CHANGELOG. Merging that PR publishes
   to npm + PyPI.

## What we look for in PRs

- **Small surface**. One concern per PR.
- **Tests** for the new behavior. Contract tests live in
  `tests/contract/` and must keep passing.
- **No `console.log` / `print()`** in shipping code. Use a logger.
- **No secret values committed**. Even in examples — use placeholders
  like `<YOUR_API_KEY>`.
- **No tenant-specific code**. Reference implementations should run
  against any subscriber, not just our `safiyafood.jaringan-dagang.id`.

## Reporting bugs

Open an issue using the bug template. Include:
- Which package + version
- Minimal repro
- What you expected vs what happened

## Reporting security issues

**Do not open a public issue for security bugs.** Email
security@metatech.id with details. We respond within 72 h.

## Questions

Open a Discussion, or reach the maintainers on Telegram
@hallucinogen.
