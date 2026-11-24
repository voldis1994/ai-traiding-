import { INTEL } from "../../data/mockMarketData";
import { StatusBadge } from "../ui/StatusBadge";

type Item = {
  tag: "BULLISH" | "BEARISH" | "NEUTRAL";
  title: string;
  source: string;
  ago: string;
};

export function MarketIntel({ items }: { items?: Item[] }) {
  const rows = items && items.length ? items : [...INTEL];
  return (
    <aside className="market-intel panel">
      <header className="panel-header">
        <span>MARKET INTEL</span>
      </header>
      <div className="panel-body intel-feed">
        {rows.map((n, i) => (
          <article key={i} className="intel-card">
            <div className="intel-top">
              <StatusBadge kind={n.tag} />
              <span className="intel-ago">{n.ago}</span>
            </div>
            <div className="intel-title">{n.title}</div>
            <div className="intel-src">{n.source}</div>
          </article>
        ))}
      </div>
    </aside>
  );
}
