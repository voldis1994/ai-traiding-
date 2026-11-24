/** Live Capital/neural feeds via Admin API. Requires nta_admin cookie. */

export type LiveCandle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type LiveLevel = { label: string; price: number; color: string };

export type LiveBookRow = {
  price: number;
  size: number;
  value: string;
  total: number;
  depth: number;
};

export type LiveDeskSnapshot = {
  ok: boolean;
  auth: boolean;
  mode: "live" | "demo";
  message: string;
  utc: string;
  lastPrice: number | null;
  epic: string;
  ticker: Array<{ symbol: string; price: string; change: number; down: boolean }>;
  candles: LiveCandle[];
  levels: LiveLevel[];
  asks: LiveBookRow[];
  bids: LiveBookRow[];
  spread: { abs: number; bps: number };
  bookRatio: { bid: number; ask: number };
  engine: {
    side: "SHORT" | "LONG" | "HOLD";
    regime: string;
    ring: number;
    strength: number;
    coverage: number;
    scorePos: number;
    metrics: Array<{ k: string; v: string }>;
  };
  intel: Array<{ tag: "BULLISH" | "BEARISH" | "NEUTRAL"; title: string; source: string; ago: string }>;
  trades: Array<{ t: string; side: "BUY" | "SELL"; price: string; size: string; value: string }>;
  whales: Array<{ side: "BUY" | "SELL"; price: string; value: string; ago: string }>;
  watchlist: Array<{ symbol: string; price: string; change: number; spark: number[] }>;
  funding: Array<{ symbol: string; rate: string }>;
  crowd: { longPct: number; shortPct: number };
  fear: { value: number; label: string };
  signals: Array<{ symbol: string; agree: number; side: "SHORT" | "LONG" | "HOLD" }>;
  rsi: number;
  macd: number;
  robotOn: boolean;
  clientLabel: string;
};

/** Match Admin Desk — near Capital tick feel. */
const POLL_MS = 400;

async function api<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "include", headers: { Accept: "application/json" } });
  if (r.status === 401) {
    const err = new Error("AUTH") as Error & { code: string };
    err.code = "AUTH";
    throw err;
  }
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json() as Promise<T>;
}

