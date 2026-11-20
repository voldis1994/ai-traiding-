#!/usr/bin/env node
/**
 * Capture Admin Desk at 1290×515 and compare against PerpSignal reference.
 */
import { chromium } from '../perpsignal-desk/node_modules/playwright/index.mjs';
import fs from 'fs';
import path from 'path';
import { createRequire } from 'module';

const outDir = '/opt/cursor/artifacts';
const refPath = '/workspace/perpsignal-desk/docs/reference.jpg';
const shotPath = path.join(outDir, 'admin-desk-demo-1290x515.png');
const overlayPath = path.join(outDir, 'admin-desk-overlay.png');
const diffPath = path.join(outDir, 'admin-desk-diff.png');

fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({
  viewport: { width: 1290, height: 515 },
  deviceScaleFactor: 1,
});

await page.goto('http://127.0.0.1:8765/admin?deskDemo=1', { waitUntil: 'networkidle' });
const loginVisible = await page.locator('#loginForm').isVisible().catch(() => false);
if (loginVisible) {
  await page.fill('input[name="username"]', 'admin');
  await page.fill('input[name="password"]', 'admin123');
  await page.click('#loginForm button[type="submit"]');
  await page.waitForSelector('#dash:not(.hidden)', { timeout: 15000 });
  // Preserve deskDemo query after login may rewrite URL
  if (!page.url().includes('deskDemo=1')) {
    await page.goto('http://127.0.0.1:8765/admin?deskDemo=1', { waitUntil: 'networkidle' });
    // If redirected to login again, re-auth
    if (await page.locator('#loginForm').isVisible().catch(() => false)) {
      await page.fill('input[name="username"]', 'admin');
      await page.fill('input[name="password"]', 'admin123');
      await page.click('#loginForm button[type="submit"]');
      await page.waitForSelector('#dash:not(.hidden)', { timeout: 15000 });
    }
  }
}

await page.waitForSelector('#psDesk', { timeout: 10000 });
await page.waitForTimeout(2500);
await page.screenshot({ path: shotPath, fullPage: false });
console.log('shot', shotPath);

// Pixel compare via sharp if available, else canvas-free PNG parse with pngjs/jimp
let compared = false;
try {
  const { PNG } = await import('../perpsignal-desk/node_modules/pngjs/lib/png.js').catch(() => ({ PNG: null }));
  // Prefer pure buffer compare with playwright's PNG and sharp-less approach
} catch (_) {}

// Overlay: load both with Playwright canvas in-page
const refBuf = fs.readFileSync(refPath);
const shotBuf = fs.readFileSync(shotPath);
const refB64 = refBuf.toString('base64');
const shotB64 = shotBuf.toString('base64');

const metrics = await page.evaluate(async ({ refB64, shotB64 }) => {
  const load = (b64, mime) => new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = rej;
    img.src = `data:${mime};base64,${b64}`;
  });
  const ref = await load(refB64, 'image/jpeg');
  const shot = await load(shotB64, 'image/png');
  const w = 1290, h = 515;
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d');
  // Draw reference scaled to viewport
  ctx.drawImage(ref, 0, 0, w, h);
  const refData = ctx.getImageData(0, 0, w, h);
  ctx.clearRect(0, 0, w, h);
  ctx.drawImage(shot, 0, 0, w, h);
  const shotData = ctx.getImageData(0, 0, w, h);
  // Overlay 50%
  ctx.globalAlpha = 0.5;
  ctx.drawImage(ref, 0, 0, w, h);
  const overlayUrl = c.toDataURL('image/png');
  // Diff heatmap
  const diff = ctx.createImageData(w, h);
  let sum = 0, n = w * h;
  for (let i = 0; i < refData.data.length; i += 4) {
    const dr = Math.abs(refData.data[i] - shotData.data[i]);
    const dg = Math.abs(refData.data[i + 1] - shotData.data[i + 1]);
    const db = Math.abs(refData.data[i + 2] - shotData.data[i + 2]);
    const d = (dr + dg + db) / 3;
    sum += d;
    diff.data[i] = d > 24 ? 255 : 0;
    diff.data[i + 1] = d > 24 ? 40 : 0;
    diff.data[i + 2] = d > 24 ? 40 : 0;
    diff.data[i + 3] = 255;
  }
  ctx.putImageData(diff, 0, 0);
  const diffUrl = c.toDataURL('image/png');
  // Measure key DOM boxes
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.x), y: Math.round(r.y) };
  };
  return {
    meanAbsDiff: sum / n,
    boxes: {
      header: box('.term-top'),
      tape: box('#deskTape'),
      rail: box('.psx-rail'),
      intel: box('.psx-intel'),
      grid: box('.psx-grid'),
      right: box('.psx-right'),
      chart: box('.psx-chart-col'),
      book: box('.psx-book-col'),
      engine: box('.psx-engine-col'),
    },
    overlayUrl,
    diffUrl,
  };
}, { refB64, shotB64 });

const decode = (dataUrl) => Buffer.from(dataUrl.split(',')[1], 'base64');
fs.writeFileSync(overlayPath, decode(metrics.overlayUrl));
fs.writeFileSync(diffPath, decode(metrics.diffUrl));
console.log(JSON.stringify({ meanAbsDiff: metrics.meanAbsDiff, boxes: metrics.boxes }, null, 2));

await browser.close();
