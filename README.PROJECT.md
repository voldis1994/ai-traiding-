> **v6 AUTONOMA AI:** Reālie BUY/SELL/HOLD lemumi tikai no viena apstiprināta, pabeigta M1 bara (M5/M15/H1 kā konteksts), bez fiksētiem 60% un 8.1pp LIVE sliekšņiem. Jauns manuāls Capital.com dealId automātiski nomaina instrumentu un lotu; AI nākamos orderus var iesniegt pēc manuālā deal slēgšanas. Brokeru SL/TP, orderu un ziņu aizsardzība saglabāta. **Pilnā instrukcija:** `V6_AUTONOMA_AI_LV.txt`. Testēts tikai OFFLINE (simulēti orderi); ieteikts DEMO pirms LIVE. Evolved policy ir research/shadow, vairs nav LIVE vārtsargs.

## v5 — Adaptive price-edge validation (no fixed 60% direction hit-rate)

The LIVE entry classifier no longer requires a fixed 60% delayed-label precision.
Instead, it estimates the past 8-completed-minute *signed price move after an
approximate entry/exit spread*, using only fully matured and timestamp-contiguous
non-overlapping signal windows. A conservative uncertainty allowance must be
positive in both earlier and later, chronological forward-held-out periods. This
can admit a sub-60% hit rate when winning moves substantially outweigh losing
moves, and reject a high hit rate when the losses dominate. The metric is a
**price proxy, not executed-trade P&L or validated profit**, and excludes
financing, slippage, commissions and actual broker SL/TP timing. All broker,
news, intermarket, one-manual-first, and order reconciliation vetoes remain.

See `ADAPTIVA_TIRDZNIECIBAS_VALIDACIJA_LV.txt` for Latvian details and cautions.

---

## Neural fusion v4 — cross-market and Forex Factory data train the network

This update expands the recurrent network's input from the existing 10 complete
OHLC features to **18 real features** once verified external data is available:
6 time-aligned peer-market features from Capital.com and 2 Forex Factory
scheduled high-impact-news timing features. The additional input weights are
trained by PyTorch using the existing cost-aware, delayed 8-minute directional
labels, not merely applied as an order-entry veto. Existing v3 learned input
weights are expanded without resetting the network and saved as a backup before
migration. Each EPIC keeps its own v3/v4 model, and training pauses rather than
invents missing external input after v4 activation. Existing entry vetoes,
first-manual-trade requirements and broker SL/TP protections remain intact.

**See `NEIRONU_INTERMARKET_APMACIBA_LV.txt` for exact upgrade and backup steps.**

**Note:** supervised targets estimate 8-minute price direction after bid/ask
spread, *not* profitability or causality; LIVE order placement and SL/TP have
not been confirmed with a real Capital.com session. Test on DEMO first.

---

# NEURAL TRADING AI · Futuristic Neon Windows system

One integrated project with a self-adapting recurrent neural core, automatic Python strategy-code evolution, Capital.com LIVE connector, broker order reconciliation, Windows one-click launcher and a complete local Neon control center. Code is supplied, not compiled for Windows in this Linux build environment.

## Start on Windows — no code editing required

**Important: this project must be fully EXTRACTED before running it.** The `run_windows.bat` file and the `app/` folder must be together. Do not run a BAT or EXE from inside a ZIP preview.

1. Install 64-bit Python **3.11, 3.12 or 3.13** once, from https://www.python.org/downloads/windows/. The Windows installer or `py` launcher should be available. A network connection is required on first launch for Python package installation.
2. Unzip the whole archive and double-click **`run_windows.bat`**. It automatically creates `.venv`, installs required packages including the CPU PyTorch build, starts the FastAPI service and opens the login page at `http://127.0.0.1:8765`.
3. If the browser does not appear but the console says "Uvicorn running", manually open `http://127.0.0.1:8765` in the same Windows computer. If another application already uses port 8765, the launcher now displays this as a clear startup error.
4. Enter Capital.com API Key, Identifier and Password on the local login page. No credential file or code editing is needed.
5. In the Capital.com phone app, open one initial position to specify the instrument and volume. After adoption, the integrated robot handles its own subsequent cycle, subject to broker verification and technical reconciliation conditions.

