import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    // XSS / code-injection sinks are banned outright. React escapes everything by default; the only ways to
    // bypass that (raw HTML, eval-like APIs, javascript: URLs) are errors, so they can't slip in unnoticed.
    // Untrusted HTML (e.g. an email body) must go through the backend sanitizer and a sandboxed iframe instead.
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "react/no-danger": "error",
      "react/no-danger-with-children": "error",
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-script-url": "error",
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
