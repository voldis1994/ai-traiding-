# Mans pašmācīšanās treidinga robots

Personīgā **Neural Trading AI v6** sistēma ar rekurentu neironu tīklu, Capital.com LIVE savienojumu un automātisku stratēģijas evolūciju.

## Kas tas ir

- Pašmācās no pabeigtām M1 svecēm (+ M5/M15/H1, intermarket, ziņu kalendārs)
- Lemj BUY / SELL / HOLD
- Neironi var augt un tikt izgriezti (birth / prune)
- Pirmais darījums: **manuāli** Capital.com → robots pārņem EPIC un lotu
- Pēc manuālās pozīcijas aizvēršanas AI var atvērt nākamos orderus pats

## Palaišana (Windows)

1. Python **3.11–3.13** (64-bit)
2. Izpako / klonē šo repo
3. Palaid `run_windows.bat`
4. Atver `http://127.0.0.1:8765`
5. Ievadi savus Capital.com API datus (paliek tikai RAM šajā sesijā)
6. Telefonā atver vienu pozīciju → robots to adoptē un sāk mācīties / aizsargāt

Linux: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && pip install torch --index-url https://download.pytorch.org/whl/cpu && python launcher.py`

## Testi

```bash
python -m pytest -q
```

Pašlaik: **137 passed**.

## Svarīgi

- Šis ir **tavs** lokālais robots — kredenciāli netiek commitoti.
- Neironu svari un SQLite saglabājas `data/` (vai Windows: `%LOCALAPPDATA%\NeuralTradingAI`).
- Signāli ir modeļa score, nevis peļņas garantija. Riskē tikai ar naudu, ko vari zaudēt.

Detalizētā tehniskā dokumentācija: `README.PROJECT.md`, `V6_AUTONOMA_AI_LV.txt`, `UZSTADISANA_LV.txt`.