### If `run_windows.bat` or the Windows EXE fails

The BAT window now **stays open on failure** and tells you which step failed. You can find logs using File Explorer by pasting:

- `%LOCALAPPDATA%\NeuralTradingAI\logs\setup.log` — Python/venv/pip dependency installation errors.
- `%LOCALAPPDATA%\NeuralTradingAI\logs\startup.log` — launcher or web-server Python exception.
- `%LOCALAPPDATA%\NeuralTradingAI\logs\build.log` — PyInstaller build and compiled EXE self-test results.

If Windows does not even show the BAT window, open **Command Prompt** in the unzipped project folder, type `run_windows.bat` and press Enter to keep the console output visible. Windows SmartScreen, antivirus quarantine, blocked files or insufficient permissions can stop program execution *before Python starts*, so these cases will not always create a Python log.

### Build a Windows `.exe`

Run `build_windows_exe.bat` once on the same Windows computer. It installs the packaging tool, builds the full distribution and runs **an offline test on the compiled executable itself** (checks bundled imports, files and neural inference). The build reports SUCCESS *only if that self-test passes*.

**Start the compiled application using `dist\NeuralTradingAI\START_HERE.bat`. Keep the entire `dist\NeuralTradingAI\` directory together.** This is a PyInstaller `--onedir` distribution: a standalone `NeuralTradingAI.exe` copied elsewhere **will not run**, because it needs the nearby `_internal` folder and shared runtime libraries. The application does *not* require you to launch separate services or modules. A packed EXE does *not* require a separately installed Python interpreter at runtime.

The frozen executable also has a dedicated isolated generated-Python-policy checker; previously the code incorrectly attempted to run a `.py` script with `NeuralTradingAI.exe` as if it were a Python interpreter. That packaged-runtime defect is fixed.

**Test limits:** development tests ran on Linux; native Windows binary startup has not been reproduced in this environment. The updated Windows scripts and compiled binary self-test are designed to reveal the exact Windows-specific failure. Capital.com LIVE order execution still needs real broker verification.

On each application start a fresh login is required; credentials stay in RAM only. A session-only HttpOnly cookie is used for the local browser. UI binds only to 127.0.0.1:8765. PAUSE, RESUME, EMERGENCY STOP/CLOSE and LOG OUT controls remain available.

## Architecture

- `launcher.py` — opens browser and starts one local FastAPI server.
- `app/main.py` — session/auth, background automatic worker, operator controls, WebSocket updates and REST/UI.
- `app/broker.py` — Capital.com LIVE REST login, account/position/market/candle API, order requests, confirmations, update and close. Non-idempotent requests are never automatically retried after ambiguous transport outcomes.
- `app/stream.py` — authenticated Capital.com market-data WebSocket quotes with reconnection; REST quotes provide fallback and broker-specific dealing rules.
- `app/engine.py` — durable order journal; broker-confirmed single-position trade cycle, first manual trade adoption, reconnect reconciliation, live candle log, delayed-label training, strategy evolution and SQLite storage.
- `app/core.py` — PDF dynamical equations: normalized z, h and c time constants, BUY/SELL/HOLD softmax, reconstruction G, automatic neuron birth and utility-based pruning, one-step PyTorch gradient through P/W/R/O/b/tau/tau_c/G and persistent structural tensors. Broker-confirmed exit levels, where present, also provide delayed directional outcome feedback.
- `app/math_rules.py` — PDF stop-loss/take-profit tick rounding; PDF risk-quantity formula and signed cost-adjusted P&L math. **The adopted manual size takes precedence** over risk-derived alternative quantities, because that is the requested sizing rule.
- `app/autonomy.py` — creates different whole Python decision-policy modules automatically, syntax-checks with a restricted AST, executes isolated smoke checks, optionally compares candidates on delayed label replay, atomically promotes a candidate and preserves rollback policy. It does **not** arbitrarily rewrite authentication, order execution or its own test harness.
- `app/web/index.html`, `app/web/style.css`, `app/web/ui.js` — offline capable browser Neon dashboard, real quote-based and historical OHLC candlesticks, all management/history/AI/code pages, authenticated WebSocket updates.
- `app/storage.py` — writable memory/state store: in source runs in `data/`, in Windows packaged runs under `%LOCALAPPDATA%\NeuralTradingAI`.
- `tests/` — isolated simulations and numerical/integration tests without real money or credentials.

## Automatic operations

After login, the application polls the broker every 3 seconds, starts subscribed price streaming, attempts to adopt the one mobile position, attaches missing protective SL/TP, collects actual candlesticks, updates online recurrent neural parameters and neural utility, resizes the network at most once every 25 observations when reconstruction/utility triggers fire, and evolves Python strategy policy after enough delayed labels and candidate checks. Checkpoints are saved to disk and loaded on the next program run. A WebSocket sends UI updates about every two seconds; REST is the fallback.

A BUY/SELL/HOLD action is treated as a score, not a scientifically validated prediction. HOLD means no new position. On exit, the broker confirms/reconciles an empty position list before a fresh signal may open a new position. A crash or uncertain broker response leaves a durable record that must be reconciled before any further submission; the bot does not duplicate unknown-status orders.

## Source PDF, model assumptions and numerical implementation

Source: `Parbauditas_Sakartotas_Formulas(1).pdf` included in project under `reference/`.

- State is `(h,c,P,W,R,O,b,G,tau,tau_c)`; For the legacy v3 market model, x(t) is a **10-dimensional, instrument-relative market-context vector** from completed 1-minute OHLC: 1/5/15/60-minute returns, EMA relationships, 15-minute volatility, 60-minute range position, final candle body and spread. The older three-input model is kept only for backward-compatible low-level tests and archived neural checkpoints.
- Targets `y_t` are not defined by the source; training uses a **delayed eight-observation price-direction proxy**, with an explicit HOLD label when the *entire* eight-step move does not exceed the observed bid/ask spread (all compared in price units). This is not a ground-truth optimum for trading.
- Gradient uses a one-step differentiable unroll through current recurrent state, not full unlimited-time BPTT across the entire transaction history. Parameters are clipped for numerical stability.
- SL/TP calculations conform to tick rounding and the kappa multiplier. Broker dealing minimum distance is handled for POINTS or PERCENTAGE rules. If an instrument provides unknown dealing constraints, automated submission is withheld rather than inventing rules.
- The risk quantity formula is available for analysis; its max-risk and tick-value inputs depend on a trading account/instrument and cannot be manufactured. Consistent with your specification, trade volume comes from the first mobile order instead.
- Unrealized P&L is taken from the broker when returned. Realized P&L is left blank unless it can be grounded in a broker confirmation; no invented monetary profit numbers.

## Testing

From the project root run `python -m pytest -q`. Tests cover PDF numerical sizing/stops/P&L, gradients, dynamic neuron resize and state persistence, policy-source generation and code-safety checks, API payload fields and session tokens, mock first trade adoption/protection/exit/reentry, uncertain-response duplicate prevention, multi-position stop and UI authorization.

**LIVE broker validation is still required before treating execution as verified:** no Capital.com credentials or broker-session access were available in this development environment. These tests **do not** prove correctness of actual order acceptance for every instrument, real-world strategy profit or suitability for unattended exposure. The code attempts full automatic operation upon a LIVE connection, but live network/exchange behavior has not been demonstrated. Only use real funds after verifying the implementation with your broker and understanding the risks.


## Learning integrity repair (2026-10-13)

- **Instrument isolation:** Each EPIC has a distinct saved recurrent neural state (`brain.npz` for the original bound instrument; `brain_<hash>.npz` for additional EPICs). On a change of instrument the appropriate network and its pending-label queue are restored. Two different instruments' mid-prices are never subtracted. The source code evolution policy remains shared, but its replay input is the active EPIC only.
- **Eight-observation training horizon:** The old code compared *one* poll-to-poll change against the full spread and incorrectly labeled gradual directional price trends as HOLD. Delayed BUY/SELL/HOLD labels now compare price delta and spread using the same PRICE units, over eight observed market quotes. This choice is a training proxy, not a validated prediction horizon or guarantee of profitability.
- **No discarded gradients after births/pruning:** Delayed samples capture stable neuron IDs; training remaps to surviving current neurons and zero-fills newborns. New neuron creation is throttled to once per 25 observations, even if reconstruction error remains high.
- **Restart continuation:** Neuron IDs, pending multi-observation examples and network matrices are checkpointed atomically after each quote. Durable labeled accuracy and the last 1000 signals are reconstructed from SQLite at startup. Earlier one-poll labels are retained for audit but excluded from the new training statistics and evolution replay.
- **Unit checking:** The adaptive threshold and evolution replay measure spread in **basis points** (`10,000 * spread / mid`), not ticks. A confirmed trade-outcome label uses its **entry EPIC and saved entry tick size**; it does not subtract spread twice from executed entry/exit levels. Old entry records without these metadata are skipped, not guessed.
- **Trading mode unchanged:** Capital.com LIVE integration, manual-first adoption, order journaling, broker confirmation, SL/TP and one-position restrictions remain in place. No DEMO broker or real Capital.com validation was used for this fix.

**UPGRADE WARNING:** Do not overwrite your existing `data/` directory or `%LOCALAPPDATA%\NeuralTradingAI` when installing. In particular retain `robot.sqlite3`, the existing `brain.npz`, any `brain_*.npz` and `active_policy.py`. Copy only the updated source files into your backed-up installed project, or extract the archive to a new folder and copy your backed-up data into it before starting. Database migration occurs automatically on first start; take a backup while the robot is STOPPED. Legacy one-step training labels will not count toward the new learning threshold.

## Capital.com API reference

Official docs: https://open-api.capital.com/ . Relevant endpoints: POST /api/v1/session, GET /api/v1/accounts, GET /api/v1/positions, GET /api/v1/markets/{epic}, GET /api/v1/prices/{epic}, POST /api/v1/positions, PUT/DELETE /api/v1/positions/{dealId}, GET /api/v1/confirms/{dealReference}. Capital.com streaming: `wss://api-streaming-capital.backend-capital.com/connect`.

