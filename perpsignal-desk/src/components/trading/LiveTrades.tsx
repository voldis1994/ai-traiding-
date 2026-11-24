import { LIVE_TRADES } from "../../data/mockMarketData";
import { LiveBadge } from "../ui/LiveBadge";

type Trade = { t: string; side: "BUY" | "SELL"; price: string; size: string; value: string };

export function LiveTrades({ trades, epic }: { trades?: Trade[]; epic?: string }) {
  const rows = trades && trades.length ? trades : [...LIVE_TRADES];
  const label = (epic || "BTC").split(/[./]/)[0];
  return (
    <section className="panel live-trades">
      <header className="panel-header">
        <span>LIVE TRADES {label}</span>
        <LiveBadge />
      </header>
      <div className="lt-flow">
        OPEN POSITIONS · {rows.length} prints · Capital feed
      </div>
      <div className="lt-cols">
        <span>TIME-UTC</span>
        <span>SIDE</span>
        <span>PRICE</span>
        <span>SIZE</span>
        <span>VALUE</span>
      </div>
      <div className="panel-body">
        {rows.map((t, i) => (
          <div key={i} className={`lt-row ${t.side === "BUY" ? "buy" : "sell"}`}>
            <span className="num">{t.t}</span>
            <span className={t.side === "BUY" ? "up" : "dn"}>
              {t.side === "BUY" ? "▲ BUY" : "▼ SELL"}
            </span>
            <span className="num">{t.price}</span>
            <span className="num">{t.size}</span>
            <span className="num">{t.value}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
