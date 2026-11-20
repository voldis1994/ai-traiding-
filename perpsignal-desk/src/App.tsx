import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { CryptoTicker } from "./components/layout/CryptoTicker";
import { GlobalHeader } from "./components/layout/GlobalHeader";
import { MarketIntel } from "./components/layout/MarketIntel";
import { NavigationRail } from "./components/layout/NavigationRail";
import { SignalWatchSidebar } from "./components/layout/SignalWatchSidebar";
import { WorkspaceToolbar } from "./components/layout/WorkspaceToolbar";
import { BTCChart } from "./components/trading/BTCChart";
import { IndicatorsPanel } from "./components/trading/IndicatorsPanel";
import { LiveTrades } from "./components/trading/LiveTrades";
import { OrderBook } from "./components/trading/OrderBook";
import { PerpSignalEngine } from "./components/trading/PerpSignalEngine";
import { Watchlist } from "./components/trading/Watchlist";
import { WhaleAlerts } from "./components/trading/WhaleAlerts";
import { useLiveDesk } from "./live/LiveDeskContext";

type Mode = "DEFAULT" | "SIGNAL DESK" | "SCALPER" | "SWING" | "PRACTICE";
type Tf = "1M" | "5M" | "15M" | "1H" | "4H";

const DEFAULTS = {
  nav: "terminal",
  mode: "SIGNAL DESK" as Mode,
  tf: "15M" as Tf,
  query: "",
  watch: "BTC / USDT",
};

export default function App() {
  const { live } = useLiveDesk();
  const [nav, setNav] = useState(DEFAULTS.nav);
  const [mode, setMode] = useState<Mode>(DEFAULTS.mode);
  const [tf, setTf] = useState<Tf>(DEFAULTS.tf);
  const [query, setQuery] = useState(DEFAULTS.query);
  const [watch, setWatch] = useState(DEFAULTS.watch);
  const [scale, setScale] = useState(1);

  useEffect(() => {
    const fit = () => {
      const sx = window.innerWidth / 1290;
      const sy = window.innerHeight / 515;
      setScale(Math.min(1, sx, sy));
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, []);

  useEffect(() => {
    if (live.watchlist[0]?.symbol) setWatch(live.watchlist[0].symbol);
  }, [live.watchlist]);

  const frameStyle = useMemo(
    () => ({ transform: `scale(${scale})` }) as CSSProperties,
    [scale],
  );

  const reset = () => {
    setNav(DEFAULTS.nav);
    setMode(DEFAULTS.mode);
    setTf(DEFAULTS.tf);
    setQuery(DEFAULTS.query);
    setWatch(live.watchlist[0]?.symbol || DEFAULTS.watch);
  };

  return (
    <div className="desk-stage">
      {!live.auth && (
        <div className="desk-auth-banner">
          <strong>{live.mode === "demo" ? "DEMO / OFFLINE" : "LIVE GATE"}</strong>
          <span>{live.message}</span>
          <a href="/admin">Open Admin login →</a>
          <span className="desk-auth-hint">
            Pilnā sistēma: <code>PALAIST_PILNO_SISTEMU.bat</code> → http://127.0.0.1:8765/admin (nevis :5173)
          </span>
        </div>
      )}
      {live.auth && (
        <div className={`desk-live-banner ${live.robotOn ? "on" : ""}`}>
          <span className="live-badge">LIVE</span>
          <span>
            {live.message} · {live.clientLabel} · {live.epic}
          </span>
        </div>
      )}
      <div className="desk-frame" style={frameStyle} data-nav={nav}>
        <GlobalHeader query={query} onQuery={setQuery} utc={live.utc} liveMode={live.auth} />
        <CryptoTicker filter={query} items={live.ticker} />
        <div className="desk-workspace">
          <NavigationRail active={nav} onSelect={setNav} />
          <MarketIntel items={live.intel} />
          <div className="desk-center">
            <WorkspaceToolbar
              mode={mode}
              onMode={setMode}
              onReset={reset}
              epic={live.epic}
              last={live.lastPrice}
            />
            <div className="desk-center-top">
              <BTCChart
                tf={tf}
                onTf={setTf}
                candles={live.candles}
                levels={live.levels}
                epic={live.epic}
              />
              <OrderBook
                asks={live.asks}
                bids={live.bids}
                spread={live.spread}
                ratio={live.bookRatio}
                epic={live.epic}
              />
              <PerpSignalEngine engine={live.engine} />
            </div>
            <div className="desk-center-bottom">
              <IndicatorsPanel rsi={live.rsi} macd={live.macd} candles={live.candles} />
              <LiveTrades trades={live.trades} epic={live.epic} />
              <Watchlist selected={watch} onSelect={setWatch} rows={live.watchlist} />
              <WhaleAlerts whales={live.whales} />
            </div>
          </div>
          <SignalWatchSidebar
            funding={live.funding}
            crowd={live.crowd}
            fear={live.fear}
            signals={live.signals}
          />
        </div>
      </div>
    </div>
  );
}
