import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { server } from "./server";

// Any request without an explicit handler fails the test: no accidental real network calls.
// (MSW v3 renamed `onUnhandledRequest` → `onUnhandledFrame`; see msw-guard.test.ts, which proves this works.)
beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
afterEach(() => {
  server.resetHandlers();
  cleanup();
});
afterAll(() => server.close());
