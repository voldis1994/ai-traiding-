const MODES = ["DEFAULT", "SIGNAL DESK", "SCALPER", "SWING", "PRACTICE"] as const;

export function WorkspaceToolbar({
  mode,
  onMode,
  onReset,
  epic,
  last,
}: {
  mode: (typeof MODES)[number];
  onMode: (m: (typeof MODES)[number]) => void;
  onReset: () => void;
  epic?: string;
  last?: number | null;
}) {
  const label = (epic || "BTC").split(/[./]/)[0];
  const lastTxt =
    last != null && Number.isFinite(last)
      ? last.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 })
      : "62,093.3";
  return (
    <div className="ws-toolbar">
      <div className="ws-epic">
        <strong>{label}</strong>
        <span>PERPETUAL</span>
        <span>LAST</span>
        <b className="dn num">{lastTxt}</b>
      </div>
      <div className="ws-modes">
        {MODES.map((m) => (
          <button
            key={m}
            type="button"
            className={`ws-mode ${mode === m ? "on" : ""}`}
            onClick={() => onMode(m)}
          >
            {m}
          </button>
        ))}
        <button type="button" className="ws-mode">
          ● WIDGET
        </button>
        <button type="button" className="ws-mode" onClick={onReset}>
          RESET
        </button>
      </div>
    </div>
  );
}
