# Dators kā serveris — adrese NEMANĀS

Mērķis: robots un dati **tavā PC**, klientiem viena un tā pati saite.

| Variants | Cena | Stabila adrese | Piezīmes |
|----------|------|----------------|----------|
| **A) DuckDNS + port forward** | Bezmaksas | Jā (`tavsvards.duckdns.org`) | Vajag routerī atvērt portu |
| **B) Cloudflare Tunnel** | Bezmaksas + lēts domēns | Jā | Bez port forward; drošāk |
| Ngrok free | Bezmaksas | **Nē** | Adrese mainās |
| Ngrok reserved | Maksas | Jā | Vienkārši, bet maksā |

---

## A) DuckDNS — **viens bat palaiz visu**

### Faili (jau repo)
| Fails | Kas |
|-------|-----|
| `start_all_windows.bat` | **Galvenais** — DuckDNS + robots + pārlūks |
| `duckdns_config.bat.example` | Šablons → nokopē kā `duckdns_config.bat` |
| `duckdns_config.bat` | Tavs token (lokāli, netiek commitots) |
| `update_duckdns_windows.bat` | IP atjaunošana |
| `setup_firewall_windows.bat` | Firewall 8765 (vienreiz kā Admin) |

### Soli
1. https://www.duckdns.org → domēns (tev: `neuraltraidingai`) + nokopē **token**
2. `git pull origin main`
3. Dubultklikšķis **`start_all_windows.bat`**  
   — pirmajā reizē atvērs Notepad: ieraksti token → saglabā → palaid vēlreiz
4. Vienreiz: labais klikšķis **`setup_firewall_windows.bat`** → Run as administrator
5. Routerī **vienreiz** Port Forward `8765` → PC IPv4 → `8765` TCP

### Saites (nemainās)
```
http://neuraltraidingai.duckdns.org:8765/app
http://neuraltraidingai.duckdns.org:8765/admin
```
Lokāli tev: `http://127.0.0.1:8765/admin`

**Svarīgi:** PC ieslēgts + `start_all_windows.bat` / robots logs atvērti.

---

## B) Cloudflare Tunnel (stabilāks, ar HTTPS)

1. Nopērc lētu domēnu (~5–12 €/gadā) VAI izmanto jau esošu  
2. Pievieno Cloudflare (free plan)  
3. Instalē `cloudflared` Windows  
4. `cloudflared tunnel login` → izveido tunnel uz `localhost:8765`  
5. DNS CNAME → tunnel  

Klients: `https://trading.tavsdomens.lv/app` — **nemainās**, bez port forward.

---

## Ko NEdarīt
- Neskrien paralēli Render + lokāli kā “viena sistēma” — dati šķiras  
- Ngrok free — adrese mainās  
- Render free bez Disk — mākoņa dati var pazust  

**Tev:** A vai B. Dati = PC. Adrese = stabila.