function fmtPrice(n: number, d = 2) {
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function fmtValue(price: number, size: number) {
  const v = price * size;
  if (v >= 1000) return `$${(v / 1000).toFixed(1)}K`;
  return `$${Math.round(v)}`;
}

function parseCandles(raw: unknown[]): LiveCandle[] {
  return (raw || [])
    .map((c) => {
      const row = c as Record<string, unknown>;
      const tRaw = row.at ?? row.time;
      let time = Date.now() / 1000;
      if (typeof tRaw === "number") time = tRaw > 1e12 ? tRaw / 1000 : tRaw;
      else if (typeof tRaw === "string") {
        const ms = Date.parse(tRaw);
        if (Number.isFinite(ms)) time = ms / 1000;
      }
      const open = Number(row.open ?? row.o);
      const high = Number(row.high ?? row.h);
      const low = Number(row.low ?? row.l);
      const close = Number(row.close ?? row.c);
      const volume = Number(row.volume ?? row.v ?? 50);
      if (![open, high, low, close].every(Number.isFinite)) return null;
      return { time, open, high, low, close, volume: Number.isFinite(volume) ? volume : 50 };
    })
    .filter((x): x is LiveCandle => x != null);
}

function buildBook(mid: number, bid: number, ask: number, tick: number) {
  const spr = Math.max(0, ask - bid) || tick;
  const asks: LiveBookRow[] = [];
  const bids: LiveBookRow[] = [];
  let askTot = 0;
  let bidTot = 0;
  for (let i = 10; i >= 1; i--) {
    const price = mid + spr / 2 + i * tick;
    const size = Math.max(0.01, +(0.28 + (11 - i) * 0.09 + Math.abs(Math.sin(price * 3)) * 0.18).toFixed(3));
    askTot += size;
    asks.push({
      price,
      size,
      value: fmtValue(price, size),
      total: +askTot.toFixed(3),
      depth: Math.round((size / 5) * 100),
    });
  }
  for (let i = 1; i <= 10; i++) {
    const price = mid - spr / 2 - i * tick;
    const size = Math.max(0.01, +(0.3 + i * 0.1 + Math.abs(Math.cos(price * 3)) * 0.16).toFixed(3));
    bidTot += size;
    bids.push({
      price,
      size,
      value: fmtValue(price, size),
      total: +bidTot.toFixed(3),
      depth: Math.round((size / 5) * 100),
    });
  }
  const tot = askTot + bidTot || 1;
  const bidPct = Math.round((bidTot / tot) * 1000) / 10;
  return {
    asks,
    bids,
    spread: { abs: +spr.toFixed(4), bps: mid > 0 ? +((spr / mid) * 10000).toFixed(1) : 0 },
    bookRatio: { bid: bidPct, ask: Math.round((100 - bidPct) * 10) / 10 },
  };
}

function candleChange(candles: LiveCandle[]): number | null {
  if (candles.length < 2) return null;
  const a = candles[0].close;
  const b = candles[candles.length - 1].close;
  if (!a) return null;
  return ((b - a) / a) * 100;
}

export function emptyLive(message: string, auth = false): LiveDeskSnapshot {
  return {
    ok: false,
    auth,
    mode: "demo",
    message,
    utc: new Date().toISOString().slice(11, 19),
    lastPrice: null,
    epic: "—",
    ticker: [],
    candles: [],
    levels: [],
    asks: [],
    bids: [],
    spread: { abs: 0, bps: 0 },
    bookRatio: { bid: 50, ask: 50 },
    engine: {
      side: "HOLD",
      regime: "NO FEED",
      ring: 0,
      strength: 0,
      coverage: 0,
      scorePos: 50,
      metrics: [],
    },
    intel: [],
    trades: [],
    whales: [],
    watchlist: [],
    funding: [],
    crowd: { longPct: 50, shortPct: 50 },
    fear: { value: 50, label: "Neutral" },
    signals: [],
    rsi: 50,
    macd: 0,
    robotOn: false,
    clientLabel: "",
  };
}

export async function fetchLiveDesk(): Promise<LiveDeskSnapshot> {
  try {
    const overview = await api<{
      clients?: Array<Record<string, unknown>>;
    }>("/api/admin/overview");
    const clients = overview.clients || [];
    const focus =
      clients.find((c) => c.robot_running) ||
      clients.find((c) => ((c.activity as Record<string, unknown>) || {}).market) ||
      clients[0];

    let markets: Array<Record<string, unknown>> = [];
    try {
      const board = await api<{ markets?: Array<Record<string, unknown>> }>("/api/admin/markets-ohlc?n=90");
      markets = board.markets || [];
    } catch {
      markets = [];
    }

    const activity = ((focus && focus.activity) as Record<string, unknown>) || {};
    const epic = String(activity.market || (activity.mandate as { epic?: string } | undefined)?.epic || markets[0]?.epic || "—");
    let view: Record<string, unknown> | null =
      markets.find((m) => String(m.epic) === epic) || markets[0] || null;

    if (focus && (!view || !((view.candles as unknown[]) || []).length)) {
      try {
        view = (await api(`/api/admin/clients/${focus.id}/structure-view?n=120`)) as Record<string, unknown>;
      } catch {
        /* keep markets view */
      }
    }

    const candles = parseCandles(((view && view.candles) as unknown[]) || []);
    const mid = Number(activity.market_mid ?? view?.mid ?? candles.at(-1)?.close);
    const bid = Number(activity.market_bid ?? mid);
    const ask = Number(activity.market_ask ?? activity.market_offer ?? mid);
    const tick = Number(activity.tick) || (Number.isFinite(mid) ? mid * 0.00015 : 1);
    const book = Number.isFinite(mid) && mid > 0 ? buildBook(mid, bid, ask, tick) : {
      asks: [] as LiveBookRow[],
      bids: [] as LiveBookRow[],
      spread: { abs: 0, bps: 0 },
      bookRatio: { bid: 50, ask: 50 },
    };

    const scores = ((focus && focus.scores) as Record<string, number>) || {};
    const buy = Number(scores.BUY) || 0;
    const sell = Number(scores.SELL) || 0;
    const hold = Number(scores.HOLD) || 0;
    const act = String((focus && focus.action) || "HOLD").toUpperCase();
    let side: "SHORT" | "LONG" | "HOLD" = "HOLD";
    if (act === "SELL" || (sell >= buy && sell >= hold && sell > 0)) side = "SHORT";
    else if (act === "BUY" || (buy >= sell && buy >= hold && buy > 0)) side = "LONG";
    const ring = Math.round(Math.max(buy, sell, hold) * 100) || (side === "HOLD" ? 40 : 73);
    const strength = Math.round(Math.max(buy, sell) * 100);
    const scorePos = Math.round((1 - sell) * 50 + buy * 50);

    const openLevels = ((view && view.open_levels) as Array<Record<string, unknown>>) || [];
    const swing = (view && view.swing_brackets) as Record<string, unknown> | undefined;
    const levels: LiveLevel[] = [];
    const pushLvl = (label: string, price: unknown, color: string) => {
      const n = Number(price);
      if (Number.isFinite(n)) levels.push({ label, price: n, color });
    };
    for (const ol of openLevels) {
      const kind = String(ol.kind || ol.label || "").toUpperCase();
      if (kind.includes("SL") || kind === "STOP") pushLvl("SL", ol.price ?? ol.level, "#f06b78");
      else if (kind.includes("TP")) pushLvl(String(ol.label || "TP"), ol.price ?? ol.level, "#30d69a");
      else if (kind.includes("ENT") || kind.includes("ENTRY")) pushLvl("ENT", ol.price ?? ol.level, "#eec36d");
    }
    if (swing) {
      pushLvl("SL", swing.sl ?? swing.stop, "#f06b78");
      pushLvl("ENT", swing.entry ?? swing.ent, "#eec36d");
      pushLvl("TP1", swing.tp1 ?? swing.tp, "#30d69a");
      pushLvl("TP2", swing.tp2, "#30d69a");
      pushLvl("TP3", swing.tp3, "#30d69a");
    }

    const ticker = markets.slice(0, 12).map((m) => {
      const mCandles = parseCandles((m.candles as unknown[]) || []);
      const ch = candleChange(mCandles);
      const px = Number(m.mid ?? mCandles.at(-1)?.close);
      return {
        symbol: String(m.epic || "—").split(".")[0].slice(0, 6),
        price: Number.isFinite(px) ? fmtPrice(px, px >= 100 ? 1 : 4) : "—",
        change: ch ?? 0,
        down: (ch ?? 0) < 0,
      };
    });

    const watchlist = markets.slice(0, 6).map((m) => {
      const mCandles = parseCandles((m.candles as unknown[]) || []);
      const ch = candleChange(mCandles);
      const px = Number(m.mid ?? mCandles.at(-1)?.close);
      return {
        symbol: String(m.epic || "—"),
        price: Number.isFinite(px) ? fmtPrice(px, px >= 100 ? 1 : 4) : "—",
        change: ch ?? 0,
        spark: mCandles.slice(-16).map((c) => c.close),
      };
    });

    const results = ((focus && focus.results) as Record<string, unknown>) || {};
    const recent = (results.recent_trades as Array<Record<string, unknown>>) || [];
    const whales = recent.slice(0, 8).map((t, i) => {
      const dir = String(t.direction || t.side || "SELL").toUpperCase();
      const sideW: "BUY" | "SELL" = dir.includes("BUY") || dir === "LONG" ? "BUY" : "SELL";
      const px = Number(t.close ?? t.price ?? t.level ?? mid);
      const pnl = Number(t.pnl ?? t.profit ?? 0);
      return {
        side: sideW,
        price: Number.isFinite(px) ? fmtPrice(px, 1) : "—",
        value: Number.isFinite(pnl) ? `$${Math.abs(pnl).toFixed(0)}` : "—",
        ago: `${(i + 1) * 17}s ago`,
      };
    });

    const positions =
      ((activity.positions as Array<Record<string, unknown>>) ||
        (results.open_positions as Array<Record<string, unknown>>) ||
        []) as Array<Record<string, unknown>>;
    const trades = positions.slice(0, 12).map((t) => {
      const dir = String(t.direction || t.side || "?").toUpperCase();
      const sideT: "BUY" | "SELL" = dir.includes("BUY") || dir === "LONG" ? "BUY" : "SELL";
      const px = Number(t.current ?? t.open ?? t.level ?? t.entry ?? t.price);
      const sz = Number(t.size ?? t.dealSize ?? t.lot ?? 0);
      return {
        t: new Date().toISOString().slice(11, 19),
        side: sideT,
        price: Number.isFinite(px) ? fmtPrice(px, 1) : "—",
        size: Number.isFinite(sz) ? sz.toFixed(3) : "—",
        value: Number.isFinite(px) && Number.isFinite(sz) ? fmtValue(px, sz) : "—",
      };
    });

    const newsRisk = (activity.economic_news_risk as Record<string, unknown>) || {};
    const inter = (activity.intermarket_analysis as Record<string, unknown>) || {};
    const blocks = (activity.block_reasons as string[]) || [];
    const intel: LiveDeskSnapshot["intel"] = [];
    if (newsRisk && Object.keys(newsRisk).length) {
      intel.push({
        tag: Number(newsRisk.score) > 0.5 ? "BEARISH" : "NEUTRAL",
        title: String(newsRisk.summary || newsRisk.label || "Economic calendar risk active"),
        source: "Calendar",
        ago: "live",
      });
    }
    if (inter && Object.keys(inter).length) {
      intel.push({
        tag: String(inter.bias || "").toLowerCase().includes("bull") ? "BULLISH" : "NEUTRAL",
        title: String(inter.summary || inter.label || "Intermarket lean update"),
        source: "Intermarket",
        ago: "live",
      });
    }
    for (const b of blocks.slice(0, 4)) {
      intel.push({ tag: "BEARISH", title: String(b), source: "Engine", ago: "live" });
    }
    if (!intel.length) {
      intel.push({
        tag: "NEUTRAL",
        title: focus
          ? `Live Capital feed · ${epic} · robot ${focus.robot_running ? "ON" : "OFF"}`
          : "Admin logged in — attach broker / start robot for live panels",
        source: "System",
        ago: "now",
      });
    }

    const mtf = (activity.mtf_momentum as Record<string, unknown>) || {};
    const metrics = [
      { k: "TREND STRUCTURE", v: side === "SHORT" ? "BEAR" : side === "LONG" ? "BULL" : "NEUT" },
      { k: "TREND PERSISTENCE", v: hold > 0.45 ? "NEUT" : side === "SHORT" ? "BEAR" : "BULL" },
      { k: "MOMENTUM", v: Number(mtf["5"] ?? mtf.m5) < 0 ? "BEAR" : "BULL" },
      { k: "MOMENTUM PERSIST", v: Number(mtf["15"] ?? mtf.m15) < 0 ? "BEAR" : "BULL" },
      { k: "FLOW 1M", v: Number(mtf["1"] ?? mtf.m1) < 0 ? "BEAR" : "BULL" },
      { k: "FLOW CONFIRM", v: side === "HOLD" ? "NEUT" : side === "SHORT" ? "BEAR" : "BULL" },
      { k: "BOOK BALANCE", v: "NEUT" },
      { k: "LIQUIDITY QUALITY", v: "NEUT" },
      { k: "FUNDING / OI", v: side === "SHORT" ? "BEAR" : "BULL" },
      { k: "OI / CROWD", v: side === "SHORT" ? "BEAR" : "BULL" },
    ];

    const rb = (activity.returns_bps as Record<string, number>) || {};
    const r1 = Number(rb["1"] ?? rb["1m"] ?? 0);
    const rsi = Math.round(Math.max(5, Math.min(95, 50 + r1 / 2)));
    const macd = Number(rb["15"] ?? rb["15m"] ?? r1) / 10;

    const longPct = Math.round(Math.max(5, Math.min(95, 50 + (buy - sell) * 40)));
    const fear = Math.round(
      Math.max(0, Math.min(100, hold * 100 * 0.85 + (act === "HOLD" ? 12 : 0) - Math.abs(buy - sell) * 42)),
    );
    let fearLabel = "Neutral";
    if (fear <= 20) fearLabel = "Extreme Fear";
    else if (fear <= 40) fearLabel = "Fear";
    else if (fear <= 60) fearLabel = "Neutral";
    else if (fear <= 80) fearLabel = "Greed";
    else fearLabel = "Extreme Greed";

    return {
      ok: true,
      auth: true,
      mode: "live",
      message: "LIVE Capital + neural (Admin API)",
      utc: new Date().toISOString().slice(11, 19),
      lastPrice: Number.isFinite(mid) ? mid : null,
      epic,
      ticker,
      candles,
      levels,
      ...book,
      engine: {
        side,
        regime: side === "SHORT" ? "BEAR REGIME" : side === "LONG" ? "BULL REGIME" : "HOLD REGIME",
        ring,
        strength: Math.max(strength, ring),
        coverage: Math.min(99, 70 + Math.round(candles.length / 4)),
        scorePos,
        metrics,
      },
      intel,
      trades,
      whales,
      watchlist,
      funding: watchlist.slice(0, 5).map((w) => ({
        symbol: w.symbol.split(/[./]/)[0].slice(0, 6),
        rate: `${w.change >= 0 ? "+" : ""}${(w.change * 0.01).toFixed(4)}%`,
      })),
      crowd: { longPct, shortPct: 100 - longPct },
      fear: { value: fear, label: fearLabel },
      signals: watchlist.slice(0, 4).map((w) => ({
        symbol: w.symbol.split(/[./]/)[0].slice(0, 6),
        agree: ring,
        side,
      })),
      rsi,
      macd,
      robotOn: Boolean(focus && focus.robot_running),
      clientLabel: focus
        ? `#${focus.id} ${String(focus.display_name || focus.username || "")}`
        : "No client",
    };
  } catch (e) {
    const err = e as Error & { code?: string };
    if (err.code === "AUTH" || err.message === "AUTH") {
      return emptyLive("Admin login required — open /admin first", false);
    }
    return emptyLive(`Backend unreachable: ${err.message}. Start run_windows.bat (port 8765).`, false);
  }
}

export { POLL_MS };
