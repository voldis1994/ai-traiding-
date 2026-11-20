import { useState } from "react";
import { BOOK_RATIO, ORDER_ASKS, ORDER_BIDS, SPREAD, type BookRow } from "../../data/mockOrderBook";
import { LiveBadge } from "../ui/LiveBadge";

function fmtPrice(n: number) {
  return n.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

export function OrderBook({
  asks,
  bids,
  spread,
  ratio,
  epic,
}: {
  asks?: BookRow[];
  bids?: BookRow[];
  spread?: { abs: number; bps: number };
  ratio?: { bid: number; ask: number };
  epic?: string;
}) {
  const [tab, setTab] = useState<"BOOK" | "CMP">("BOOK");
  const askRows = asks && asks.length ? asks : ORDER_ASKS;
  const bidRows = bids && bids.length ? bids : ORDER_BIDS;
  const spr = spread || SPREAD;
  const rat = ratio || BOOK_RATIO;
  return (
    <section className="panel order-book">
      <header className="panel-header">
        <span>ORDER BOOK {(epic || "BTC").split(/[./]/)[0]}</span>
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
        <span>SIZE</span>
        <span>VALUE</span>
        <span>TOTAL</span>
      </div>
      <div className="panel-body ob-body">
        {tab === "BOOK" ? (
          <>
            {askRows.map((r) => (
              <div key={`a-${r.price}`} className="ob-row ask">
                <i style={{ width: `${r.depth}%` }} />
                <span className="num">{fmtPrice(r.price)}</span>
                <span className="num">{r.size.toFixed(3)}</span>
                <span className="num">{r.value}</span>
                <span className="num">{r.total.toFixed(3)}</span>
              </div>
            ))}
            <div className="ob-spread">
              SPREAD {spr.abs.toFixed(1)} · {spr.bps.toFixed(1)} BPS
            </div>
            {bidRows.map((r) => (
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
          <div className="ob-cmp">CMP — Capital mid/spread composite</div>
        )}
      </div>
      <div className="ob-ratio">
        <div className="ob-ratio-bar">
          <i className="b" style={{ width: `${rat.bid}%` }} />
          <i className="a" style={{ width: `${rat.ask}%` }} />
        </div>
        <div className="ob-ratio-lbl">
          <span className="up">{rat.bid}% BID</span>
          <span className="dn">{rat.ask}% ASK</span>
        </div>
      </div>
    </section>
  );
}
