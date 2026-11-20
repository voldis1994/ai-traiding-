type Kind = "BULLISH" | "BEARISH" | "NEUTRAL" | "BULL" | "BEAR" | "NEUT";

export function StatusBadge({ kind }: { kind: Kind }) {
  const cls =
    kind === "BULLISH" || kind === "BULL"
      ? "bull"
      : kind === "BEARISH" || kind === "BEAR"
        ? "bear"
        : "neu";
  return <span className={`status-badge ${cls}`}>{kind}</span>;
}