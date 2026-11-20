# PerpSignal Desk (React)

Pixel UI for the trading terminal. **Live data comes from the FastAPI Admin API**
(Capital.com + neural) when served at `http://127.0.0.1:8765/desk` after Admin login.

## Full system (recommended)

From the **repo root** (where `launcher.py` lives):

```bat
PALAIST_PILNO_SISTEMU.bat
```

Then open:

- `http://127.0.0.1:8765/admin` — full NEURAL Desk
- `http://127.0.0.1:8765/desk` — this React Desk (same cookies / live feeds)

`start.bat` in this folder now redirects to that full launcher (it does **not** start a mock-only Vite server).

## Dev-only (UI work)

Requires the Python backend already running on `8765`:

```bash
cd perpsignal-desk
npm install
npm run dev
```

Vite proxies `/api` → `127.0.0.1:8765`. Without Admin login you see the offline banner + demo fallback panels.

## Build into FastAPI

```bash
npm run build
```

Outputs to `app/web/desk/` (served at `/desk`).

## Demo notice

If Admin is not logged in or broker is offline, panels fall back to deterministic demo shapes. LIVE badges mean “wired to Admin poll”, not a separate exchange websocket.
