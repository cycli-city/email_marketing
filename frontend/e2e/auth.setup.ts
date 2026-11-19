import { expect, test as setup } from "@playwright/test";

import { OWNER, OWNER_STATE, PASSWORD } from "./support/constants";

// Signs in once through the real login form; the dashboard specs then reuse the resulting session cookies.
setup("sign in as the owner and keep the session", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill(OWNER.email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/system$/);
  await page.context().storageState({ path: OWNER_STATE });
});
