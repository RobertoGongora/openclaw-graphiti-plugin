import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    testTimeout: 15_000,
    include: ["test/**/*.test.ts"],
    exclude: ["**/node_modules/**"],
  },
});
