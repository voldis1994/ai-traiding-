import { WATCHLIST } from "../../data/mockMarketData";
import { LiveBadge } from "../ui/LiveBadge";

function Spark({ seed }: { seed: number }) {
  const pts = Array.from({ length: 16 }, (_, i) => {
    const y = 14 - ((i * 0.55 + Math.sin((i + seed) * 0.7) * 3 + seed) % 10);
    return `${i * 4},${y}`;
  }).join(" ");
  return (
    <svg width="64" height="16" viewBox="0 0 64 16" className="spark">
      <polyline fill="none" stroke="#f06b78" strokeWidth="1.2" points={pts} />
    </svg>
  );
}

export function Watchlist({
  selected,
  onSelect,
}: {
  selected: string;
  onSelect: (s: string) => void;
}) {
  return (
    <section className="panel watchlist">
      <header className="panel-header">
        <span>WATCHLIST</span>
        <LiveBadge />
      </header>
      <div className="panel-body">
        {WATCHLIST.map((w, i) => (
          <button
            key={w.symbol}
            type="button"
            className={`wl-row ${selected === w.symbol ? "on" : ""}`}
            onClick={() => onSelect(w.symbol)}
          >
            <strong>{w.symbol}</strong>
            <Spark seed={i + 2} />
            <span className="num">{w.price}</span>
            <span className={`num ${w.change < 0 ? "dn" : "up"}`}>
              {w.change.toFixed(2)}%
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}