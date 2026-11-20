import { LIVE_TRADES } from "../../data/mockMarketData";
import { LiveBadge } from "../ui/LiveBadge";

export function LiveTrades() {
  return (
    <section className="panel live-trades">
      <header className="panel-header">
        <span>LIVE TRADES BTC</span>
        <LiveBadge />
      </header>
      <div className="lt-flow">
        FLOW 1s $0 / $0 +$0 · 5s $45.2K / $9.62K <span className="up">+$35.6K</span>
      </div>
      <div className="lt-cols">
        <span>TIME-UTC</span>
        <span>SIDE</span>
        <span>PRICE</span>
        <span>SIZE-BTC</span>
        <span>VALUE</span>
      </div>
      <div className="panel-body">
        {LIVE_TRADES.map((t, i) => (
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