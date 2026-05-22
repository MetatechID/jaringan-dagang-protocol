import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/unit/**/*.test.ts", "tests/unit/**/*.test.tsx"],
    exclude: ["tests/browser/**", "tests/llm/**", "node_modules/**", "**/dist/**"],
    environment: "node",
  },
});
