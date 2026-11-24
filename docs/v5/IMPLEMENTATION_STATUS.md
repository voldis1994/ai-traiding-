# V5.0 LIVE — implementation status

V5.0 (2026-11-24) fully replaces V1–V4. Basis: operator schemes + GitHub #270.
Goal: fix the existing LIVE system on real Capital.com — not a new engine/demo.

## Contract (non-negotiable)

- `hard_risk` / `day_risk_gates` stay **informational** (#270). No new HOLD_RISK / HOLD_RR / HOLD_NEWS / HOLD_MODEL / REQUIRED→HOLD OPEN gates.
- Capital Funds £ is the only real PnL. Blank ≠ cash; mid/UPL ≠ realized.
- Structural SL/TP: no RR soft-cap past invalidation; RR is context only.
- SHADOW researches in parallel; must not rewrite LIVE weights or stop LIVE.
- Keep broker dealingRules, Pause/Emergency, double-POST protection.

## Done in this branch (align to V5)

| Module | Change |
|--------|--------|
| `app/hard_risk.py` | Restored informational OPEN path (`allowed=True`, `open_gated=False`) |
| `app/math_rules.py` | Mandate size-grid fallback OK — no HOLD_RISK invent |
| `app/engine.py` | Size-plan soft fallback; hard_risk note only; cold-start unlocks LIVE |
| `app/model_readiness.py` | Cold-start after manual close permits LIVE OPEN (T24) |
| `app/ohlc_regime_brackets.py` | No soft-cap SL; no HOLD_RR veto (T09) |
| `app/ohlc_entry_zones.py` | Zone confirm OR (T06); invalidation kept (Z03) |
| `app/market_fusion.py` | Stale last-good usable; honest timestamp; no TTL→HOLD (T04) |
| Funds / shadow isolation / scores_raw / no synthetic OHLC | Kept from prior P0 |

## Acceptance matrix T01–T39 (V5 reading)

| ID | Focus | Status | Notes |
|----|-------|--------|-------|
| T01 | Gap → no synthetic OHLC | **PASS** | `market_features` unready |
| T02 | Forming bar ≠ mutate context | **PASS** | |
| T03 | M5 aggregation | **PASS** | |
| T04 | External stale/missing → no REQUIRED→HOLD | **PASS** | last-good kept with age |
| T05 | Zone invalidation | **PASS** | |
| T06 | Rejection + micro as separate evidence; no AND | **PASS** | OR confirm |
| T07 | Scenario tags | **PASS** | |
| T08 | RANGE mid; UNKNOWN not confirmed | **PASS** | |
| T09 | SL=94.5; no soft-cap; no HOLD_RR | **PASS** | |
| T10 | Fakeout SL below sweep | **PASS** | |
| T11 | Neto RR context | **PASS** | |
| T12 | No post-infer flip | **PASS** | |
| T13–T15 | Bid/ask / UNCERTAIN | **PASS** / T14 **XFAIL** | |
| T16 | Partial close | **PENDING** | needs broker harness |
| T17 | hard_risk informational; no HOLD_RISK | **PASS** | |
| T18–T21 | Funds / shadow isolation | **PASS** | |
| T22–T23 / T25–T26 | Crash / checkpoint / cycle | **PENDING** | |
| T24 | Cold-start continues LIVE; no HOLD_MODEL | **PASS** | unlocks ready |
| T27 | Dual client isolation | **PASS** | |
| T28–T39 | Funds ledger fixtures | **PASS** | |
| P16 live Capital Funds API | **NOT RUN** | no credentials in this environment |

## Release gate (V5 ch. 23)

| Criterion | Status |
|-----------|--------|
| Funds £ exact / fill from broker (T28–T39 fixtures) | PASS (fixtures); live API NOT RUN |
| No invented OHLC / mid cash / parallel score override | PASS |
| v6 LIVE cycle continues; hard_risk informational | PASS |
| SHADOW parallel, no LIVE replace | PASS |
| dealingRules / journal / reconcile / operator Pause | PASS (existing) |
