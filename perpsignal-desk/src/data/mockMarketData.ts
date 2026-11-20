export const TICKER = [
  { symbol: "BTC", price: "62,093.3", change: -0.42, down: true },
  { symbol: "ETH", price: "1,758.00", change: -1.29, down: true },
  { symbol: "SOL", price: "73.18", change: -0.48, down: true },
  { symbol: "BNB", price: "581.93", change: -0.81, down: true },
  { symbol: "XRP", price: "0.9618", change: -0.77, down: true },
  { symbol: "XAU", price: "3,673.4", change: -0.18, down: true },
  { symbol: "XAG", price: "42.86", change: 0.22, down: false },
  { symbol: "WTI", price: "68.41", change: -0.35, down: true },
  { symbol: "ADA", price: "0.412", change: -0.91, down: true },
  { symbol: "APT", price: "6.24", change: -1.05, down: true },
  { symbol: "ARB", price: "0.382", change: -0.64, down: true },
  { symbol: "ATOM", price: "4.91", change: 0.18, down: false },
] as const;

export const NAV_ITEMS = [
  { id: "desk", n: 1, label: "DESK" },
  { id: "terminal", n: 2, label: "TERMINAL" },
  { id: "global", n: 3, label: "GLOBAL" },
  { id: "flow", n: 4, label: "FLOW-LIQ" },
  { id: "oi", n: 5, label: "OI-FUND" },
  { id: "liq", n: 6, label: "LIQUID'N" },
  { id: "research", n: 7, label: "RESEARCH" },
  { id: "news", n: 8, label: "NEWS" },
  { id: "cipher", n: 9, label: "CIPHER" },
  { id: "practice", n: 10, label: "PRACTICE" },
  { id: "learn", n: 11, label: "LEARN" },
] as const;

export const INTEL = [
  {
    tag: "BEARISH" as const,
    title: "Bitcoin Enters Death Cross as Anthropic IPO Fails to Lift Wall…",
    source: "Decrypt",
    ago: "1h",
  },
  {
    tag: "BULLISH" as const,
    title: "Binance wins $6.5 billion freezing order against hackers behind…",
    source: "CoinDesk",
    ago: "2h",
  },
  {
    tag: "NEUTRAL" as const,
    title: "Binance Wins $6.5B Freezing Order Over Hack That Can Be…",
    source: "Cointelegraph",
    ago: "3h",
  },
  {
    tag: "BEARISH" as const,
    title: "Trump Media wants to clear $8B+ million of debt without selling it…",
    source: "The Block",
    ago: "4h",
  },
  {
    tag: "BEARISH" as const,
    title: "Bitcoin Struggles to Hold $64,000 as ETF Outflows Hit $2.9B in…",
    source: "Decrypt",
    ago: "5h",
  },
  {
    tag: "BEARISH" as const,
    title: "Bitcoin ETF assets fall to three-month low…",
    source: "CoinDesk",
    ago: "6h",
  },
  {
    tag: "BULLISH" as const,
    title: "Binance gets $6.5 billion freezing order…",
    source: "Cointelegraph",
    ago: "7h",
  },
];

export const WATCHLIST = [
  { symbol: "BTC / USDT", price: "62,093.3", change: -0.42 },
  { symbol: "ETH / USDT", price: "1,757.53", change: -1.31 },
  { symbol: "SOL / USDT", price: "73.12", change: -0.55 },
  { symbol: "BNB / USDT", price: "581.93", change: -0.81 },
  { symbol: "XRP / USDT", price: "0.9617", change: -0.78 },
];

export const FUNDING = [
  { symbol: "BTC", rate: "+0.0100%" },
  { symbol: "ETH", rate: "+0.0027%" },
  { symbol: "SOL", rate: "+0.0059%" },
  { symbol: "BNB", rate: "+0.0000%" },
  { symbol: "XRP", rate: "+0.0028%" },
];

export const SIGNAL_WATCH = [
  { symbol: "BTC", agree: 100, side: "SHORT" as const },
  { symbol: "ETH", agree: 83, side: "SHORT" as const },
  { symbol: "SOL", agree: 100, side: "SHORT" as const },
  { symbol: "BNB", agree: 83, side: "SHORT" as const },
];

export const ENGINE_METRICS = [
  { k: "TREND STRUCTURE", v: "BEAR" },
  { k: "TREND PERSISTENCE", v: "BEAR" },
  { k: "MOMENTUM", v: "BEAR" },
  { k: "MOMENTUM PERSIST", v: "BEAR" },
  { k: "FLOW 1M", v: "BEAR" },
  { k: "FLOW CONFIRM", v: "BEAR" },
  { k: "BOOK BALANCE", v: "NEUT" },
  { k: "LIQUIDITY QUALITY", v: "NEUT" },
  { k: "FUNDING / OI", v: "BEAR" },
  { k: "OI / CROWD", v: "BEAR" },
];

export const LEVELS = [
  { label: "SL", price: 62799.3, color: "#f06b78" },
  { label: "ENT", price: 62102.9, color: "#eec36d" },
  { label: "TP1", price: 62027.9, color: "#30d69a" },
  { label: "TP2", price: 61977.9, color: "#30d69a" },
  { label: "TP3", price: 61902.9, color: "#30d69a" },
];

export const LIVE_TRADES = [
  { t: "20:12:38", side: "BUY" as const, price: "62,093.3", size: "0.071", value: "$4.41K" },
  { t: "20:12:37", side: "BUY" as const, price: "62,093.2", size: "0.001", value: "$62" },
  { t: "20:12:36", side: "BUY" as const, price: "62,093.1", size: "0.036", value: "$2.24K" },
  { t: "20:12:35", side: "BUY" as const, price: "62,093.0", size: "0.005", value: "$310" },
  { t: "20:12:34", side: "BUY" as const, price: "62,092.8", size: "0.24", value: "$14.9K" },
  { t: "20:12:33", side: "BUY" as const, price: "62,092.6", size: "0.072", value: "$4.47K" },
  { t: "20:12:32", side: "BUY" as const, price: "62,092.4", size: "0.073", value: "$4.53K" },
  { t: "20:12:31", side: "SELL" as const, price: "62,093.5", size: "0.018", value: "$1.12K" },
  { t: "20:12:30", side: "BUY" as const, price: "62,093.0", size: "0.009", value: "$559" },
];

export const WHALES = [
  { side: "SELL" as const, price: "62,113.3", value: "$621.1K", ago: "17s ago" },
  { side: "BUY" as const, price: "62,129.2", value: "$386K", ago: "30s ago" },
  { side: "SELL" as const, price: "62,065.6", value: "$267.8K", ago: "50s ago" },
  { side: "BUY" as const, price: "62,090.2", value: "$459.7K", ago: "1m ago" },
  { side: "SELL" as const, price: "62,099.7", value: "$312.4K", ago: "2m ago" },
  { side: "BUY" as const, price: "62,085.1", value: "$198.0K", ago: "2m ago" },
];