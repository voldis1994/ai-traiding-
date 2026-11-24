# Neural Trading AI — pilnā sistēmas arhitektūra

Šis dokuments ir **full map** no Windows start līdz Capital Funds £ mācīšanai.
Avots: `main` pēc PR #180…#267 (+ sekojošie). LIVE Capital sesija šajā tekstā nav
pārbaudīta — tikai koda un git inventārs.

---

## 1. Kas tā ir (vienā rindā)

Viens FastAPI process ar **multi-tenant** Capital.com engine katram klientam.
LIVE lēmums: **NeuralCore argmax → hard gates → broker**. Mandāts no pirmā manuālā
deals. Peļņa māca tikai no **Capital Funds £**. SHADOW dalās ar **to pašu NeuralCore**.

| Konstanta | Vērtība |
|-----------|---------|
| Poll | ~3 s (`RuntimeHub` worker) |
| Smadzenes | 1 NeuralCore LIVE + SHADOW |
| SL/TP | Tikai OHLC swing (trail OFF) |
| Peļņa | Tikai Funds £ (ne UPL / confirm) |

---

## 2. 11 slāņi (karte)

| # | Slānis | Kas notiek | Faili |
|---|--------|------------|-------|
| 1 | Ieeja | `START.bat` → `run_windows.bat` → `launcher.py` → uvicorn | `START.bat`, `launcher.py` |
| 2 | Process | FastAPI lifespan: admin bootstrap + `hub.start_all_active()` | `app/main.py` |
| 3 | Multi-tenant | Katram klientam Store + Brain + Engine + QuoteStream | `runtime_hub.py`, `platform.py` |
| 4 | Broker | Capital.com REST + WS quotes (nav retry non-idempotent) | `broker.py`, `stream.py` |
| 5 | Poll | sync → protect → features → decide → open/manage/close → learn | `engine.py` |
| 6 | Smadzenes | NeuralCore BUY/SELL/HOLD + AdaptiveBrain per-epic | `core.py`, `engine.py` |
| 7 | Vārti | news · day_book · edge · reentry · regime · zone · swing RR | `ohlc_*`, `unified_cycle.py` |
| 8 | Izpilde | journal PENDING → `open_position` → confirm → protect SL/TP | `broker.py`, `engine.py` |
| 9 | Peļņa | Tikai Capital Funds £ — nekad UPL/confirm invent | `_resolve_broker_pnl` |
| 10 | Mācīšanās | profit → learn_gate → brain + shadows + day_book settle | `_learn_closed_trade` |
| 11 | Desk | Admin / PerpSignal Desk / Client portal / WS | `app/web/`, `perpsignal-desk/` |

---

## 3. ASCII — pilna plūsma

```
START.bat / PALAIST_PILNO_SISTEMU.bat
    │  kill :8765, NTA_SECURE_COOKIES=0
    ▼
run_windows.bat  →  .venv + pip  →  launcher.py
    │                    Docker/Render: uvicorn app.main:app :PORT
    ▼
app/main.py lifespan
    ├─ PlatformStore bootstrap admin (platform.sqlite3 + secret.key)
    ├─ legacy worker() idle until /api/connect (prod disabled)
    └─ RuntimeHub.start_all_active()
            │
            ▼  per client
    AccountRuntime
      Store(robot.sqlite3) + AdaptiveBrain(brain*.npz)
      + SingleTradeEngine + CapitalQuoteStream
            │
    ┌───────┴──────────────────────────────────────────────┐
    │  every ~3s: worker → engine.poll()                   │
    │  WS: stream.py → bid/ask (REST fallback)             │
    └───────┬──────────────────────────────────────────────┘
            ▼
  sync / reconcile orders ──► CapitalBroker (REST LIVE)
            │
  protect SL/TP (OHLC swing; TRAIL HARD OFF)
            │
  candles + market_context + fusion
    (intermarket + calendar_feed + liquidity_feed → market_fusion)
            │
  NeuralCore.step → scores BUY/SELL/HOLD
            │
  soft nudges: adaptive_system / structure_edge / gold /
               situation_memory / strategy_gen / mtf / flow_consensus /
               forecast_bridge (optional)
            │
  AutoEvolution.evaluate_neural_entry (+ policy.py if policy_live_execute)
            │
  LIVE OPEN gates (any HOLD stops):
    readiness → news/intermarket → day_book → unified_cycle edge
    → reentry_confirm → ohlc_regime_gate → ohlc_entry_zones
    → swing brackets (RR) → [gold hard_block optional]
    hard_risk / day_risk_gates = OFF (informational only)
            │
  journal PENDING → broker.open_position → confirm → robot_open
            │
  manage: protect brackets; trail enroll NO-OP
            │
  close (SL/TP/broker) → Capital £ confirm
            │
  _learn_closed_trade →
    profit_learning + bracket_learn + learn_gate(ΔW keep/rollback)
    + AdaptiveBrain.learn_from_trade + structure_shadow teach
    + ohlc_scenarios / shadow_mistake_audit + adaptive_system.learn_close
    + day_book / unified_cycle / situation_memory / strategy_gen
    + shadow_paper + hold_shadow + autonomy.shadow_self_dev_tick
            │
  brain.save() → brain_<epic>.npz ; Store state keys ; Admin Desk /ws
```

