import { ESLint } from "eslint";
import { beforeAll, describe, expect, it } from "vitest";

/**
 * Proves the security lint rules are really enforced by the project config (not just written down):
 * each banned pattern must produce an ERROR, and safe code must be clean.
 */
let eslint: ESLint;

async function lint(code: string) {
  const [result] = await eslint.lintText(code, { filePath: "src/components/probe.tsx" });
  return result.messages.filter((m) => m.severity === 2).map((m) => m.ruleId);
}

// Loading the full Next + TypeScript ESLint config takes several seconds on a cold start.
describe("security lint rules", { timeout: 60_000 }, () => {
  beforeAll(async () => {
    eslint = new ESLint({ cwd: process.cwd() });
    await lint("export const warmup = 1;");
  }, 60_000);

  it("bans dangerouslySetInnerHTML (raw HTML injection)", async () => {
    const rules = await lint(
      `export const X = ({ html }: { html: string }) => <div dangerouslySetInnerHTML={{ __html: html }} />;`,
    );
    expect(rules).toContain("react/no-danger");
  });

  it("bans eval", async () => {
    expect(await lint(`export const run = (s: string) => eval(s);`)).toContain("no-eval");
  });

  it("bans new Function", async () => {
    expect(await lint(`export const f = (s: string) => new Function(s);`)).toContain("no-new-func");
  });

  it("bans string-argument setTimeout (implied eval)", async () => {
    expect(await lint(`export const t = () => setTimeout("alert(1)", 10);`)).toContain("no-implied-eval");
  });

  it("bans javascript: URLs", async () => {
    expect(await lint(`export const l = () => <a href="javascript:alert(1)">x</a>;`)).toContain(
      "no-script-url",
    );
  });

  it("accepts ordinary safe React", async () => {
    expect(await lint(`export const Safe = ({ name }: { name: string }) => <p>{name}</p>;`)).toEqual([]);
  });
});
