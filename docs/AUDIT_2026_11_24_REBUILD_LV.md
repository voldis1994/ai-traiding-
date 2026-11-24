# Audita pārbūve 24.11.2026 — datu un izpildes pamats

Avots: `AI_Trading_audits_LV.pdf` (zip). Formulas / OHLC / LIVE+Shadow modeļi **saglabāti**.
Pārbūvēts datu un izpildes pamats 1:1 pēc mērķa arhitektūras (audita 8.–9. nod.).

## Jaunie / paplašinātie moduļi

| Modulis | Audita punkts |
|---------|---------------|
| `app/capital_rest.py` | P0-01, P1-16 — REST budžets, prioritātes, 429, LIVE/DEMO izolācija |
| `app/broker.py` | P0-01 — visas HTTP caur manager |
| `app/stream.py` | P1-04 — ping, subscribe ack, broker ts |
| `app/trade_cycle_fsm.py` + `cycle_runtime.py` | P0-12 — LIVE/HOLD cikls faktiskajā engine ceļā |
| `app/learn_attempt.py` + `learn_correction.py` | P0-13/14 — CLAIMED→COMMITTED; recovery; korekcija |
| `app/work_queue.py` | P1-15 — noturīga rinda, backoff, diagnostika |
| `app/offloop.py` | item 11 — smagais SQLite/AI ārpus event loop |
| `app/telemetry.py` | item 15 — posmu aizkave, API/s, cikla pabeigšana |

## Galvenie labojumi esošajā kodā

- **P0-02** — `poll()` vairs nedublē `_retry_pending_broker_pnl` (tikai `sync()`)
- **P0-03** — `runtime_hub` un `main.worker` neatkarīgi heavy/light cikli
- **P0-06** — UPL = `ESTIMATE`; mācība tikai no Cash Ledger / broker Funds
- **P0-07** — lotes līdzība neizmet `amount`/`pnl` Funds laukus
- **P0-08** — verificēts £0.00 = SETTLED (Desk + mirror + ledger)
- **P0-09 / item 6** — valūta tikai no konta; hint nekad nepārraksta
- **P0-10 / item 4** — precīza `deal_id` piesaiste; citādi UNMATCHED
- **P0-11** — orphan Funds → `cash_ledger` UNMATCHED
- **item 3** — `quote_upl_tick` uz `_light_lock` (ne `engine.lock`)
- **item 5** — Desk `_trade_stats` overlay no Cash Ledger
- **P1-15 / item 10** — durable `work_queue` + legacy pending mirror
- **P1-17 / item 12** — Desk N/A sintētikai; `npm run build` → `app/web/desk`

## Testi

```bash
python3 -m pytest -q tests/test_audit_303_integration.py \
  tests/test_audit_2026_11_24_rebuild.py \
  tests/test_close_show_upl_not_pending.py tests/test_fast_tick_loop.py
```

Integrācijas testi (`test_audit_303_integration.py`) pārbauda uzvedību, ne pirmkoda teksta esamību.

## Robežas (no audita)

- Risk limitu / lotes / jaunu stratēģijas filtru **nav** atjaunoti (pasūtītājs noņēmis apzināti).
- LIVE Demo 24h slodzes tests šajā vidē **nav** izpildīts (nav Capital credentials).
