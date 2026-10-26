# Mans pašmācīšanās treidinga robots

**Šis repo ir reāla pašmācīšanās mašīna** — neironu kodols + Capital.com LIVE +
automātiska mācīšanās un (shadow) stratēģijas evolūcija.

## Algoritmi un vienādojumi

Sāc šeit (latviski, viena patiesība par sistēmu):

→ **[`PASMACISANAS_MASINA_LV.md`](PASMACISANAS_MASINA_LV.md)**

Tur ir:
- cauruļvads \(F_{\text{learn}}\circ\ldots\circ F_{\text{neuron}}\)
- \(h/c\) dinamika, Softmax, birth/prune
- mācīšanās ar \(\eta=0.1\)
- SL/TP formulas
- kas ir LIVE lēmums vs kas ir tikai shadow

Matemātiskais PDF: `reference/Parbauditas_Sakartotas_Formulas.pdf`  
Tehniskā vēsture: `README.PROJECT.md`, `V6_AUTONOMA_AI_LV.txt`

## Portāli

| URL | Kas |
|-----|-----|
| `/` | Operatora konsoles (Capital.com LIVE, pilnais dashboard) |
| `/admin` | **Admin** — izveido klienta login/paroli, pieslēdz brokeru API |
| `/app` | **Klients** — LV/EN/RU, vadīti soļi, tad robots (BUY/HOLD/SELL + evolūcija, Pause/Resume) |

Noklusējuma admin (mainiet ar env): `NTA_ADMIN_USER=admin` · `NTA_ADMIN_PASSWORD=admin123`  
Brokeru atslēgas šifrētas `data/secret.key` (vai `NTA_SECRET_KEY`).

## Kā palaist

1. Windows: `run_windows.bat` → `http://127.0.0.1:8765`
2. Admin: `/admin` → izveido klientu + brokeri **vai** operator: Capital.com API uz `/`
3. Klients: `/app` ar admin iedoto login/paroli → 1. manuālais treids broker app → robots pārņem
4. Pēc aizvēršanas AI lemj uz **katru jaunu pabeigtu M1** sveci

Publiskam hostam (klienti ārpus tava Wi‑Fi) deployē šo pašu app ar stabilu URL; ceļi `/admin` un `/app` nemainās.

Linux:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu
python launcher.py
```

## Testi

```bash
python -m pytest -q
```

147 passed.

## Svarīgi

- LIVE lēmums = **neironu argmax** (BUY/SELL/HOLD), ne vecie 60%/MIN_EDGE vārti
- Background labels = **nākamā M1** svece; pēc aizvērta treida — **P&L pašnovērtējums** (mīnuss → regret)
- Evolūcija pēc treida (`force`), lai nesekotu novecojušam režīmam
- **Pēc restarta / login:** vecais adoptive size **netiek** atjaunots no SQLite — jāatver **jauna manuāla** pozīcija
- Datubāze: source run → `data/`; Windows EXE → `%LOCALAPPDATA%\NeuralTradingAI` (login event rāda `data_dir`)
- Riskē tikai naudu, ko vari zaudēt
