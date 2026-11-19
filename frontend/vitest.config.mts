import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    clearMocks: true,
    coverage: {
      provider: "v8",
      include: ["src/lib/**", "src/components/**", "src/hooks/**"],
      exclude: ["src/components/ui/**", "src/lib/api/schema.d.ts"],
    },
  },
});
