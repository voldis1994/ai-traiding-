export type BookRow = { price: number; size: number; value: string; total: number; depth: number };

function fmtValue(price: number, size: number): string {
  const v = price * size;
  if (v >= 1000) return `$${(v / 1000).toFixed(1)}K`;
  return `$${Math.round(v)}`;
}

/** Ask side — highest price at top (matches reference). */
export const ORDER_ASKS: BookRow[] = [
  { price: 62095.0, size: 0.48, value: "", total: 0.48, depth: 28 },
  { price: 62094.9, size: 0.31, value: "", total: 0.79, depth: 22 },
  { price: 62094.6, size: 0.14, value: "", total: 0.93, depth: 16 },
  { price: 62094.5, size: 0.22, value: "", total: 1.15, depth: 20 },
  { price: 62094.4, size: 0.09, value: "", total: 1.24, depth: 12 },
  { price: 62093.9, size: 0.55, value: "", total: 1.79, depth: 34 },
  { price: 62093.8, size: 0.18, value: "", total: 1.97, depth: 18 },
  { price: 62093.7, size: 0.27, value: "", total: 2.24, depth: 24 },
  { price: 62093.4, size: 0.41, value: "", total: 2.65, depth: 30 },
  { price: 62093.3, size: 4.24, value: "", total: 6.89, depth: 92 },
].map((r) => ({ ...r, value: fmtValue(r.price, r.size) }));

/** Bid side — closest to mid at top. */
export const ORDER_BIDS: BookRow[] = [
  { price: 62093.2, size: 3.42, value: "", total: 3.42, depth: 88 },
  { price: 62093.1, size: 0.62, value: "", total: 4.04, depth: 36 },
  { price: 62092.9, size: 0.28, value: "", total: 4.32, depth: 22 },
  { price: 62092.8, size: 0.19, value: "", total: 4.51, depth: 18 },
  { price: 62092.6, size: 0.44, value: "", total: 4.95, depth: 30 },
  { price: 62092.3, size: 0.15, value: "", total: 5.1, depth: 14 },
  { price: 62092.2, size: 0.33, value: "", total: 5.43, depth: 26 },
  { price: 62092.0, size: 0.21, value: "", total: 5.64, depth: 20 },
  { price: 62091.9, size: 0.52, value: "", total: 6.16, depth: 34 },
  { price: 62091.6, size: 0.38, value: "", total: 6.54, depth: 28 },
].map((r) => ({ ...r, value: fmtValue(r.price, r.size) }));

export const SPREAD = { abs: 0.1, bps: 0.0 };
export const BOOK_RATIO = { bid: 53.5, ask: 46.5 };
