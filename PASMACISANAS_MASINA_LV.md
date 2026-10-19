# Pašmācīšanās mašīna — viena sistēma, skaidri algoritmi

Šis projekts **jau ir** reāla pašmācīšanās treidinga mašīna.
Šis dokuments to saliek **kā vienu mašīnu** (nevis “v3 vs v5 pret v6”) un
paskaidro **vienādojumus ↔ kodu**.

Avots formulām: `reference/Parbauditas_Sakartotas_Formulas.pdf`  
Implementācija: `app/core.py`, `app/math_rules.py`, `app/engine.py`, `app/autonomy.py`

---

## 1. Kas ir “mašīna”?

Viena cauruļvada funkcija (no PDF):

\[
F_t = F_{\text{learn}} \circ F_{\text{broker}} \circ F_{\text{order}} \circ F_{\text{decision}} \circ F_{\text{neuron}}
\]

| Bloks | Kodā | Ko dara |
|--------|------|---------|
| \(F_{\text{neuron}}\) | `NeuralCore.step` | stāvoklis → Softmax scores |
| \(F_{\text{decision}}\) | `evaluate_neural_entry` | argmax → BUY/SELL/HOLD |
| \(F_{\text{order}}\) | `submit` + journal | SL/TP, 1 pozīcija |
| \(F_{\text{broker}}\) | `CapitalBroker` | LIVE izpilde / confirm |
| \(F_{\text{learn}}\) | `NeuralCore.learn` | kļūda → svaru update |

**LIVE lēmums:** tikai neironu argmax (v6).  
**Evolūcija** (`active_policy.py`, `MIN_EDGE`): shadow/research — **nebloķē** LIVE.

---

## 2. Tirgus ieeja (pirms neirona)

Mid un spread:

\[
m_t=\frac{\text{Bid}_t+\text{Ask}_t}{2},\qquad
s_t=\text{Ask}_t-\text{Bid}_t\ge 0
\]

**Praksē** (v3/v4) neirons nesaņem 3 jēlos skaitļus, bet **10–18 bezdimensiju pazīmes**:
- 1/5/15/60 min returni ×1000 (tipiski ~0.1…10, clip ±20)
- EMA attiecības, volatilitāte, range, sveces ķermenis, spread
- + peer tirgi + ziņu proximity (v4, 18 dim kopā)

Kodā: `market_context.market_features`, `market_fusion.fusion_features`.

Normalizācija neironā:

\[
z_t=\mathrm{clip}\!\left(\frac{x_t-\mu_t}{\max(\sigma_t,\varepsilon)},\,-7,\,7\right)
\]

(`NeuralCore.step`)

---

## 3. Neironu dinamika (kodols)

Stāvoklis: slēptais \(h\) un atmiņa \(c\), laika konstantes \(\tau,\tau^c\).

\[
\lambda_{i,t}=1-e^{-\Delta t/\tau_i}
\]

\[
h_{i,t+1}=h_{i,t}+\lambda_{i,t}\Big[\tanh(p_i^\top z_t + \textstyle\sum_j w_{ij}h_{j,t})-h_{i,t}\Big]
\]

\[
\gamma_{i,t}=1-e^{-\Delta t/\tau^c_i}
\]

\[
c_{i,t+1}=c_{i,t}+\gamma_{i,t}\Big[\tanh(0.5\,h_{i,t+1}+0.2\,c_{i,t})-c_{i,t}\Big]
\]

Logits un Softmax (3 klases: BUY=1, SELL=2, HOLD=3):

\[
\ell_t = R_t h_{t+1} + O_t c_{t+1} + b_t
\]

\[
p_{t,j}=\frac{e^{\ell_{t,j}-M_t}}{\sum_{k=1}^{3}e^{\ell_{t,k}-M_t}},\quad
M_t=\max_k\ell_{t,k}
\]

\[
a_t=\arg\max_j p_{t,j}
\]

Kodā: `app/core.py` → `_forward`, `step`.  
**Svarīgi:** \(p_{t,j}\) **nav** kalibrēta peļņas varbūtība — tikai modeļa score.

Lēmuma solis LIVE: **1× uz katru jaunu pabeigtu M1 sveci** (`dt=60`), nevis ik 3 s poll.

---

## 4. Pašattīstība (birth / prune)

Rekonstrukcija:

