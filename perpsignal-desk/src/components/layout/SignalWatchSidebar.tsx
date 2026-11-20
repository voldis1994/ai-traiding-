import { FUNDING, SIGNAL_WATCH } from "../../data/mockMarketData";
import { LiveBadge } from "../ui/LiveBadge";

export function SignalWatchSidebar() {
  return (
    <aside className="signal-watch panel">
      <header className="panel-header">
        <span>SIGNAL WATCH</span>
        <LiveBadge />
      </header>
      <div className="panel-body sw-body">
        <section className="sw-sec">
          <h3>ASSET FUNDING</h3>
          {FUNDING.map((f) => (
            <div key={f.symbol} className="sw-row">
              <span>{f.symbol}</span>
              <span className="up num">{f.rate}</span>
            </div>
          ))}
        </section>

        <section className="sw-sec">
          <h3>CROWD POSITIONING</h3>
          <div className="sw-crowd-lbl">
            <span className="up">76.6% Long</span>
            <LiveBadge />
            <span className="dn">23.4% Short</span>
          </div>
          <div className="sw-bar">
            <i className="b" style={{ width: "76.6%" }} />
            <i className="s" style={{ width: "23.4%" }} />
          </div>
          <div className="sw-meta">Long-leaning · as of 20:00 UTC</div>
        </section>

        <section className="sw-sec">
          <h3>CRYPTO MARKET LEAN</h3>
          <div className="up" style={{ fontWeight: 700, marginBottom: 3 }}>
            24% BULLISH LEAN
          </div>
          <div className="sw-lean">
            <i style={{ width: "24%" }} />
          </div>
        </section>

        <section className="sw-sec sw-fear">
          <div className="sw-fear-h">
            <h3>GLOBAL MARKET SENTIMENT INDEX</h3>
            <LiveBadge />
          </div>
          <div className="sw-fear-val">
            <b>10</b>
            <div>
              <div>Extreme Fear</div>
              <div className="sw-meta">— Stable</div>
            </div>
          </div>
          <div className="sw-fear-meter">
            <i style={{ left: "10%" }} />
          </div>
          <div className="sw-meta">as of Nov 18 · prev 11 · next n/a</div>
        </section>

        <section className="sw-sec">
          <h3>SIGNAL WATCH</h3>
          {SIGNAL_WATCH.map((s) => (
            <div key={s.symbol} className="sw-sig">
              <strong>{s.symbol}</strong>
              <span className="sw-meta">{s.agree}% agree</span>
              <span className="sw-short">▼ {s.side}</span>
            </div>
          ))}
        </section>
      </div>
    </aside>
  );
}