## Relative BUY/SELL/HOLD LIVE gate repair (2026-10-13)

This update removes the structural **absolute 60% BUY/SELL confidence requirement** from the project's *automatically generated entry policies*. BUY, SELL and HOLD still compete in the neural network; the policy now evaluates the strongest directional score relative to its nearest alternative, using a score margin (`MIN_EDGE`, default 0.06 = **six percentage points of model-score difference**). It does **not** interpret 43% neural output as a calibrated 43% chance of profitability.

- The evolved margin is adjusted moderately for historical label accuracy and broker spread in **basis points**. A moderate but clearly dominant signal can qualify; a direction tied with BUY/HOLD cannot. The UI identifies the metric as *Directional lead* rather than an absolute confidence threshold.
- Crucially, a directional proposal is **not** a live order. Before the first autonomous BUY or SELL, the program requires at least 64 resolved, same-instrument, eight-observation delayed labels and at least eight approximately non-overlapping historically similar proposals **for that direction**, of which at least 60% match the later price-direction proxy label. These are deliberately imperfect label-proxy checks, not broker net-P&L profitability validation. When data is insufficient or contradicts the proposal, the dashboard gives the specific blocking reason.
- Each `poll()` and `/api/status` evaluation invokes the **same** `AutoEvolution.evaluate_entry()` decision function. After a broker-confirmed close of the first adopted manual position, a validated proposal can reach the existing real `broker.open_position()` method (journaled before send, authenticated Capital.com LIVE, broker-confirmed and reconciled). Only one concurrent trade is allowed. This is verified with fake-broker integration tests for **both BUY and SELL** using moderate 43.3% model scores against 36.6% runner-up scores. **No real orders were transmitted during development or tests.**
- Existing generated `active_policy.py` files with recognized old auto-generated confidence templates are **backed up** to `active_policy.before_edge_v2.py` and migrated to new `MIN_EDGE` code at startup. Old `robot.sqlite3`, model weights and history remain. Unknown hand-written policies are not silently modified; if you use one, confirm the actual policy source in the Code Evolution tab.
- Candidate search now explores several relative-edge structures; candidates are compared on class-balanced labeled historical replay **and a chronological holdout**, without bypassing the relative-margin check. HOLD remains a valid action. Autonomous trading is not artificially forced.
- Broker-cycle exception events now include the exception class and Python source filename/line number, *not credentials or sensitive HTTP request content*.

