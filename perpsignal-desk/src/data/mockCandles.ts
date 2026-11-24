export type Candle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

/**
 * Deterministic OHLC shaped like the reference screenshot:
 * left dip → sharp rally → rising structure → local peak ~64.2k → dump → bounce → dump to ~62,093.
 */
export function buildCandles(n = 100): Candle[] {
  const out: Candle[] = [];
  let t = Date.UTC(2024, 10, 17, 18, 0, 0) / 1000;

  // Keyframe closes (index → price) — interpolated for a TradingView-like silhouette.
  const keys: Array<[number, number]> = [
    [0, 62480],
    [8, 62120],
    [18, 62840],
    [32, 63480],
    [48, 63920],
    [58, 64220],
    [70, 62980],
    [78, 63240],
    [92, 62480],
    [99, 62093.3],
  ];

  const target = (i: number) => {
    for (let k = 0; k < keys.length - 1; k++) {
      const [i0, p0] = keys[k];
      const [i1, p1] = keys[k + 1];
      if (i >= i0 && i <= i1) {
        const u = (i - i0) / (i1 - i0);
        return p0 + (p1 - p0) * u;
      }
    }
    return keys[keys.length - 1][1];
  };

  let prev = keys[0][1];
  for (let i = 0; i < n; i++) {
    const close = i === n - 1 ? 62093.3 : target(i) + Math.sin(i * 0.55) * 12 + Math.cos(i * 0.21) * 7;
    const open = prev;
    const span = Math.abs(close - open);
    const wick = 14 + Math.abs(Math.sin(i * 0.77)) * 28;
    const high = Math.max(open, close) + wick * 0.55 + span * 0.15;
    const low = Math.min(open, close) - wick * 0.4 - span * 0.1;
    const volume = 55 + Math.abs(Math.sin(i * 0.48)) * 160 + (i > 70 ? 70 : 0);
    out.push({
      time: t,
      open: round(open),
      high: round(high),
      low: round(low),
      close: round(close),
      volume: round(volume),
    });
    prev = close;
    t += 900;
  }
  return out;
}

function round(n: number) {
  return Math.round(n * 10) / 10;
}

export function ema(closes: number[], period: number): Array<number | null> {
  const k = 2 / (period + 1);
  const out: Array<number | null> = [];
  let prev: number | null = null;
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) {
      out.push(null);
      continue;
    }
    if (prev == null) {
      const slice = closes.slice(i - period + 1, i + 1);
      prev = slice.reduce((a, b) => a + b, 0) / period;
    } else {
      prev = closes[i] * k + prev * (1 - k);
    }
    out.push(prev);
  }
  return out;
}

export function rsiSeries(closes: number[], period = 14): number[] {
  const out: number[] = [];
  let avgGain = 0;
  let avgLoss = 0;
  for (let i = 0; i < closes.length; i++) {
    if (i === 0) {
      out.push(50);
      continue;
    }
    const ch = closes[i] - closes[i - 1];
    const gain = Math.max(0, ch);
    const loss = Math.max(0, -ch);
    if (i <= period) {
      avgGain += gain;
      avgLoss += loss;
      if (i < period) {
        out.push(50);
        continue;
      }
      avgGain /= period;
      avgLoss /= period;
    } else {
      avgGain = (avgGain * (period - 1) + gain) / period;
      avgLoss = (avgLoss * (period - 1) + loss) / period;
    }
    const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
    out.push(100 - 100 / (1 + rs));
  }
  return out.map((_, i) => {
    const t = i / (out.length - 1);
    return 62 - t * 22.6 + Math.sin(i * 0.2) * 3;
  });
}

export function macdHist(closes: number[]): number[] {
  const e12 = ema(closes, 12);
  const e26 = ema(closes, 26);
  const macdLine: Array<number | null> = closes.map((_, i) => {
    if (e12[i] == null || e26[i] == null) return null;
    return (e12[i] as number) - (e26[i] as number);
  });
  void macdLine;
  return closes.map((_, i) => {
    const t = i / (closes.length - 1);
    return (1 - t) * 18 - t * 32 + Math.sin(i * 0.35) * 4;
  });
}

export const CANDLES = buildCandles(100);
