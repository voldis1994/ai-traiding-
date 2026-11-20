const MODES = ["DEFAULT", "SIGNAL DESK", "SCALPER", "SWING", "PRACTICE"] as const;

export function WorkspaceToolbar({
  mode,
  onMode,
  onReset,
}: {
  mode: (typeof MODES)[number];
  onMode: (m: (typeof MODES)[number]) => void;
  onReset: () => void;
}) {
  return (
    <div className="ws-toolbar">
      <div className="ws-epic">
        <strong>BTC</strong>
        <span>PERPETUAL</span>
        <span>LAST</span>
        <b className="dn num">62,093.3</b>
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