### Install over an existing LIVE Windows project

**STOP** the existing running robot and make a backup of the entire project first. Never run two instances connected to the same account simultaneously. Extract this full ZIP into a new folder, then bring forward the old state/data directory (including `robot.sqlite3`, `brain.npz`, `brain_*.npz`, and `active_policy.py`) before launching. If using a compiled Windows app, the writable state directory may instead be `%LOCALAPPDATA%\NeuralTradingAI`. Inspect Code Evolution for `MIN_EDGE` after login. `Entry eligible: YES` means that the broker execution system is technically ready; only `Policy decision: BUY` or `SELL`, with a successful historical direction check, signifies an **approved proposal**, and an accepted/reconciled **OPEN** order is the final proof of execution. `PROTECT` confirmation only updates the existing manual position.

**Risk limitation:** automatic Capital.com LIVE execution has not been verified against real broker responses in this environment. The replay target is based on price direction after eight completed minute candles and includes spread as a price-move threshold, but it does not model slippage, overnight financing or commissions and is **not** proof that a strategy is profitable. Do not infer a trade will open on any particular signal, or rely on the software for unattended financial decisions without independent verification.


## v3 — WHOLE-MARKET / MULTI-TIMEFRAME LEARNING (2026-10-13)

- **No single active candle as AI training:** A fresh decision is made once per newly completed, broker-historical MINUTE candle; the current incomplete quote-based candle is shown in the chart only.
- **1, 5, 15 and 60-minute context:** All four horizons are rolling lookbacks derived from the actual Capital.com historical M1 OHLC bars. Other inputs describe local trend, volatility, spread, price position within the 60-minute range and candle body. Signals are not simply the color of the current candle.
- **History readiness:** At least 61 consecutive completed M1 candles are needed. Historical candles are requested from Capital.com (`maximum=180`); if the broker does not provide enough recent, continuous data the AI reports WAIT and does not submit new orders. The first download may already provide sufficient candles; it is not necessary to sit for 61 minutes if the historical endpoint supplies them.
- **Aligned 8-minute labels:** The supervised target compares two completed-bar closes separated by eight newly observed, consecutive 1-minute bars. Missing minutes invalidate the unfinished target; repeated 3-second quote polls never create duplicate training labels.
- **Provenance protection:** Broker-historical OHLC and provisional quote-chart candles carry distinct SQLite source markers; legacy candles without provenance are not treated as historical training samples.
- **Breaking neural input schema:** Old `brain*.npz` weights were trained on incompatible 3-feature units. On first available valid OHLC context, the robot automatically copies that weight file to `*.before_mtf_v3.npz` and initializes a fresh 10-feature neural model. Previous broker orders and labeled history remain in SQLite for audit, but pre-v3 labels are not counted toward the new supervised objective. This is an intentional partial neural restart **not a wipe of your database**.
- **Evolution:** Each new labeled 20-sample checkpoint is evaluated at most once; repeated REST polling does not count as new evolutionary data. The existing candidate/holdout safeguards remain.
- **Position safety unchanged:** The initial manual position is still adopted and protected with SL/TP even while there is insufficient candle history. AUTO LIVE entry still requires a broker-tradeable market, first manual trade closure, a validated model proposal, risk rules and order confirmation/reconciliation. Historical replay is not calibrated profit prediction.

