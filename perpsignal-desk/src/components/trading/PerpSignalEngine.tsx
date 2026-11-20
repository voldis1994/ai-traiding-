import { useState } from "react";
import { ENGINE_METRICS } from "../../data/mockMarketData";
import { LiveBadge } from "../ui/LiveBadge";

export function PerpSignalEngine() {
  const [core, setCore] = useState<"classic" | "engine">("classic");
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
          <div className="eng-ring" style={{ ["--p" as string]: 73 }}>
            <div className="eng-ring-core">
              <b>73%</b>
            </div>
          </div>
          <div className="eng-signal">
            <div className="eng-short">SHORT</div>
            <div className="eng-regime">BEAR REGIME</div>
            <div className="eng-meta">strength 94/100 · data coverage 86%</div>
            <div className="eng-note">
              Ring = how aligned the inputs are. Not a probability, win rate or forecast.
            </div>
          </div>
        </div>
        <div className="eng-score">
          <span className="dn">BEAR</span>
          <div className="eng-score-track">
            <i style={{ left: "14%" }} />
          </div>
          <span className="up">BULL</span>
        </div>
        <div className="eng-score-lbl">ENGINE SCORE</div>
        <div className="eng-grid">
          {ENGINE_METRICS.map((m) => (
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
