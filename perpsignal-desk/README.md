# PerpSignal Desk

Pixel-faithful recreation of the PerpSignal Desk trading terminal (reference viewport **1290 × 515**).

## Demo data notice

All market data, LIVE badges, clocks, charts, and feeds are **deterministic mock/demo data** — not a live exchange stream. No real order execution is implemented.

## Develop

**Windows:** double-click `start.bat` (installs deps, opens the browser, runs Vite).

```bash
cd perpsignal-desk
npm install
npm run dev
```

## Build & preview

```bash
npm run build
npm run preview -- --host 127.0.0.1 --port 5173
```

## Screenshot validation

```bash
node scripts/screenshot.mjs
```

Captures `/opt/cursor/artifacts/perpsignal-desk-1290x515.png` at the reference viewport.
