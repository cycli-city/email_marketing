import { describe, expect, it } from "vitest";

describe("test network guard", () => {
  it("rejects requests that have no mock handler (no accidental real network calls)", async () => {
    await expect(fetch("http://no-handler.invalid/anything")).rejects.toThrow();
  });
});
