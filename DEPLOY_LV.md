# Publisks hostings (klienti ārpus tava Wi‑Fi)

**Savs domēns NAV obligāts.** Render/ngrok paši iedod `https://….` adresi.

Īsā klientu saite: skati **`KLIENTA_SAITE_LV.md`**.

Mērķis: **viens stabils URL**, ceļi nekad nemainās:

| Ceļš | Kas |
|------|-----|
| `/` | Portāla sākums |
| `/app` | Klienta mobilā web |
| `/admin` | Tavs admin |
| `/console` | Novirza uz `/admin` |

## Varianti

### A) Render (ieteicams — bezmaksas stabils URL)

Render iedod **nemainīgu** adresi bez pirkta domēna, piem.:
`https://neural-trading-ai.onrender.com`

1. Push `main` uz GitHub
2. [Render](https://render.com) → **New → Blueprint** → repo + `render.yaml`
3. Env: `NTA_ADMIN_PASSWORD` = tava stiprā parole  
   (`NTA_PUBLIC_URL` jau ir blueprintā)
4. Klientiem: `https://neural-trading-ai.onrender.com/app`

Īsie soļi: **`KLIENTA_SAITE_LV.md`**.

**Savs domēns (nav obligāts):** Render → Custom Domain → `trading.tavsdomens.lv`  
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
