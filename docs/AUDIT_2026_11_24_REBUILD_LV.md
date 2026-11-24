# Audita pārbūve 24.11.2026 — datu un izpildes pamats

Avots: `AI_Trading_audits_LV.pdf` (zip). Formulas / OHLC / LIVE+Shadow modeļi **saglabāti**.
Pārbūvēts datu un izpildes pamats 1:1 pēc mērķa arhitektūras (audita 8.–9. nod.).

## Jaunie moduļi

| Modulis | Audita punkts |
|---------|---------------|
| `app/capital_rest.py` | P0-01, P1-16 — kopīgs REST budžets, 429, single-flight 401 |
| `app/broker.py` (pārrakstīts uz manager) | P0-01, P1-16 |
| `app/stream.py` | P1-04 — regulārs ping, subscribe ack, broker ts |
| `app/trade_cycle_fsm.py` | P0-12 — posmi 01–10 |
| `app/learn_attempt.py` | P0-13/14 — CLAIMED→…→COMMITTED; UPL neteach |

## Galvenie labojumi esošajā kodā

- **P0-02** — `poll()` vairs nedublē `_retry_pending_broker_pnl` (tikai `sync()`)
- **P0-03** — `runtime_hub` neatkarīgi `_heavy_loop` + `_light_loop`
- **P0-06** — UPL = `ESTIMATE`; mācība tikai no `broker` Funds
- **P0-07** — lotes līdzība neizmet `amount`/`pnl` Funds laukus
- **P0-08** — verificēts £0.00 = SETTLED (Desk + mirror)
- **P0-09** — `currentAccountId` pirms preferred; identity store
- **P0-10** — precīza `deal_id=?` piesaiste (bez `LIKE LIMIT 1`)
- **P0-11** — orphan Funds insert arī `cash_ledger.ingest_funds_row`
- **P1-05** — sveču batch UPSERT; vienreizējs bootstrap
- **P1-15** — pending rinda bez klusas izmešanas
- **P1-17** — Desk: N/A sintētiskiem funding/crowd/fear/RSI/MACD

## Testi

```bash
python3 -m pytest -q tests/test_audit_2026_11_24_rebuild.py \
  tests/test_close_show_upl_not_pending.py tests/test_fast_tick_loop.py
```

## Robežas (no audita)

- Risk limitu / lotes / jaunu stratēģijas filtru **nav** atjaunoti (pasūtītājs noņēmis apzināti).
- LIVE Demo 24h slodzes tests šajā vidē **nav** izpildīts (nav Capital credentials).
