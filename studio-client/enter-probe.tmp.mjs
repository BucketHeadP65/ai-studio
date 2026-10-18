import { chromium } from "@playwright/test";

const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
const posts = [];
page.on("request", (r) => {
  if (r.method() === "POST") posts.push(r.url());
});
page.on("console", (m) => {
  if (m.type() === "error" || m.type() === "warning") errors.push(`${m.type()}: ${m.text().slice(0, 300)}`);
});
page.on("pageerror", (e) => errors.push(`PAGEERROR: ${String(e).slice(0, 300)}`));

try {
  await page.goto("http://127.0.0.1:3031/studio", { waitUntil: "networkidle" });
  const box = page.getByLabel("Message");

  // Poll up to 30s for React to attach.
  let attached = false;
  for (let i = 0; i < 60; i++) {
    attached = await box.evaluate((el) => Object.keys(el).some((k) => k.startsWith("__reactProps$")));
    if (attached) break;
    await page.waitForTimeout(500);
  }
  console.log("react attached to textarea:", attached);

  const navAttached = await page
    .locator("nav a")
    .first()
    .evaluate((el) => Object.keys(el).some((k) => k.startsWith("__reactProps$")))
    .catch(() => "no nav");
  console.log("react attached to nav:", navAttached);

  if (attached) {
    await box.click();
    await box.pressSequentially("probe after hydration");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(2000);
    console.log("after Enter — textarea:", JSON.stringify(await box.inputValue()), "posts:", posts);
  }
} finally {
  console.log("console errors/warnings:", errors.slice(0, 8));
  await browser.close();
}