\[
\hat z_t=G_t h_{t+1},\qquad
\nu_t=\|z_t-\hat z_t\|_2,\qquad
\bar\nu_t=(1-\alpha)\bar\nu_{t-1}+\alpha\nu_t
\]

Ja kļūda liela → **jauns neirons** (birth):

\[
p_{\text{new}}=\frac{z_t}{\|z_t\|_2+\varepsilon},\quad h_{\text{new}}=c_{\text{new}}=0
\]

Utility (cik neirons palīdz loss):

\[
q_{i,t}=\mathcal L_t^{(-i)}-\mathcal L_t,\qquad
U_{i,t+1}=(1-\beta)U_{i,t}+\beta q_{i,t}
\]

Zema utility + pietiekams vecums → **prune**.

Kodā: `grow_if_needed`, `update_utility`, `prune`.

---

## 5. Mācīšanās (gradienti)

Zudums (virziena klase + rekonstrukcija):

\[
\mathcal L_t=-\sum_{j=1}^{3} y_{t,j}\log(p_{t,j}+\varepsilon_p)
\;+\;\omega\,\|G h - z\|^2
\]

Parametru update:

\[
\Theta_{t+1}=\Theta_t-\eta\,\nabla_{\Theta}\mathcal L_t
\]

kur \(\Theta=\{P,W,R,O,b,\tau,\tau^c,G\}\).

| Iestatījums | Vērtība | Kāpēc |
|-------------|---------|--------|
| \(\eta\) (`DEFAULT_LR`) | **0.1** | 1 solis maina svarus ~**0.01** mērogā (vecais 0.0005 gandrīz neko nemainīja) |
| Labels \(y_t\) | 8 pabeigtu M1 **virziens** pret spread | PDF nenosaka \(y_t\); tas ir proxy, **ne** P&L |
| Grad clip | `max_norm=2` | stabilitāte |

Kodā: `NeuralCore.learn` (lr=0.1), `AdaptiveBrain.tick` (delayed labels).

---

## 6. SL / TP un risks

\[
b^{\text{buf}}_t=\max(2\delta_t,\,2s_t)
\]

\[
d_t=\max(20\delta_t,\,4s_t,\,d^{\min}_t+b^{\text{buf}}_t)
\]

\[
n^{\text{SL}}_t=\left\lceil\frac{d_t}{\delta_t}\right\rceil,\quad
\rho_t=n^{\text{SL}}_t\,\delta_t
\]

BUY: \(SL=P_e-\rho\), \(TP=P_e+\kappa\rho\) (noapaļots ticks).  
SELL: spoguļoti. HOLD: nav SL/TP.

**Lots:** LIVE izmērs = **pirmā manuālā** pozīcija (ne riska formulā pārrēķināts).

Kodā: `math_rules.stop_take`, `engine.protect_position`.

---

## 7. Kad robots DRĪKST atvērt orderi (izpildes vārti)

Šie **nav** “vecās versijas cīņa”. Tie ir drošības filtri **pēc** neirona:

1. Manuālā pirmā pozīcija **aizvērta** (mandate + `last_closed_step`)
2. Jauna **pabeigta M1** svece
3. Softmax uzvarētājs = BUY vai SELL (ne HOLD)
4. Ziņu kalendārs OK (Forex Factory)
5. Tirgus `TRADEABLE`, `armed`, nav unresolved ordera
6. Pēc v4: fusion dati (peers+news) jābūt gataviem, citādi signāla nav

---

## 8. Kas NAV peļņas garantija

- Softmax % ≠ “varbūtība nopelnīt”
- 8 min virziens ≠ brokeru neto P&L
- Backtest / offline testi ≠ Capital.com LIVE pierādījums
- Pašmācīšanās nozīmē **pielāgošanos**, ne automātisku peļņu

---

## 9. Kā palaist mašīnu

Windows: `run_windows.bat` → `http://127.0.0.1:8765` → Capital.com API →
manuāla 1. pozīcija → aizver → AI mācās un lemj uz nākamajām M1 svecēm.

Testi: `python -m pytest -q` (137).

---

## 10. Viena patiesība par “versijām”

| Nosaukums dokumentācijā | Loma mašīnā |
|-------------------------|-------------|
| v3 | OHLC pazīmju shēma |
| v4 | + intermarket/ziņas ieejā |
| v5 | vēsturiski entry filtri — **LIVE vairs nelieto** |
| v6 | LIVE = neural argmax |

**Gala lēmējs ir viens:** \(a_t=\arg\max p_{t,j}\).