**Kaimiņu karte (viena rinda):**

```
launcher/Docker → main.lifespan → RuntimeHub → CapitalBroker + QuoteStream
  → Engine.poll → market_context + fusion → NeuralCore → adaptive nudges
  → AutoEvolution → LIVE gates → Capital OPEN/CLOSE £
  → profit / bracket / learn_gate / shadows → Desk
```

---

## 4. Boot un tenancy

| Solis | Kas | Detalizācija |
|-------|-----|--------------|
| A | `START.bat` / `PALAIST_PILNO_SISTEMU.bat` | Atbrīvo :8765, local cookies, atver `/admin` |
| B | `run_windows.bat` | `.venv` + pip + `requirements.txt` + CPU torch |
| C | `launcher.py` | Bind host/port, health-wait, uvicorn `app.main:app` |
| D | `lifespan` | Bootstrap admin; `hub.start_all_active()` |
| E | `AccountRuntime` | `data/accounts/client_N/` — `robot.sqlite3` + `brain*.npz` |
| F | Mandāts | Pirmais manuālais Capital deals → epic+lot; AI OPEN pēc close |
| G | Docker / Render | Tas pats `app.main:app`; `/data` volume; `/api/health` |

### Auth

- PBKDF2 paroles; Fernet (`secret.key` / `NTA_SECRET_KEY`) broker secrets
- Cookies: `nta_admin`, `nta_client`, (legacy `nta_session`)
- Loopback START.bat var auto-mint Admin cookie
- Production: vāja `NTA_ADMIN_PASSWORD` → refuse start

### v6 mandāta likums

1. Manuāli atver Capital deals  
2. Robots adoptē epic + size  
3. Kamēr atvērts — tikai protect SL/TP  
4. Pēc close — AI lemj uz **jaunas completed M1** sveces  

Skat. arī `V6_AUTONOMA_AI_LV.txt`.

---

## 5. Tick → learn (pilns cikls)

| # | Solis | Kas |
|---|-------|-----|
| 1 | Ingest | WS bid/ask (REST fallback); forming M1 tikai chartam |
| 2 | Sync | Reconcile orders; vanish → £ queue; protect naked brackets |
| 3 | Features | `market_context` OHLC + fusion (peers+news+liq) + MTF |
| 4 | Neural | `AdaptiveBrain.tick` → NeuralCore softmax (`dt=1` M1 bar) |
| 5 | Nudge | structure / GOLD / strategy / flow / forecast / situation / MTF |
| 6 | Policy | `evaluate_neural_entry` (+ `policy.py` ja `policy_live_execute`) |
| 7 | Gates | readiness → news → day_book → edge → reentry → regime → zone → RR |
| 8 | OPEN | mandate size + OHLC SL/TP `require_swing` → Capital POST |
| 9 | Manage | protect SL+TP; trail OFF; TP1 scale-out BUY; neural reverse |
| 10 | CLOSE £ | Funds TRADE £ → queue/retry; nekad UPL kā teach |
| 11 | Learn | labels → learn_gate ΔW → settle → shadows → nākamais bars |
| ∥ | SHADOW | Tas pats NeuralCore + paper journal (atsevišķa £) |

### Hard OPEN vārti (secība)

