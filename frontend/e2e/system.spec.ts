import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test.describe("System status page (real backend)", () => {
  test("root redirects to /system and shows live backend health", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/system$/);
    await expect(page.getByRole("heading", { name: "System status" })).toBeVisible();
    await expect(page.getByText("All systems operational")).toBeVisible();
    await expect(page.getByText("Healthy")).toHaveCount(1); // database
    await expect(page.getByText("Up", { exact: true })).toBeVisible(); // API
  });

  test("navigation highlights the active page", async ({ page }) => {
    await page.goto("/system");
    // Base UI marks the active item with the bare `data-active` attribute (present, empty value).
    await expect(page.getByRole("link", { name: "System" })).toHaveAttribute("data-active", "");
  });

  test("theme toggle switches between light and dark", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/system");
    const html = page.locator("html");
    await expect(html).not.toHaveClass(/\bdark\b/);
    await page.getByRole("button", { name: /toggle light\/dark theme/i }).click();
    await expect(html).toHaveClass(/\bdark\b/);
    await page.getByRole("button", { name: /toggle light\/dark theme/i }).click();
    await expect(html).not.toHaveClass(/\bdark\b/);
  });

  test("shows an actionable message when the backend is unreachable", async ({ page }) => {
    await page.route("**/healthz", (route) => route.abort("connectionrefused"));
    await page.goto("/system");
    await expect(page.getByText("Backend unreachable")).toBeVisible();
    await expect(page.getByText("BACKEND_URL")).toBeVisible();
  });

  test("shows which dependency is failing when the backend is degraded", async ({ page }) => {
    await page.route("**/readyz", (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ status: "degraded", checks: { database: "error" } }),
      }),
    );
    await page.goto("/system");
    await expect(page.getByText(/degraded — a dependency is failing/i)).toBeVisible();
    // exact: the banner text "…a dependency is failing" would otherwise also match.
    await expect(page.getByText("Failing", { exact: true })).toHaveCount(1);
  });

  test("refresh button re-checks the backend", async ({ page }) => {
    await page.goto("/system");
    await expect(page.getByText("All systems operational")).toBeVisible();
    const refetch = page.waitForRequest("**/healthz");
    await page.getByRole("button", { name: "Refresh" }).click();
    await refetch;
  });
});

test.describe("Responsive layout", () => {
  test.use({ viewport: { width: 390, height: 780 } });

  test("on mobile the navigation is behind a toggle that works", async ({ page }) => {
    await page.goto("/system");
    await expect(page.getByRole("link", { name: "System" })).toBeHidden();
    await page.getByRole("button", { name: "Toggle navigation" }).first().click();
    await expect(page.getByRole("link", { name: "System" })).toBeVisible();
  });

  test("no horizontal scrolling on mobile", async ({ page }) => {
    await page.goto("/system");
    await expect(page.getByText("All systems operational")).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
});

test.describe("Accessibility (WCAG 2.1 A/AA)", () => {
  for (const scheme of ["light", "dark"] as const) {
    test(`/system has no axe violations in ${scheme} mode`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.goto("/system");
      await expect(page.getByText("All systems operational")).toBeVisible();
      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
      expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
    });
  }
});
