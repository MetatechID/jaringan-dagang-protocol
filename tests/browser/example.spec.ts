import { test, expect } from "@playwright/test";

test("playwright is wired up", async ({ page }) => {
  await page.goto("data:text/html,<title>browser-tier</title><h1>ok</h1>");
  await expect(page).toHaveTitle("browser-tier");
});
