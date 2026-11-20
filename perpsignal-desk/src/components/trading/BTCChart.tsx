import { useEffect, useMemo, useRef, useState } from "react";
import { CANDLES, ema, type Candle } from "../../data/mockCandles";
import { LEVELS } from "../../data/mockMarketData";
import { LiveBadge } from "../ui/LiveBadge";

const TFS = ["1M", "5M", "15M", "1H", "4H"] as const;
type Level = { label: string; price: number; color: string };

export function BTCChart({
  tf,
  onTf,
  candles,
  levels,
  epic,
}: {
  tf: (typeof TFS)[number];
  onTf: (t: (typeof TFS)[number]) => void;
  candles?: Candle[];
  levels?: Level[];
  epic?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [cross, setCross] = useState<{ x: number; y: number } | null>(null);
  const series = candles && candles.length ? candles : CANDLES;
  const lvl = levels && levels.length ? levels : LEVELS;

  const closes = useMemo(() => series.map((c) => c.close), [series]);
  const ema20 = useMemo(() => ema(closes, 20), [closes]);
  const ema50 = useMemo(() => ema(closes, 50), [closes]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;

    const paint = () => {
      const dpr = window.devicePixelRatio || 1;
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const padL = 4;
      const padR = 58;
      const padT = 14;
      const padB = 28;
      const volH = Math.floor(h * 0.16);
      const chartH = h - padT - padB - volH;

      ctx.fillStyle = "#060a14";
      ctx.fillRect(0, 0, w, h);

      // watermark
      ctx.fillStyle = "rgba(120,130,150,0.07)";
      ctx.font = "700 18px IBM Plex Mono";
      ctx.fillText("PerpSignalDesk", padL + 8, padT + 22);

      // Scale primarily to candle range so bodies stay visible; levels may sit near edges.
      const candlePrices = series.flatMap((c) => [c.high, c.low]);
      let minP = Math.min(...candlePrices);
      let maxP = Math.max(...candlePrices);
      // Pull in nearby levels so SL/ENT/TP remain on-canvas when close.
      lvl.forEach((l) => {
        if (l.price > maxP && l.price < maxP + 400) maxP = l.price;
        if (l.price < minP && l.price > minP - 400) minP = l.price;
      });
      const padPx = (maxP - minP) * 0.06;
      minP -= padPx;
      maxP += padPx;
      const yOf = (p: number) => padT + ((maxP - p) / (maxP - minP)) * chartH;
      const slot = (w - padL - padR) / series.length;

      // grid
      ctx.strokeStyle = "rgba(40,48,59,0.65)";
      ctx.lineWidth = 1;
      for (let i = 0; i < 6; i++) {
        const y = padT + (chartH / 5) * i;
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(w - padR, y);
        ctx.stroke();
      }
      for (let i = 0; i < 8; i++) {
        const x = padL + ((w - padL - padR) / 7) * i;
        ctx.beginPath();
        ctx.moveTo(x, padT);
        ctx.lineTo(x, padT + chartH);
        ctx.stroke();
      }

      // volume profile (right of plot, left of labels)
      const buckets = 24;
      const counts = new Array(buckets).fill(0);
      series.forEach((c) => {
        const mid = (c.high + c.low) / 2;
        const b = Math.min(
          buckets - 1,
          Math.max(0, Math.floor(((mid - minP) / (maxP - minP)) * buckets)),
        );
        counts[b] += c.volume;
      });
      const maxC = Math.max(...counts, 1);
      const vpX = w - padR - 34;
      counts.forEach((c, i) => {
        const y1 = yOf(minP + ((i + 1) / buckets) * (maxP - minP));
        const y0 = yOf(minP + (i / buckets) * (maxP - minP));
        const ww = (c / maxC) * 30;
        ctx.fillStyle = "rgba(120,130,150,0.22)";
        ctx.fillRect(vpX + (30 - ww), Math.min(y0, y1), ww, Math.abs(y0 - y1) - 1);
      });

      // candles — thick enough to read as OHLC sticks at ~100 bars
      series.forEach((c, i) => {
        const x = padL + i * slot + slot / 2;
        const up = c.close >= c.open;
        const col = up ? "#30d69a" : "#f06b78";
        ctx.strokeStyle = col;
        ctx.fillStyle = col;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, yOf(c.high));
        ctx.lineTo(x, yOf(c.low));
        ctx.stroke();
        const y1 = yOf(c.open);
        const y2 = yOf(c.close);
        const body = Math.max(1.5, Math.abs(y1 - y2));
        const bw = Math.max(2.4, Math.min(5, slot * 0.72));
        ctx.fillRect(x - bw / 2, Math.min(y1, y2), bw, body);
      });

      // EMA lines
      const drawEma = (series: Array<number | null>, color: string) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        let started = false;
        series.forEach((v, i) => {
          if (v == null) return;
          const x = padL + i * slot + slot / 2;
          const y = yOf(v);
          if (!started) {
            ctx.moveTo(x, y);
            started = true;
          } else ctx.lineTo(x, y);
        });
        ctx.stroke();
      };
      drawEma(ema20, "#7eb6e8");
      drawEma(ema50, "#c9b28a");

      // volume histogram
      const maxV = Math.max(...series.map((c) => c.volume));
      series.forEach((c, i) => {
        const x = padL + i * slot;
        const vh = (c.volume / maxV) * (volH - 4);
        const up = c.close >= c.open;
        ctx.fillStyle = up ? "rgba(48,214,154,0.45)" : "rgba(240,107,120,0.45)";
        ctx.fillRect(x + 0.5, h - padB - vh, Math.max(1, slot - 1), vh);
      });

      // signal levels + price tags
      const fmt = (n: number) =>
        n.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
      lvl.forEach((l) => {
        const y = yOf(l.price);
        ctx.strokeStyle = l.color;
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(w - padR, y);
        ctx.stroke();
        ctx.setLineDash([]);
        const label = `${l.label} ${fmt(l.price)}`;
        ctx.font = "700 8px IBM Plex Mono";
        const tw = ctx.measureText(label).width + 6;
        const bx = w - padR + 1;
        const by = y - 6;
        ctx.fillStyle = l.color;
        ctx.fillRect(bx, by, Math.min(tw, padR - 2), 12);
        ctx.fillStyle = l.label === "SL" ? "#1a0c10" : "#0a0f18";
        ctx.fillText(label, bx + 3, y + 3);
      });

      // price axis labels
      ctx.fillStyle = "#828b9d";
      ctx.font = "8px IBM Plex Mono";
      for (let i = 0; i < 5; i++) {
        const p = maxP - ((maxP - minP) / 4) * i;
        const y = yOf(p);
        ctx.fillText(
          p.toLocaleString("en-US", { maximumFractionDigits: 1 }),
          w - padR + 4,
          Math.max(padT + 8, y - 8),
        );
      }

      // time labels
      for (let i = 0; i < 5; i++) {
        const idx = Math.floor((series.length - 1) * (i / 4));
        const x = padL + idx * slot;
        const d = new Date(series[idx].time * 1000);
        const hh = d.getUTCHours();
        const ampm = hh >= 12 ? "PM" : "AM";
        const h12 = ((hh + 11) % 12) + 1;
        ctx.fillText(
          `${String(h12).padStart(2, "0")}:00 ${ampm}`,
          x,
          h - 8,
        );
      }

      // corner legend with colored EMA names
      ctx.font = "8px IBM Plex Mono";
      ctx.fillStyle = "#828b9d";
      ctx.fillText(`${tf}  `, padL + 4, padT - 3);
      ctx.fillStyle = "#7eb6e8";
      ctx.fillText("EMA20", padL + 28, padT - 3);
      ctx.fillStyle = "#c9b28a";
      ctx.fillText("EMA50", padL + 58, padT - 3);
      ctx.fillStyle = "#828b9d";
      ctx.fillText(` · ${series.length} bars`, padL + 88, padT - 3);

      if (cross) {
        ctx.strokeStyle = "rgba(203,208,218,0.35)";
        ctx.setLineDash([2, 2]);
        ctx.beginPath();
        ctx.moveTo(cross.x, padT);
        ctx.lineTo(cross.x, padT + chartH);
        ctx.moveTo(padL, cross.y);
        ctx.lineTo(w - padR, cross.y);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    };

    paint();
    const ro = new ResizeObserver(paint);
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [ema20, ema50, tf, cross, series, lvl]);

  return (
    <section className="panel btc-chart">
      <header className="panel-header">
        <span>{(epic || "BTC/USDT").replace(".", "/")}</span>
        <span className="chart-tools">
          <button type="button" className="mini-tab">
            VP
          </button>
          <button type="button" className="mini-tab">
            SIGNALS
          </button>
          {TFS.map((t) => (
            <button
              key={t}
              type="button"
              className={`mini-tab ${tf === t ? "tf-on" : ""}`}
              onClick={() => onTf(t)}
            >
              {t}
            </button>
          ))}
          <LiveBadge />
        </span>
      </header>
      <div
        className="chart-wrap"
        ref={wrapRef}
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          setCross({ x: e.clientX - r.left, y: e.clientY - r.top });
        }}
        onMouseLeave={() => setCross(null)}
      >
        <canvas ref={canvasRef} />
      </div>
    </section>
  );
}