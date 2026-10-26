# Dators kā serveris — adrese NEMANĀS

Mērķis: robots un dati **tavā PC**, klientiem viena un tā pati saite.

| Variants | Cena | Stabila adrese | Piezīmes |
|----------|------|----------------|----------|
| **A) DuckDNS + port forward** | Bezmaksas | Jā (`tavsvards.duckdns.org`) | Vajag routerī atvērt portu |
| **B) Cloudflare Tunnel** | Bezmaksas + lēts domēns | Jā | Bez port forward; drošāk |
| Ngrok free | Bezmaksas | **Nē** | Adrese mainās |
| Ngrok reserved | Maksas | Jā | Vienkārši, bet maksā |

---

## A) DuckDNS (ieteicams, ja gribi 0 €)

### 1. DuckDNS
1. https://www.duckdns.org → Sign in (Google/GitHub)
2. Izveido subdomēnu, piem. `neuraltradingai`  
   → adrese: `neuraltradingai.duckdns.org`
3. Nokopē **token**

### 2. Windows — atjauno IP (CMD kā Administrators nav obligāti)
Izveido failu `C:\NeuralTradingAI_NEW\update_duckdns.bat`:

```bat
@echo off
set DOMAIN=neuraltradingai
set TOKEN=TAVA_DUCKDNS_TOKENA
curl "https://www.duckdns.org/update?domains=%DOMAIN%&token=%TOKEN%&ip="
echo.
pause
```

Palaid **reizi** un pēc tam ieliec Task Scheduler ik pēc 5–15 min (vai katru reizi ieslēdzot PC).

### 3. Router — port forward
Router admin (bieži `192.168.1.1` / `192.168.0.1`):
- **External port:** `8765` (vai `80` → iekšēji 8765)
- **Internal IP:** tava PC LAN IP (piem. `192.168.1.50`) — labāk **DHCP reservation**, lai IP nemainās
- **Internal port:** `8765`
- Protocol: TCP

### 4. Windows Firewall
CMD **kā Administrator**:
```bat
netsh advfirewall firewall add rule name="Neural Trading AI" dir=in action=allow protocol=TCP localport=8765
```

### 5. Palaid robotu
```bat
cd /d C:\NeuralTradingAI_NEW
run_windows.bat
```

### 6. Klientam (nemainās)
```
http://neuraltradingai.duckdns.org:8765/app
```
Admin:
```
http://neuraltradingai.duckdns.org:8765/admin
```

**Svarīgi:** PC jāpaliek ieslēgtam + `run_windows.bat` jāstrādā.  
HTTP (ne HTTPS) — pietiek sākumam; vēlāk var Caddy/HTTPS.

Env (pēc izvēles) pirms starta:
```bat
set NTA_PUBLIC_URL=http://neuraltradingai.duckdns.org:8765
set NTA_PRODUCTION=1
```

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
