import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

// Runs as the signed-in owner against the real backend and a real database: the settings are really saved and read back.

test.describe("Sending settings", () => {
  test("is reachable from the navigation and shows both identities and the country rules", async ({
    page,
  }) => {
    await page.goto("/system");
    await page.getByRole("link", { name: "Sending" }).click();
    await expect(page).toHaveURL(/\/settings\/sending$/);
    await expect(page.getByRole("heading", { name: "Sending", level: 1 })).toBeVisible();
    await expect(page.getByLabel("NEXORA Sales", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Job Hunter", { exact: true })).toBeVisible();
    await expect(page.getByRole("rowheader", { name: "DE", exact: true })).toBeVisible();
    await expect(page.getByRole("rowheader", { name: "Everywhere else" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Sending" })).toHaveAttribute("data-active", "");
  });

  test("has no accessibility violations (WCAG A/AA)", async ({ page }) => {
    await page.goto("/settings/sending");
    await expect(page.getByRole("rowheader", { name: "DE", exact: true })).toBeVisible();
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(results.violations).toEqual([]);
  });

  test("saving the sender identity makes the mode ready, and it is still there after a reload", async ({
    page,
  }) => {
    await page.goto("/settings/sending");
    const nexora = page.getByLabel("NEXORA Sales", { exact: true });
    await nexora.getByLabel("Your name as it appears on the e-mail").fill("Alex Founder");
    await nexora.getByLabel("Postal address").fill("1 Example Street\nExample City");
    await nexora.getByRole("button", { name: /save the nexora sales sender identity/i }).click();
    await expect(nexora.getByText("Saved")).toBeVisible();

    await page.reload();
    const again = page.getByLabel("NEXORA Sales", { exact: true });
    await expect(again.getByText("Ready to send")).toBeVisible();
    await expect(again.getByLabel("Postal address")).toHaveValue("1 Example Street\nExample City");
  });

  test("an invalid reply-to is refused by the server and shown against its field", async ({ page }) => {
    await page.goto("/settings/sending");
    const nexora = page.getByLabel("NEXORA Sales", { exact: true });
    await nexora.getByLabel("Your name as it appears on the e-mail").fill("Alex Founder");
    await nexora.getByLabel("Reply-to address (optional)").fill("not-an-address");
    await nexora.getByRole("button", { name: /save the nexora sales sender identity/i }).click();
    await expect(nexora.getByRole("alert")).toContainText("not a valid e-mail address");
    await expect(nexora.getByLabel("Reply-to address (optional)")).toHaveAttribute("aria-invalid", "true");
  });

  test("relaxing a country's rule asks first, and declining leaves it exactly as it was", async ({
    page,
  }) => {
    await page.goto("/settings/sending");
    await page.getByLabel("Companies for FR").selectOption("allowed");
    await page.getByRole("button", { name: "Save and mark FR reviewed" }).click();

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("This makes sending easier");
    await dialog.getByRole("button", { name: /keep it as it was/i }).click();
    await expect(dialog).toBeHidden();

    await page.reload();
    await expect(page.getByLabel("Companies for FR")).toHaveValue("requires_consent");
  });
});
