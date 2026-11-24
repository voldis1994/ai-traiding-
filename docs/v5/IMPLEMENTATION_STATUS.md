# V5.0 LIVE — implementation status

V5.0 (2026-11-24) fully replaces V1–V4. Basis: operator schemes + GitHub #270.
Goal: fix the existing LIVE system on real Capital.com — not a new engine/demo.

## Contract (non-negotiable)

- `hard_risk` / `day_risk_gates` stay **informational** (#270). No new HOLD_RISK / HOLD_RR / HOLD_NEWS / HOLD_MODEL / REQUIRED→HOLD OPEN gates.
- Capital Funds £ is the only real PnL. Blank ≠ cash; mid/UPL ≠ realized.
- Structural SL/TP: no RR soft-cap past invalidation; RR is context only.
- SHADOW researches in parallel; must not rewrite LIVE weights or stop LIVE.
- Keep broker dealingRules, Pause/Emergency, double-POST protection.

## Done (align to V5 + post-merge audit fixes)

| Module | Change |
|--------|--------|
| `app/hard_risk.py` | Informational OPEN path (`allowed=True`, `open_gated=False`) |
| `app/math_rules.py` | Mandate size-grid fallback OK — no HOLD_RISK invent |
| `app/engine.py` | Size soft fallback; fusion never OPEN-vetoes; Funds teach gate; scores_raw on submit; cash_ledger ingest on Funds mirror |
| `app/bracket_learn.py` | `allow_soft_shrink=False` on LIVE decide / swing paths (T09) |
| `app/ohlc_regime_brackets.py` | No soft-cap; TP1/TP2 ladder both sides (B03) |
| `app/external_risk.py` | NEWS_WINDOW / calendar status info-only — `entry_allowed=True` |
| `app/autonomy.py` | policy_live_execute fail-closed OFF; SHADOW promote signal only (no LIVE W rewrite) |
| `app/shadow_mistake_audit.py` | Never `commit=True` into LIVE ohlc_scenarios |
| `app/web/admin/admin.js` | Blank ≠ Capital £ (T38) |
| `app/flow_consensus.py` | `DEFAULT_BLOCK_FIGHTS=0` |
| `app/model_readiness.py` | Cold-start after manual close permits LIVE OPEN (T24) |
| `app/ohlc_entry_zones.py` | Zone confirm OR (T06) |
| `app/market_fusion.py` | Stale last-good usable; no TTL→HOLD (T04) |

## Acceptance matrix T01–T39 (V5 reading)

| ID | Focus | Status | Notes |
|----|-------|--------|-------|
| T01 | Gap → no synthetic OHLC | **PASS** | context unready; no invent |
| T02 | Forming bar ≠ mutate context | **PASS** | |
| T03 | M5 aggregation | **PASS** | |
| T04 | External stale/missing → no REQUIRED→HOLD | **PASS** | fusion + calendar info-only |
| T05 | Zone invalidation | **PASS** | |
| T06 | Rejection + micro OR | **PASS** | |
| T07 | Scenario tags | **PASS** | |
| T08 | RANGE mid; UNKNOWN not confirmed | **PASS** | |
| T09 | SL=94.5; no soft-cap; no HOLD_RR | **PASS** | regime + LIVE decide / apply_min_rr |
| T10 | Fakeout SL below sweep | **PASS** | |
| T11 | Neto RR context | **PASS** | |
| T12 | No post-infer flip | **PASS** | |
| T13–T15 | Bid/ask / UNCERTAIN | **PASS** / T14 **XFAIL** | |
| T16 | Partial close | **PENDING** | needs broker harness |
| T17 | hard_risk informational; no HOLD_RISK | **PASS** | |
| T18–T21 | Funds / shadow isolation | **PASS** | promote = signal only |
| T22–T23 / T25–T26 | Crash / checkpoint / cycle | **PENDING** | |
| T24 | Cold-start continues LIVE; no HOLD_MODEL | **PASS** | |
| T27 | Dual client isolation | **PASS** | |
| T28–T39 | Funds ledger fixtures | **PASS** | runtime ingest wired on Funds mirror |
| P16 live Capital Funds API | **NOT RUN** | no credentials in this environment |

## Launch (Windows)

Production start path: **`START.bat` → `run_windows.bat` → `launcher.py` → uvicorn `app.main:app`**.

## Release gate (V5 ch. 23)

| Criterion | Status |
|-----------|--------|
| Funds £ exact / fill from broker (T28–T39 fixtures) | PASS (fixtures); live API NOT RUN |
| No invented OHLC / mid cash / parallel score override | PASS |
| LIVE cycle continues; hard_risk informational | PASS |
| SHADOW parallel, no LIVE weight replace | PASS |
| dealingRules / journal / reconcile / operator Pause | PASS (existing) |
| Windows launch via `run_windows.bat` | PASS (entrypoint preserved) |
