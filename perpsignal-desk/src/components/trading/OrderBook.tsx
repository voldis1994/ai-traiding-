import { useState } from "react";
import { BOOK_RATIO, ORDER_ASKS, ORDER_BIDS, SPREAD } from "../../data/mockOrderBook";
import { LiveBadge } from "../ui/LiveBadge";

function fmtPrice(n: number) {
  return n.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

export function OrderBook() {
  const [tab, setTab] = useState<"BOOK" | "CMP">("BOOK");
  return (
    <section className="panel order-book">
      <header className="panel-header">
        <span>ORDER BOOK BTC</span>
        <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
          <button
            type="button"
            className={`mini-tab ${tab === "BOOK" ? "on" : ""}`}
            onClick={() => setTab("BOOK")}
          >
            BOOK
          </button>
          <button
            type="button"
            className={`mini-tab ${tab === "CMP" ? "on" : ""}`}
            onClick={() => setTab("CMP")}
          >
            CMP
          </button>
          <LiveBadge />
        </span>
      </header>
      <div className="ob-sub">PERPSIGNAL ORDER BOOK</div>
      <div className="ob-cols">
        <span>PRICE</span>
        <span>SIZE BTC</span>
        <span>VALUE</span>
        <span>TOTAL</span>
      </div>
      <div className="panel-body ob-body">
        {tab === "BOOK" ? (
          <>
            {ORDER_ASKS.map((r) => (
              <div key={`a-${r.price}`} className="ob-row ask">
                <i style={{ width: `${r.depth}%` }} />
                <span className="num">{fmtPrice(r.price)}</span>
                <span className="num">{r.size.toFixed(3)}</span>
                <span className="num">{r.value}</span>
                <span className="num">{r.total.toFixed(3)}</span>
              </div>
            ))}
            <div className="ob-spread">
              SPREAD {SPREAD.abs.toFixed(1)} · {SPREAD.bps.toFixed(1)} BPS
            </div>
            {ORDER_BIDS.map((r) => (
              <div key={`b-${r.price}`} className="ob-row bid">
                <i style={{ width: `${r.depth}%` }} />
                <span className="num">{fmtPrice(r.price)}</span>
                <span className="num">{r.size.toFixed(3)}</span>
                <span className="num">{r.value}</span>
                <span className="num">{r.total.toFixed(3)}</span>
              </div>
            ))}
          </>
        ) : (
          <div className="ob-cmp">CMP view — demo depth composite (mock)</div>
        )}
      </div>
      <div className="ob-ratio">
        <div className="ob-ratio-bar">
          <i className="b" style={{ width: `${BOOK_RATIO.bid}%` }} />
          <i className="a" style={{ width: `${BOOK_RATIO.ask}%` }} />
        </div>
        <div className="ob-ratio-lbl">
          <span className="up">{BOOK_RATIO.bid}% BID</span>
          <span className="dn">{BOOK_RATIO.ask}% ASK</span>
        </div>
      </div>
    </section>
  );
}
