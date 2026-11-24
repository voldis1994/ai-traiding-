import { chromium } from "playwright";
import { mkdirSync } from "fs";

const outDir = "/opt/cursor/artifacts";
mkdirSync(outDir, { recursive: true });
const out = `${outDir}/perpsignal-desk-1290x515.png`;

const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 1290, height: 515 },
  deviceScaleFactor: 1,
});
await page.goto("http://127.0.0.1:5173/", { waitUntil: "networkidle" });
await page.waitForTimeout(800);
await page.screenshot({ path: out, fullPage: false });
await browser.close();
console.log("wrote", out);