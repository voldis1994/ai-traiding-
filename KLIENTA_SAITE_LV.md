# Klienta web adrese (BEZ sava domēna)

`127.0.0.1` klientiem neder.

| Ceļš | Kas |
|------|-----|
| `/admin` | Reģistrācija (pareizi) |
| `/app` | Klienta web — **šo sūti** |
| `/console` | Novirza uz `/admin` |

---

## Nemainīga bezmaksas adrese (ieteicams)

**Render** pats iedod:
```
https://neural-trading-ai.onrender.com
```
- Domēnu **pirkt nevajag**
- Adrese **nemainās**, ja restartē sistēmu / redeploy
- Klientam vienmēr: `https://neural-trading-ai.onrender.com/app`

### Soli pa solim

1. Atver https://render.com → **Sign up with GitHub** (tas pats konts, kur `voldis1994/ai-traiding-`)
2. Dashboard → **New +** → **Blueprint**
3. Izvēlies repo **`voldis1994/ai-traiding-`**, branch **`main`**
4. Apstiprini servisu `neural-trading-ai` (free)
5. Env **`NTA_ADMIN_PASSWORD`** → ieraksti **savu** stipro admin paroli (neģenerē nejaušu, ja gribi zināt)
6. **Apply** / Create → gaidi ~5–10 min (Build)
7. Kad Status = **Live**, atver:
   - Admin: `https://neural-trading-ai.onrender.com/admin`
   - Klients: `https://neural-trading-ai.onrender.com/app`
8. `/admin` → izveido klientu + pieslēdz brokeru API  
9. Klientam sūti **tikai**:
   ```
   https://neural-trading-ai.onrender.com/app
   ```
   + login + parole

Ja vārds `neural-trading-ai` jau aizņemts — Render ļaus citu servisa nosaukumu; tad URL būs `https://<tavs-nosaukums>.onrender.com` un to ieliec arī env `NTA_PUBLIC_URL`.

### Brīdinājumi (dati!)

- **Klienti + history** dzīvo `/data` (`platform.sqlite3`, `accounts/…`).
- Bez **Render Disk** uz `/data` Manual Deploy / restart **var dzēst** visus klientus un treidu history.
- Dari: Render → serviss → **Disks** → Mount path `/data`, size 1 GB (parasti vajag **Starter**, ne free).
- `NTA_SECRET_KEY` **nemaini** pēc tam, kad klienti jau izveidoti (citādi brokeru API atšifrēšana lūzt).
- Free arī **aizmieg** ~15 min → pirmā ielāde 30–60 s.

---

## Ātri lokāli (adrese MAINĀS katru reizi)

Ngrok: `run_public_tunnel_windows.bat` — der testam, **nav** stabils domēns.

---

## Kas NAV “bezmaksas domēns, kas nekad nemainās”

| Variants | Stabils? |
|----------|----------|
| `127.0.0.1` | Nē — tikai tavs PC |
| Ngrok free | Nē — jauns URL pēc restart |
| **Render `*.onrender.com`** | **Jā** — kamēr serviss dzīvs |
| Savs domēns (pirkts) | Jā — nav obligāts |
