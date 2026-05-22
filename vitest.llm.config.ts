import { defineConfig } from "vitest/config";

// LLM tier — tests that call a real LLM provider. Not run in CI.
export default defineConfig({
  test: {
    include: ["tests/llm/**/*.test.ts", "tests/llm/**/*.test.tsx"],
    exclude: ["node_modules/**", "**/dist/**"],
    environment: "node",
    testTimeout: 60_000,
  },
});
