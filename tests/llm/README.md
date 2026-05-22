# `llm` tier

Tests that require a live LLM provider (DeepSeek/Anthropic/Qwen) or exercise
an LLM-backed surface end-to-end. They cost money and are not run in CI by
default.

```bash
# JS
pnpm test:llm

# Python
pytest -m llm
```

This repo's protocol/reference code does not currently call an LLM directly,
so this tier is empty. Add cases here when protocol-level tooling grows an
LLM-backed adapter or validator.
