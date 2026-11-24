# V3 Capital Funds — implementation status

Snapshot for P0 modules + acceptance coverage (T01–T39). Engine / runtime_hub full wire-up remains partial.

## Done (this change)

| Module / suite | Covers |
|----------------|--------|
| `app/realized_cash.py` | P05–P08, P10, P14 money helpers |
| `app/cash_ledger.py` | P09, P11–P13, P15 ledger |
| `app/market_context.py` | D02 gap → unready (HOLD_DATA class) |
| `app/ohlc_entry_zones.py` | Z03 invalidation, Z04 AND-confirm |
| `app/ohlc_scenarios.py` | S08 HOLD_SCENARIO, mid-range HOLD |
| `app/ohlc_regime_brackets.py` | B01 HOLD_RR, fakeout SL |
| `app/shadow_paper.py` | B05/B08 bid-ask TP/SL |
| `app/flow_consensus.py` | L01 no post-infer BUY↔SELL flip |
| `app/hard_risk.py` / `math_rules` | R02 HOLD_RISK size/budget |
| `app/model_readiness.py` | V04 cold-start ≠ LIVE ready |
| `app/engine.py` | Isolated SHADOW, UNCERTAIN blocks 2nd OPEN |
| `tests/test_v3_acceptance_t01_t27.py` | Fixture T01–T27 (no live Capital) |
| `tests/test_v3_funds_t28_t39.py` | Fixture T28–T39 |
| `tests/test_v3_engine_contract.py` | H01 / L01 / R02 / A03 engine contract |

## Acceptance matrix T01–T39

| ID | Focus | Status | Notes |
|----|-------|--------|-------|
| T01 | Gap → HOLD_DATA / no synthetic | **PASS** | `market_features` unready + `features=None` |
| T02 | Forming bar ≠ mutate context | **PASS** | `market_features` + `drop_forming_minute` |
| T03 | M5 aggregation complete vs gap | **PASS** | `resample_m1_ohlc` |
| T04 | Last-good TTL expiry | **XFAIL** | Fusion carry-forward has no TTL clock API yet |
| T05 | Zone invalidation | **PASS** | `zone_invalidation_status` / `_pick_best_zone` |
| T06 | Zone AND confirm | **PASS** | `zone_confirm_allows` |
| T07 | Scenario tags (regimes) | **PASS** | Fixtures + `classify` / `open_meta_from_scenario` |
| T08 | RANGE mid HOLD; UNKNOWN HOLD | **PASS** | `mid_range_blocked` + `HOLD_SCENARIO` |
| T09 | HOLD_RR not SL move | **PASS** | `regime_brackets` keeps SL=94.5 |
| T10 | Fakeout SL below sweep | **PASS** | FAKEOUT path SL ≤ 97.8 |
| T11 | Neto RR B07 example | **PASS** | `trade_costs.fill_price` → 1.4 < 1.5 |
| T12 | No post-infer flip | **PASS** | `flow_consensus.apply_to_decision` |
| T13 | Bid/ask TP not hit | **PASS** | `shadow_paper.hit_sl_tp` |
| T14 | Pending delay cancel on RR | **XFAIL** | Gate cancel exists; RR re-check at fill pending |
| T15 | UNCERTAIN blocks 2nd OPEN | **PASS** | `SingleTradeEngine.submit` + fake broker |
| T16 | Partial close / netting | **PENDING** | Needs live DEMO / broker partial-close harness |
| T17 | HOLD_RISK size/budget | **PASS** | `position_size` + `hard_risk.entry_allowed` |
| T18 | Pending vs SETTLED_ZERO | **PASS** | `realized_cash` / `cash_ledger` |
| T19 | Path-less no OHLC label | **PASS** | `sample_strategies.ingest_trades` empty tape |
| T20 | Shadow no broker write | **PASS** | `ShadowPaperJournal` + guard broker |
| T21 | Shadow learn ≠ LIVE weights | **PASS** | Engine `_shadow_learn` |
| T22 | Crash after CLAIMED restore | **PENDING** | Learn-gate crash/replay harness not in suite |
| T23 | Corrupt checkpoint restore fail | **PENDING** | Registry/checkpoint restore tests elsewhere |
| T24 | Cold start ≠ LIVE ready | **PASS** | `grant_cold_start_open` |
| T25 | Purge/embargo / REJECT | **PENDING** | Model-validation holdout purge not wired here |
| T26 | Full BUY/SELL cycle + restart | **PENDING** | End-to-end DEMO cycle (not fixture-only) |
| T27 | Dual client isolation | **PASS** | Separate Store / seed / hard_risk |
| T28 | Verified Funds wins | **PASS** | `test_v3_funds_t28_t39` |
| T29 | SELL sign / ignore proxy | **PASS** | |
| T30 | SETTLED_ZERO vs pending | **PASS** | |
| T31 | UPL/confirm without Funds null | **PASS** | |
| T32 | MATCH_UNCERTAIN then link | **PASS** | |
| T33 | Currency mismatch | **PASS** | |
| T34 | Correction pence exact | **PASS** | |
| T35 | Duplicate / per-account | **PASS** | |
| T36 | Actual fill exit price | **PASS** | |
| T37 | Partial close cash events | **PASS** | |
| T38 | Forbidden LIVE sources | **PASS** | |
| T39 | Consumer snapshot / outbox | **PASS** | |

**Suite run (fixture):** `42 passed, 2 xfailed` for  
`tests/test_v3_acceptance_t01_t27.py` + `tests/test_v3_funds_t28_t39.py` + `tests/test_v3_engine_contract.py`.

## Remaining gaps

- **T04** — expose last-good TTL on fusion REQUIRED/OPTIONAL feeds.
- **T14** — re-validate neto RR at pending fill; cancel without rewriting absolute SL/TP.
- **T16 / T22 / T23 / T25 / T26** — DEMO / crash / validation / full-cycle harnesses.
- **P16** live DEMO Funds statement verification — **NOT RUN**.
- Engine consumers still partially on legacy PnL paths (P17).

## Engine wiring (later)

Call `ingest_funds_row` / `append_event` from the Capital transactions adapter; resolve LIVE teach via `deal_realized` + `may_teach_live_cash`; replace Desk sums with `consumer_snapshot` / `sum_account`.
