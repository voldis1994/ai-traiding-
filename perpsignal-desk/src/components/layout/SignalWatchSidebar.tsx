import { LiveBadge } from "../ui/LiveBadge";

type Props = {
  funding?: Array<{ symbol: string; rate: string }>;
  crowd?: { longPct: number | null; shortPct: number | null; unavailable?: boolean };
  fear?: { value: number | null; label: string; unavailable?: boolean };
  signals?: Array<{ symbol: string; agree: number; side: "SHORT" | "LONG" | "HOLD" }>;
};

export function SignalWatchSidebar({ funding, crowd, fear, signals }: Props) {
  const fund = funding && funding.length ? funding : [{ symbol: "—", rate: "N/A" }];
  const unavailable = Boolean(crowd?.unavailable || fear?.unavailable);
  const longPct = crowd?.longPct;
  const shortPct = crowd?.shortPct;
  const feVal = fear?.value;
  const feLabel = fear?.label || "N/A";
  const sigs = signals && signals.length ? signals : [];
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
              <span className="num">{f.rate}</span>
            </div>
          ))}
          <div className="sw-meta">Capital feed only — no synthetic rates</div>
        </section>

        <section className="sw-sec">
          <h3>CROWD POSITIONING</h3>
          {unavailable || longPct == null || shortPct == null ? (
            <div className="sw-meta">N/A — not provided by Capital</div>
          ) : (
            <>
              <div className="sw-crowd-lbl">
                <span className="up">{longPct.toFixed(1)}% Long</span>
                <LiveBadge />
                <span className="dn">{shortPct.toFixed(1)}% Short</span>
              </div>
              <div className="sw-bar">
                <i className="b" style={{ width: `${longPct}%` }} />
                <i className="s" style={{ width: `${shortPct}%` }} />
              </div>
            </>
          )}
        </section>

        <section className="sw-sec sw-fear">
          <div className="sw-fear-h">
            <h3>GLOBAL MARKET SENTIMENT INDEX</h3>
            <LiveBadge />
          </div>
          {unavailable || feVal == null ? (
            <div className="sw-meta">N/A — not provided by Capital</div>
          ) : (
            <>
              <div className="sw-fear-val">
                <b>{feVal}</b>
                <div>
                  <div>{feLabel}</div>
                </div>
              </div>
              <div className="sw-fear-meter">
                <i style={{ left: `${feVal}%` }} />
              </div>
            </>
          )}
        </section>

        <section className="sw-sec">
          <h3>SIGNAL WATCH</h3>
          {sigs.length === 0 ? (
            <div className="sw-meta">N/A</div>
          ) : (
            sigs.map((s) => (
              <div key={s.symbol} className="sw-sig">
                <strong>{s.symbol}</strong>
                <span className="sw-meta">{s.agree}% agree</span>
                <span className="sw-short">
                  {s.side === "LONG" ? "▲" : "▼"} {s.side}
                </span>
              </div>
            ))
          )}
        </section>
      </div>
    </aside>
  );
}
