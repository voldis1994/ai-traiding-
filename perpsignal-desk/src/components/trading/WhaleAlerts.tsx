import { useMemo, useState } from "react";
import { WHALES } from "../../data/mockMarketData";
import { LiveBadge } from "../ui/LiveBadge";

type Filter = "BOTH" | "BUY" | "SELL";
type Whale = { side: "BUY" | "SELL"; price: string; value: string; ago: string };

export function WhaleAlerts({ whales }: { whales?: Whale[] }) {
  const [filter, setFilter] = useState<Filter>("BOTH");
  const source = whales && whales.length ? whales : [...WHALES];
  const rows = useMemo(
    () => source.filter((w) => (filter === "BOTH" ? true : w.side === filter)),
    [filter, source],
  );

  return (
    <section className="panel whales">
      <header className="panel-header">
        <span>WHALE ALERTS</span>
        <LiveBadge />
      </header>
      <div className="wh-filters">
        {(["BOTH", "BUY", "SELL"] as Filter[]).map((f) => (
          <button
            key={f}
            type="button"
            className={`mini-tab ${filter === f ? "on" : ""}`}
            onClick={() => setFilter(f)}
          >
            {f}
          </button>
        ))}
        <button type="button" className="mini-tab">
          MKT
        </button>
        <button type="button" className="mini-tab">
          ALL
        </button>
        <span className="wh-min">MIN ANY</span>
      </div>
      <div className="panel-body">
        {rows.length === 0 ? (
          <div className="ob-cmp">No closed prints yet</div>
        ) : (
          rows.map((w, i) => (
            <div key={i} className="wh-row">
              <span className="wh-badge">WHALE</span>
              <span className="wh-large">Large</span>
              <span className={w.side === "BUY" ? "up" : "dn"}>
                {w.side === "BUY" ? "▲" : "▼"} {w.price}
              </span>
              <strong className={w.side === "BUY" ? "up" : "dn"}>{w.value}</strong>
              <span className="wh-ago">{w.ago}</span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
