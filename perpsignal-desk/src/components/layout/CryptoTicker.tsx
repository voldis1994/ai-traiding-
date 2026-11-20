import { TICKER } from "../../data/mockMarketData";

export function CryptoTicker({ filter }: { filter: string }) {
  const q = filter.trim().toLowerCase();
  const rows = q ? TICKER.filter((t) => t.symbol.toLowerCase().includes(q)) : TICKER;
  return (
    <div className="crypto-ticker">
      {rows.map((t) => (
        <div key={t.symbol} className="ct-item">
          <span className="ct-sym">{t.symbol}</span>
          <span className="num">{t.price}</span>
          <span className={t.down ? "dn" : "up"}>{t.down ? "▼" : "▲"}</span>
          <span className={`num ${t.down ? "dn" : "up"}`}>
            {Math.abs(t.change).toFixed(2)}%
          </span>
        </div>
      ))}
    </div>
  );
}