**Installation:** Close the old robot and back up its `data/` folder. Extract the complete project to a NEW folder. Copy your own stopped-project `data/` files (not a corrupted `robot.sqlite3`) only if you wish to keep your history and order journal. This ZIP intentionally contains **no** database, neural checkpoint, evolved-policy file, API key, or password. Run `run_windows.bat`. Do not run two versions concurrently.

**Test scope:** Automated/offline tests do not establish correct operation with a live Capital.com brokerage account and cannot promise that a trade will be submitted or profitable.


## 2026-10-14 — NO-ENTRY DIAGNOSTICS + RECONNECT AUTHORIZATION

This patch addresses two real routing defects after the v3 minute-bar upgrade:

- **Directional evidence phase aliasing:** Rather than sampling the history at a fixed `::8` offset (which can systematically miss valid BUY/SELL examples), the evaluator first identifies past proposals matching the requested direction, then selects chronologically spaced predictions at least 8 completed bars apart. It still requires 64 delayed labels, 8 non-overlapping direction-specific observations and historical direction-label precision >= 60%. It **does not** force LIVE trades, silently lower a quality gate, or claim this precision predicts profitability.
- **Manual authorization lost across application reconnects:** The engine can resume an initial adopted manual position's authorization if and only if the exact initial deal ID, instrument and size match a trade **durably recorded as closed by the engine** in SQLite. If the app was closed before observing the broker-confirmed close, it will NOT infer closure solely from an empty positions list, and a new manual adoption or reconciled evidence will be needed.
- **Traceable barriers:** System Reactions gets one `entry` event per completed model step with the proposal, actual validated action, required margin, measured directional edge, evidence counts and exact hold reason. Market Analysis separates broker execution readiness from signal validation and the actual order-submission gate. These are non-secret diagnostics: no Capital.com credentials or tokens are logged.

