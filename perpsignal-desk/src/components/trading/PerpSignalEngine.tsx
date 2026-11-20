import { useState } from "react";
import { ENGINE_METRICS } from "../../data/mockMarketData";
import { LiveBadge } from "../ui/LiveBadge";

type Engine = {
  side: "SHORT" | "LONG" | "HOLD";
  regime: string;
  ring: number;
  strength: number;
  coverage: number;
  scorePos: number;
  metrics: Array<{ k: string; v: string }>;
};

export function PerpSignalEngine({ engine }: { engine?: Engine }) {
  const [core, setCore] = useState<"classic" | "engine">("classic");
  const e = engine || {
    side: "SHORT" as const,
    regime: "BEAR REGIME",
    ring: 73,
    strength: 94,
    coverage: 86,
    scorePos: 14,
    metrics: [...ENGINE_METRICS],
  };
  return (
    <section className="panel engine">
      <header className="panel-header">
        <span>PERPSIGNAL ENGINE</span>
        <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
          <button
            type="button"
            className={`mini-tab ${core === "classic" ? "on" : ""}`}
            onClick={() => setCore("classic")}
          >
            Classic Core
          </button>
          <button
            type="button"
            className={`mini-tab ${core === "engine" ? "on" : ""}`}
            onClick={() => setCore("engine")}
          >
            ENGINE CORE
          </button>
          <LiveBadge />
        </span>
      </header>
      <div className="panel-body eng-body">
        <div className="eng-top">
          <div className="eng-ring" style={{ ["--p" as string]: e.ring }}>
            <div className="eng-ring-core">
              <b>{e.ring}%</b>
            </div>
          </div>
          <div className="eng-signal">
            <div className={`eng-short ${e.side === "LONG" ? "up" : ""}`}>{e.side}</div>
            <div className="eng-regime">{e.regime}</div>
            <div className="eng-meta">
              strength {e.strength}/100 · data coverage {e.coverage}%
            </div>
            <div className="eng-note">
              Ring = how aligned the inputs are. Not a probability, win rate or forecast.
            </div>
          </div>
        </div>
        <div className="eng-score">
          <span className="dn">BEAR</span>
          <div className="eng-score-track">
            <i style={{ left: `${Math.max(4, Math.min(96, e.scorePos))}%` }} />
          </div>
          <span className="up">BULL</span>
        </div>
        <div className="eng-score-lbl">ENGINE SCORE</div>
        <div className="eng-grid">
          {e.metrics.map((m) => (
            <div key={m.k} className="eng-metric">
              <span>{m.k}</span>
              <b className={m.v === "BEAR" ? "dn" : m.v === "BULL" ? "up" : "neu"}>
                {m.v}
                <em>{m.v}</em>
              </b>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
