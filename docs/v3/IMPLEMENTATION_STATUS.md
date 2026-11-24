# V3 Capital Funds — implementation status

Snapshot for P0 modules only (engine / runtime_hub wiring is out of scope here).

## Done (this change)

| Module | Covers |
|--------|--------|
| `app/realized_cash.py` | P05–P08, P10, P14 money helpers: `to_minor` / `from_minor`, forbidden LIVE sources, `classify_pnl_source` (blank ≠ cash), `parse_broker_cash_amount` (OK / PENDING / SOURCE_CONFLICT / QUARANTINE), fill vs estimate exit, `pick_live_realized` |
| `app/cash_ledger.py` | P09, P11–P13, P15 ledger: CashEvent schema, Store / `MemoryStore`, idempotent `append_event`, `sum_account` / `sum_trade_results`, UNMATCHED + `MATCH_UNCERTAIN`, `SETTLED_ZERO` vs pending null, revision corrections, outbox crash safety |
| `tests/test_v3_funds_t28_t39.py` | Fixture-only T28–T39 against the modules above (no live Capital) |

## Not in this change

- `engine.py` / `runtime_hub.py` consumers still use legacy PnL paths (other agents).
- P16 live DEMO / Funds statement verification — **NOT RUN**.
- Full T01–T27 and end-to-end Desk/API/SQLite consumer matrix — pending engine wire-up (P17).

## Engine wiring (later)

Call `ingest_funds_row` / `append_event` from the Capital transactions adapter; resolve LIVE teach via `deal_realized` + `may_teach_live_cash`; replace Desk sums with `consumer_snapshot` / `sum_account`.
