# Klienta web adrese (BEZ sava domēna)

`127.0.0.1` / `localhost` **nedrīkst** sūtīt klientiem — tas strādā tikai tavā datorā.

Tevis jau ir pareizie ceļi:
- `/admin` — reģistrācija (pareizi)
- `/app` — klienta web
- `/console` — **Operators** (Capital.com login) — tas NAV “vecais admin”, tā ir tava operatora konsoles

---

## A) ĀTRI tagad (tunelis, bez domēna)

Kamēr tavs `run_windows.bat` logs ir atvērts:

1. Otrajā CMD:
```bat
winget install ngrok.ngrok
```
2. Izveido bezmaksas kontu: https://dashboard.ngrok.com/signup  
3. Nokopē Authtoken un CMD:
```bat
ngrok config add-authtoken TAVA_TOKENA_VERTIBA
cd /d C:\NeuralTradingAI_NEW
run_public_tunnel_windows.bat
```
4. Ngrok logā būs rinda tipa:
`Forwarding  https://xxxx.ngrok-free.app -> http://127.0.0.1:8765`
5. **Klientam sūti:**
`https://xxxx.ngrok-free.app/app`  
+ login/paroli no `/admin`

**Svarīgi:** klients redz lapu tikai kamēr tavs dators un robots strādā.

---

## B) PASTĀVĪGI (Render, arī BEZ sava domēna)

Render **pats iedod** adresi `https://….onrender.com` — domēnu pirkt nevajag.

1. https://render.com → Sign up with GitHub  
2. **New → Blueprint** → repo `voldis1994/ai-traiding-`  
3. Env:
   - `NTA_ADMIN_PASSWORD` = tava stiprā parole  
   - `NTA_PUBLIC_URL` = (pēc deploy ieliec to `https://….onrender.com`)  
4. Deploy → kopē servisa URL  
5. Klientam: `https://….onrender.com/app`

Detalizētāk: `DEPLOY_LV.md`
