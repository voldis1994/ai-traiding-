import { useEffect, useRef } from "react";
import { CANDLES, macdHist, rsiSeries, type Candle } from "../../data/mockCandles";
import { LiveBadge } from "../ui/LiveBadge";

export function IndicatorsPanel({
  rsi,
  macd,
  candles,
}: {
  rsi?: number;
  macd?: number;
  candles?: Candle[];
}) {
  const rsiRef = useRef<HTMLCanvasElement>(null);
  const macdRef = useRef<HTMLCanvasElement>(null);
  const series = candles && candles.length ? candles : CANDLES;
  const closes = series.map((c) => c.close);
  const rsiData = rsiSeries(closes);
  const macdData = macdHist(closes);
  const rsiNow = rsi ?? rsiData.at(-1) ?? 39.4;
  const macdNow = macd ?? macdData.at(-1) ?? -30.54;

  useEffect(() => {
    const drawRsi = () => {
      const c = rsiRef.current;
      if (!c) return;
      const parent = c.parentElement!;
      const dpr = window.devicePixelRatio || 1;
      const w = parent.clientWidth;
      const h = parent.clientHeight;
      c.width = w * dpr;
      c.height = h * dpr;
      c.style.width = `${w}px`;
      c.style.height = `${h}px`;
      const ctx = c.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = "#060a14";
      ctx.fillRect(0, 0, w, h);
      const yOf = (v: number) => 4 + ((100 - v) / 100) * (h - 8);
      ctx.strokeStyle = "rgba(40,48,59,0.8)";
      [30, 70].forEach((lvl) => {
        const y = yOf(lvl);
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      });
      ctx.strokeStyle = "#699bdd";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      rsiData.forEach((v, i) => {
        const x = (i / Math.max(1, rsiData.length - 1)) * w;
        const y = yOf(v);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    };

    const drawMacd = () => {
      const c = macdRef.current;
      if (!c) return;
      const parent = c.parentElement!;
      const dpr = window.devicePixelRatio || 1;
      const w = parent.clientWidth;
      const h = parent.clientHeight;
      c.width = w * dpr;
      c.height = h * dpr;
      c.style.width = `${w}px`;
      c.style.height = `${h}px`;
      const ctx = c.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = "#060a14";
      ctx.fillRect(0, 0, w, h);
      const max = Math.max(...macdData.map(Math.abs), 1);
      const mid = h / 2;
      const slot = w / Math.max(1, macdData.length);
      macdData.forEach((v, i) => {
        const bh = (Math.abs(v) / max) * (h / 2 - 2);
        ctx.fillStyle = v >= 0 ? "#30d69a" : "#f06b78";
        if (v >= 0) ctx.fillRect(i * slot, mid - bh, Math.max(1, slot - 0.5), bh);
        else ctx.fillRect(i * slot, mid, Math.max(1, slot - 0.5), bh);
      });
      ctx.strokeStyle = "rgba(130,139,157,0.5)";
      ctx.beginPath();
      ctx.moveTo(0, mid);
      ctx.lineTo(w, mid);
      ctx.stroke();
    };

    drawRsi();
    drawMacd();
  }, [rsiData, macdData]);

  return (
    <section className="panel indicators">
      <header className="panel-header">
        <span>INDICATORS — RSI / MACD</span>
        <LiveBadge />
      </header>
      <div className="ind-head">
        <span>
          RSI <b className="num">{Number(rsiNow).toFixed(1)}</b>
        </span>
        <span>
          MACD HIST{" "}
          <b className={`num ${Number(macdNow) < 0 ? "dn" : "up"}`}>{Number(macdNow).toFixed(2)}</b>
        </span>
      </div>
      <div className="ind-charts">
        <div className="ind-canvas">
          <canvas ref={rsiRef} />
        </div>
        <div className="ind-canvas">
          <canvas ref={macdRef} />
        </div>
      </div>
    </section>
  );
}