| Vārti | Bloķē kad |
|-------|-----------|
| Fusion / readiness | EXTERNAL trūkst / prep false |
| News calendar | high-impact logs |
| Day book | max pos / foreign epic / weak ADD |
| Unified edge | margin < floor 0.03–0.12 |
| Re-entry | weak / chase / same range |
| OHLC regime (#180) | chop · low vol · doji · impulse · chaos |
| Entry zone + MTF | nav zonā / fleet side |
| Swing RR | nevar ielikt derīgu SL/TP |
| Hard risk | **NEKAD** — tikai Admin karogs |
| Structure shadow | **NEKAD** LIVE veto — tikai teach |

### Close → £

1. `_resolve_broker_pnl` — `transactions` TRADE  
2. Fallback: `activity`  
3. Fuzzy instrument/size bind (pence-cousin)  
4. £0 placeholder → ignore  
5. Queue `pending_broker_pnl_deals`  
6. Desk: `_mirror_capital_funds_closes`  

UPL / confirm / mid×size — **NEKAD** kā realized teach.

### Learn ķēde (`_learn_closed_trade`)

1. `profit_learning.record_closed` → label  
2. `learn_gate` KEEP / ROLLBACK ΔW  
3. Bank: hard_risk + day_book + `unified_cycle.settle`  
4. `bracket_learn` / `reentry_confirm` / zones `observe_close`  
5. `adaptive_system.learn_close`  
6. `structure_shadow` + `hold_shadow` + `shadow_mistake_audit`  
7. Claim once — `already_learned` bloķē dubultu  

---

## 6. LIVE vs SHADOW

| Aspekts | LIVE | SHADOW |
|---------|------|--------|
| Svari | `AdaptiveBrain` / `NeuralCore` | **Tas pats objekts** (`shadow_brain is core`) |
| OPEN | Pilns gate stack + Capital POST | Paper; var bez zone/fleet side |
| Peļņa | Capital Funds £ tikai | Paper £ (`cash_per_price_unit`) |
| Teach | `_learn_closed_trade` + `learn_gate` | `_shadow_learn` **bez** learn_gate |
| Trail | OFF | OFF |
| Promote | — | `READY_N=0` / `PROMOTE_N=1` (tūlīt ready_for_live) |
| Mirror | — | Katrs LIVE OPEN → `_shadow_mirror_live_open` |

**Līgums:** paper close ΔW ietekmē nākamo LIVE lēmumu tajā pašā poll ciklā.
Paper PnL **nav** Capital Funds £.

---

## 7. Moduļu inventārs

### Kodols

| Modulis | Fails | Loma |
|---------|-------|------|
| NeuralCore | `core.py` | Recurrent BUY/SELL/HOLD; birth/prune; bar-time dt |
| AdaptiveBrain | `engine.py` | Per-epic `brain*.npz`; M1 labels + trade-PnL learn |
| AutoEvolution | `autonomy.py` | Ģenerē/validē/promote `policy.py`; LIVE evaluate |
| adaptive_system | `adaptive_system.py` | Boot evo + soft-nudge OPEN + learn_close orķestris |
| evo_boot | `evo_boot.py` | Seed physical evo svaigam Store |
| self_params | `self_params.py` | Persistējamie knobs; trail forced 0 |
| machine_spec | `machine_spec.py` | LIVE līguma dokumentācija (ne executor) |

### OHLC / struktūra

| Modulis | Fails | Loma |
|---------|-------|------|
| market_context | `market_context.py` | Closed M1/M5/M15/H1 feature vector |
| ohlc_regime_gate | `ohlc_regime_gate.py` | Hard veto: chop / low vol / doji / impulse / chaos |
| ohlc_entry_zones | `ohlc_entry_zones.py` | Demand/Supply zonas + triggeri |
| ohlc_regime_brackets | `ohlc_regime_brackets.py` | Strukturāls SL/TP; BUY TP1/TP2 |
| ohlc_scenarios | `ohlc_scenarios.py` | Scenario label + self-audit |
| bracket_learn | `bracket_learn.py` | OHLC distances; κ/sl no audited closes |
| reentry_confirm | `reentry_confirm.py` | Raw edge + jauna tape pierādījums |
| structure_edge | `structure_edge.py` | Layout×side EV soft nudge |
| structure_shadow | `structure_shadow.py` | Pēc SL māca LIVE; advise ≠ gate |
| structure_memory | `structure_memory.py` | BOS/CHoCH/same-range bias |
| gold_strategies | `gold_strategies.py` | BOS soft teach; hard_block optional |
| math_rules | `math_rules.py` | Tick snap, κ×SL, dealing size grid |
| mtf_momentum | `mtf_momentum.py` | KAMA/ADX/ATR soft evidence |

### Ārējie dati

| Modulis | Fails | Loma |
|---------|-------|------|
| calendar_feed | `calendar_feed.py` | Forex Factory → news veto |
| external_risk | `external_risk.py` | High-impact news timing veto |
| intermarket | `intermarket.py` | Peer markets + contradiction |
| liquidity_feed | `liquidity_feed.py` | Yahoo/CEX volume proxies |
| market_fusion | `market_fusion.py` | OHLC+peers+news+liq → EXTERNAL dims |
| flow_consensus | `flow_consensus.py` | M1 OF+liq consensus nudge/block |
| instrument_profile | `instrument_profile.py` | Epic → asset class / currencies |
| trade_costs | `trade_costs.py` | Shared RT spread/slip accounting |

### Mācīšanās

| Modulis | Fails | Loma |
|---------|-------|------|
| profit_learning | `profit_learning.py` | Broker £ → labels + counterfactuals |
| learn_gate | `learn_gate.py` | Keep ΔW tikai ja holdout uzlabojas |
| day_book | `day_book.py` | Dienas £ / multi-pos / soft size |
| unified_cycle | `unified_cycle.py` | Edge floor + aggression + compose_open |
| learning_generation | `learning_generation.py` | UI “Paaudze” kad svari mainās |
| history_relearn | `history_relearn.py` | Offline rebuild no dump/backup |
| instrument_isolation | `instrument_isolation.py` | Park bags pie epic switch |
| feature_discovery / registry / validation | `feature_*.py` | OHLC algebra search → approve → LIVE |
| model_readiness / validation | `model_*.py` | Prep/eval; WF promote/rollback |
| forecast_* | `forecast_*.py` | Multi-horizon ledger; soft bridge LIVE |

### Shadow / diagnostika

| Modulis | Fails | Loma |
|---------|-------|------|
| shadow_paper | `shadow_paper.py` | Paper book; trail OFF; neatkarīga £ |
| shadow_mistake_audit | `shadow_mistake_audit.py` | Diagnose → fix → ready_for_live |
| hold_shadow | `hold_shadow.py` | HOLD proposal → counterfactual teach |
| forward_live | `forward_live.py` | Closed-deal diagnostika (ne executor) |

### Stratēģija / kartītes

| Modulis | Fails | Loma |
|---------|-------|------|
| strategy_gen | `strategy_gen.py` | Structure×side cards → soft nudge |
| sample_strategies | `sample_strategies.py` | Ingest closed trades → cards |
| champion_library | `champion_library.py` | Context-keyed weight snapshots |
| situation_memory | `situation_memory.py` | Scene×action £ EV soft nudge |

### Broker / runtime / platforma

| Modulis | Fails | Loma |
|---------|-------|------|
| CapitalBroker | `broker.py` | REST session, positions, open/update/close |
| CapitalQuoteStream | `stream.py` | Authed WS quotes |
| RuntimeHub | `runtime_hub.py` | Multi-account workers + Desk truth payloads |
| PlatformStore | `platform.py` | Admini, klienti, encrypted broker accounts |
| security | `security.py` | PBKDF2 + Fernet |
| storage | `storage.py` | Data root (`data/` vai `%LOCALAPPDATA%`) |
| shared_market | `shared_market.py` | Same-epic MTF/side starp klientiem |
| brokers registry | `brokers/__init__.py` | `capital_com` live; `generic_rest` reserved |
| hard_risk | `hard_risk.py` | Limits Adminam; OPEN **nebloķē** |
| protection_ai | `protection_ai.py` | Trail helpers — LIVE no-op |

---

## 8. Kas ir ON / OFF

| Komponents | Stāvoklis | Piezīme |
|------------|-----------|---------|
| OHLC regime / zone / swing RR gates | **ON** | Galvenais OPEN drošības steks |
| News / intermarket / fusion gap | **ON** | Jauni OPEN bez svaigiem datiem bloķēti |
| Order journal + UNCERTAIN block | **ON** | Nav blind re-send |
| Protect SL+TP (OHLC swing) | **ON** | Naked pozīcija → protect |
| Capital Funds £ kā peļņa | **ON** | UPL/confirm invent OFF |
| One NeuralCore LIVE+SHADOW | **ON** | Paper māca LIVE svarus |
| Broker trail / adaptive trail | **OFF** | `TRAIL_ENABLED=False` |
| Hard-risk OPEN gate | **OFF** | `entry_allowed` vienmēr True |
| Day risk gates | **OFF** | Forced 0; Admin karogs |
| Gold hard_block | **OFF** | Default soft teach |
| Structure shadow LIVE veto | **OFF** | Tikai teach / UI |
| Eternal M1 colour confirm | **OFF** | Noņemts PR #180 |

**Operatora uzmanībai:** hard-risk un day-loss limiti **NERAUJ** OPEN — tikai Admin
karogi. Drošība ir OHLC vārti + news + journal + Funds £ truth.

---

## 9. UI un persistēšana

### Virsmas

| UI | URL | Faili | Loma |
|----|-----|-------|------|
| Portal | `/` | `portal.html` | Izvēle: admin / desk / client |
| Admin | `/admin` | `web/admin/*` | Multi-client, risk, OHLC chart |
| PerpSignal Desk | `/desk` | `web/desk/`, `perpsignal-desk/` | Trading terminal; live API |
| Client | `/app` | `web/client/*` | Status / pause / emergency |
| Legacy neon | `/console` | `index.html`, `ui.js` | Vecais operator dashboard |

### Storage

| Kur | Kas |
|-----|-----|
| `platform.sqlite3` + `secret.key` | Admini, klienti, šifrēti broker konti, sesijas 12h |
| `data/accounts/client_N/robot.sqlite3` | events · state · signals · trades · orders · candles |
| `brain.npz` / `brain_<hash>.npz` | Per-epic NeuralCore checkpoint (atomisks save) |
| `policy.py` | Evolved `decide()`; LIVE ja `policy_live_execute=1` |
| Store state KV | `self_params`, shadows, day_book, unified_cycle, … |

**Nepublicē** `data/`, API atslēgas, `.env`, `robot.sqlite3`, `brain*.npz`.

### Deploy

| Ceļš | Faili |
|------|-------|
| Windows | `START.bat`, `run_windows.bat`, optional EXE (`build_windows_exe.bat`) |
| Docker | `Dockerfile`, `docker-compose.yml`, `/data` volume |
| Render | `render.yaml`, `Procfile`, `/api/health` |

---

## 10. Testi

- Katalogs: `tests/` (~168 faili)  
- Palaišana: `python -m pytest -q` no repo root  
- Offline mocki — **bez** īstas Capital naudas  
- Audit komplekti: `test_audit_*.py` (full cycle, PnL truth, gates, one-brain, …)  
- Stress / offline skripti: `scripts/`  

---

## 11. Zināmie arhitektūras riski (kopsavilkums)

Detalizēts audits (PR #180 → #267): skat. agenta canvas / iepriekšējo audit darbu.
Galvenie punkti, kas **ietekmē** šo karti:

1. **CRITICAL** — viena smadzenes: paper `_shadow_learn` uz LIVE `NeuralCore`; `READY_N=0`  
2. **HIGH** — `swing_anchor` var atgriezt `ready=True` arī kad min-RR fail (LIVE glābj post-snap)  
3. **HIGH** — `ohlc_zone` κ×SL invent ceļš paliek, ja nav `require_swing=True`  
4. **HIGH** — fuzzy Funds bind + NESEN estimate fallback var rādīt ne-Funds £  
5. **MEDIUM** — `unified_cycle.compose_open` `tighten` netiek padots LIVE `submit` (`tighten=1.0`)  
6. **MEDIUM** — hard-risk halt nebloķē OPEN  

---

## 12. Saistītie dokumenti

| Dokuments | Saturs |
|-----------|--------|
| `V6_AUTONOMA_AI_LV.txt` | Mandāts, M1 lēmumi, kas nav LIVE veto |
| `README.PROJECT.md` | Angļu arhitektūra + PDF formulas |
| `README.md` | Īss starts |
| `PASMACISANAS_MASINA_LV.md` | Self-dev / mācīšanās |
| `ADAPTIVA_SL_TP_LV.txt` | Adaptīvie SL/TP noteikumi |
| `ENTRY_MARGIN_LV.txt` | Entry edge / margin |
| `FOREX_FACTORY_INTERMARKET_LV.txt` | Ziņas + intermarket |
| `DEPLOY_LV.md` / `UZSTADISANA_LV.txt` | Deploy / instalācija |
| `KĀ_PALAIST_LV.txt` | Palaišana |

---

*Dokuments ģenerēts kā arhitektūras karte (full map). Atjaunot, kad mainās gate
stack, one-brain līgums, peļņas avots vai trail politika.*
