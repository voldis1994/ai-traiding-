import { WATCHLIST } from "../../data/mockMarketData";
import { LiveBadge } from "../ui/LiveBadge";

function Spark({ values }: { values?: number[] }) {
  const pts =
    values && values.length > 1
      ? values
          .map((v, i) => {
            const lo = Math.min(...values);
            const hi = Math.max(...values);
            const span = Math.max(1e-9, hi - lo);
            const y = 14 - ((v - lo) / span) * 12;
            return `${(i / (values.length - 1)) * 64},${y}`;
          })
          .join(" ")
      : Array.from({ length: 16 }, (_, i) => `${i * 4},${10 - (i % 5)}`).join(" ");
  const down = !values || values.length < 2 || values[values.length - 1] <= values[0];
  return (
    <svg width="64" height="16" viewBox="0 0 64 16" className="spark">
      <polyline fill="none" stroke={down ? "#f06b78" : "#30d69a"} strokeWidth="1.2" points={pts} />
    </svg>
  );
}

type Row = { symbol: string; price: string; change: number; spark?: number[] };

export function Watchlist({
  selected,
  onSelect,
  rows,
}: {
  selected: string;
  onSelect: (s: string) => void;
  rows?: Row[];
}) {
  const list = rows && rows.length ? rows : WATCHLIST.map((w) => ({ ...w, spark: undefined }));
  return (
    <section className="panel watchlist">
      <header className="panel-header">
        <span>WATCHLIST</span>
        <LiveBadge />
      </header>
      <div className="panel-body">
        {list.map((w) => (
          <button
            key={w.symbol}
            type="button"
            className={`wl-row ${selected === w.symbol ? "on" : ""}`}
            onClick={() => onSelect(w.symbol)}
          >
            <strong>{w.symbol}</strong>
            <Spark values={w.spark} />
            <span className="num">{w.price}</span>
            <span className={`num ${w.change < 0 ? "dn" : "up"}`}>{w.change.toFixed(2)}%</span>
          </button>
        ))}
      </div>
    </section>
  );
}
