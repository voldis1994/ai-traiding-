# Publisks hostings (klienti ārpus tava Wi‑Fi)

Mērķis: **viens stabils URL**, ceļi nekad nemainās:

| Ceļš | Kas |
|------|-----|
| `/` | Portāla sākums |
| `/app` | Klienta mobilā web |
| `/admin` | Tavs admin |
| `/console` | Operatora Capital.com dashboard |

## Varianti

### A) Render (ieteicams, vienkārši)

1. Push `main` uz GitHub
2. [Render](https://render.com) → **New → Blueprint** → izvēlies repo (`render.yaml`)
3. Iestatī env:
   - `NTA_ADMIN_PASSWORD` = tava stiprā parole
   - `NTA_PUBLIC_URL` = `https://<tavs-serviss>.onrender.com`
4. Pēc deploy klientiem iedod: `https://<tavs-serviss>.onrender.com/app`

**Savs domēns (URL nemainās):** Render → Custom Domain → `trading.tavsdomens.lv`  
Tad `NTA_PUBLIC_URL=https://trading.tavsdomens.lv`

### B) Docker (VPS / jebkur)

```bash
export NTA_ADMIN_PASSWORD='stipra-parole'
export NTA_PUBLIC_URL='https://trading.tavsdomens.lv'
docker compose up -d --build
```

Aiz reverse proxy (Caddy/Nginx) ar HTTPS. Ieslēdz `NTA_SECURE_COOKIES=1`.

### C) Railway

1. New project from repo  
2. Start command: no `Procfile`  
3. Env: `NTA_PRODUCTION=1`, `NTA_SECURE_COOKIES=1`, `NTA_ADMIN_PASSWORD=...`, `NTA_PUBLIC_URL=https://...`

## Obligātie env

| Env | Nozīme |
|-----|--------|
| `NTA_ADMIN_USER` | Admin login (noklusējums `admin`) |
| `NTA_ADMIN_PASSWORD` | Admin parole (**maini!**) |
| `NTA_SECRET_KEY` | Brokeru secret encryption (generē Render/Railway) |
| `NTA_PUBLIC_URL` | Publiskā bāzes adrese |
| `NTA_SECURE_COOKIES` | `1` uz HTTPS |
| `NEURAL_AI_DATA_DIR` | Persistents tilpums (`/data`) |

## Pēc deploy

1. Atver `/admin` → ielogojies  
2. Izveido klientu (login + parole)  
3. Pieslēdz Capital.com API  
4. Nosūti klientam tikai: **`NTA_PUBLIC_URL/app`** + login + parole  

Lokāli Windows joprojām: `run_windows.bat` → `http://127.0.0.1:8765`
