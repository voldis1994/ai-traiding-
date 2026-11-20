import { TICKER } from "../../data/mockMarketData";

type Item = { symbol: string; price: string; change: number; down: boolean };

export function CryptoTicker({ filter, items }: { filter: string; items?: Item[] }) {
  const source = items && items.length ? items : [...TICKER];
  const q = filter.trim().toLowerCase();
  const rows = q ? source.filter((t) => t.symbol.toLowerCase().includes(q)) : source;
  return (
    <div className="crypto-ticker">
      {rows.map((t) => (
        <div key={t.symbol} className="ct-item">
          <span className="ct-sym">{t.symbol}</span>
          <span className="num">{t.price}</span>
          <span className={t.down ? "dn" : "up"}>{t.down ? "▼" : "▲"}</span>
          <span className={`num ${t.down ? "dn" : "up"}`}>{Math.abs(t.change).toFixed(2)}%</span>
        </div>
      ))}
    </div>
  );
}
