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

## Kā palaist

1. Windows: `run_windows.bat` → `http://127.0.0.1:8765`
2. Capital.com API (sesijā RAM)
3. Telefonā **viena** manuāla pozīcija → robots adoptē EPIC/lotu
4. Pēc aizvēršanas AI lemj uz **katru jaunu pabeigtu M1** sveci

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

137 passed.

## Svarīgi

- LIVE lēmums = **neironu argmax** (BUY/SELL/HOLD), ne vecie 60%/MIN_EDGE vārti
- Labels = 8 min cenas **virziens**, ne garantēta peļņa
- Riskē tikai naudu, ko vari zaudēt
