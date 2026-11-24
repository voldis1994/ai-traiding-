import { LiveBadge } from "../ui/LiveBadge";
import type { Candle } from "../../data/mockCandles";

export function IndicatorsPanel({
  rsi,
  macd,
}: {
  rsi?: number | null;
  macd?: number | null;
  candles?: Candle[];
}) {
  const hasRsi = rsi != null && Number.isFinite(rsi);
  const hasMacd = macd != null && Number.isFinite(macd);

  return (
    <section className="panel indicators">
      <header className="panel-header">
        <span>INDICATORS — RSI / MACD</span>
        <LiveBadge />
      </header>
      <div className="ind-head">
        <span>
          RSI <b className="num">{hasRsi ? Number(rsi).toFixed(1) : "N/A"}</b>
        </span>
        <span>
          MACD HIST{" "}
          <b className={`num ${hasMacd && Number(macd) < 0 ? "dn" : "up"}`}>
            {hasMacd ? Number(macd).toFixed(2) : "N/A"}
          </b>
        </span>
      </div>
      <div className="ind-charts" style={{ padding: "12px 10px", color: "#8a93a5", fontSize: 12 }}>
        {hasRsi || hasMacd
          ? "Capital / verified feed"
          : "N/A — RSI/MACD not provided by Capital (no synthetic LIVE values)"}
      </div>
    </section>
  );
}
