import { FUNDING, SIGNAL_WATCH } from "../../data/mockMarketData";
import { LiveBadge } from "../ui/LiveBadge";

type Props = {
  funding?: Array<{ symbol: string; rate: string }>;
  crowd?: { longPct: number; shortPct: number };
  fear?: { value: number; label: string };
  signals?: Array<{ symbol: string; agree: number; side: "SHORT" | "LONG" | "HOLD" }>;
};

export function SignalWatchSidebar({ funding, crowd, fear, signals }: Props) {
  const fund = funding && funding.length ? funding : [...FUNDING];
  const cr = crowd || { longPct: 76.6, shortPct: 23.4 };
  const fe = fear || { value: 10, label: "Extreme Fear" };
  const sigs = signals && signals.length ? signals : [...SIGNAL_WATCH];
  return (
    <aside className="signal-watch panel">
      <header className="panel-header">
        <span>SIGNAL WATCH</span>
        <LiveBadge />
      </header>
      <div className="panel-body sw-body">
        <section className="sw-sec">
          <h3>ASSET FUNDING</h3>
          {fund.map((f) => (
            <div key={f.symbol} className="sw-row">
              <span>{f.symbol}</span>
              <span className="up num">{f.rate}</span>
            </div>
          ))}
        </section>

        <section className="sw-sec">
          <h3>CROWD POSITIONING</h3>
          <div className="sw-crowd-lbl">
            <span className="up">{cr.longPct.toFixed(1)}% Long</span>
            <LiveBadge />
            <span className="dn">{cr.shortPct.toFixed(1)}% Short</span>
          </div>
          <div className="sw-bar">
            <i className="b" style={{ width: `${cr.longPct}%` }} />
            <i className="s" style={{ width: `${cr.shortPct}%` }} />
          </div>
          <div className="sw-meta">
            {cr.longPct >= 50 ? "Long-leaning" : "Short-leaning"} · neural score blend
          </div>
        </section>

        <section className="sw-sec">
          <h3>CRYPTO MARKET LEAN</h3>
          <div className="up" style={{ fontWeight: 700, marginBottom: 3 }}>
            {Math.round(cr.longPct)}% BULLISH LEAN
          </div>
          <div className="sw-lean">
            <i style={{ width: `${cr.longPct}%` }} />
          </div>
        </section>

        <section className="sw-sec sw-fear">
          <div className="sw-fear-h">
            <h3>GLOBAL MARKET SENTIMENT INDEX</h3>
            <LiveBadge />
          </div>
          <div className="sw-fear-val">
            <b>{fe.value}</b>
            <div>
              <div>{fe.label}</div>
              <div className="sw-meta">— Live neural</div>
            </div>
          </div>
          <div className="sw-fear-meter">
            <i style={{ left: `${fe.value}%` }} />
          </div>
        </section>

        <section className="sw-sec">
          <h3>SIGNAL WATCH</h3>
          {sigs.map((s) => (
            <div key={s.symbol} className="sw-sig">
              <strong>{s.symbol}</strong>
              <span className="sw-meta">{s.agree}% agree</span>
              <span className="sw-short">
                {s.side === "LONG" ? "▲" : "▼"} {s.side}
              </span>
            </div>
          ))}
        </section>
      </div>
    </aside>
  );
}