Install into a NEW directory while the robot is completely stopped. Preserve your existing `data/` directory (`robot.sqlite3`, WAL/SHM if present, `brain*.npz`, `active_policy.py`) as a complete copy before migration; do not import a corrupted SQLite database. Do not reset your neurons. Stop any second running instance of the robot first. This archive ships only code and empty-state scaffolding, not your brokerage state. Test results use offline mocks only; actual LIVE account acceptance, broker rules and strategy profitability are not established by these tests.


## Directional LIVE entry margin v4 (calibrated, not forced)

The evolved `MIN_EDGE` score margin is no longer the only possible entry
threshold. For a candidate BUY/SELL the engine considers smaller margins
(1--5 percentage points) **only after** an 8-candle-label chronological
train/holdout validation: at least 96 resolved labels overall, 8 independent
directional train examples and 4 independent held-out directional examples,
with >=60% correctly labeled direction in each period and an 8-bar embargo
between train and validation. Margin selection never uses the holdout to
choose a parameter; it is used only for final approval. If the evidence is
insufficient or fails, the evolved margin stays in effect. Near ties such
as a 0.4 pp lead remain HOLD even if 1 pp is empirically validated.

This is a directional *proxy* score, not a calibrated win probability or
expected profit. Existing broker, protection and reconciliation gates stay
active. `Market Analysis` displays original/effective margins and evidence.
Existing SQLite, neural weights, and active policies are intentionally not
included in the ZIP. **No real Capital.com order was placed in testing.**

## New: adaptive, history-validated SL/TP management (LIVE positions)

See `ADAPTIVA_SL_TP_LV.txt` for Latvian setup and limitations. This is an
**empirical policy selector, not ATR and not a second neural-network profit
predictor**. It compares stop-ratcheting/trailing profit-retention policies and
optional TP extension against the initial broker bracket. Historical completed
MINUTE candles are split in chronological, non-overlapping 8-minute windows;
policy parameters are selected on earlier paths and accepted only when they
improve a separate later holdout. Intracandle ambiguity assumes stop-first fills.

Only positions confirmed opened by this updated robot get adaptive enrollment.
Manual positions and older already-open positions keep their previous management
and SL/TP. Candidates modify levels only after a favorable movement, never
widen the existing stop and never remove the original protection. TP extension
requires aligned completed 5/15-minute trend and successful held-out selection.
No update is submitted if the broker distance rule is unavailable or the live
price is already past the TP. Rejected modifications retain the old bracket;
uncertain responses pause automated trading until the order journal and broker
can reconcile. The panel displays adaptive-protection status separately from
**broker-reported actual** stop and take-profit levels.

The algorithm uses selected historical paths, not real tick-level fills,
commissions or a risk-calibrated forecast. It cannot promise an optimal exit,
limit slippage/gaps, or improve long-run profitability. LIVE broker modifications
have not been exercised from this development environment.


## Forex Factory + intermarket update (2026-10-14)

See `FOREX_FACTORY_INTERMARKET_LV.txt` for configuration, automatic Forex Factory
weekly JSON source, Capital.com reference-market checks, entry-safety behavior,
manual-first instrument switching, and safe migration of existing data.

This build intentionally does **not** bundle `robot.sqlite3`, `brain.npz` or
`active_policy.py`. The operator must preserve their own backed-up data.
