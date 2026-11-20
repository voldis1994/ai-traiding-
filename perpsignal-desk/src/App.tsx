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
      // Prefer exact 1:1 when viewport is at least reference size; otherwise scale down.
      setScale(Math.min(1, sx, sy));
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, []);

  const frameStyle = useMemo(
    () => ({ transform: `scale(${scale})` }) as CSSProperties,
    [scale],
  );

  const reset = () => {
    setNav(DEFAULTS.nav);
    setMode(DEFAULTS.mode);
    setTf(DEFAULTS.tf);
    setQuery(DEFAULTS.query);
    setWatch(DEFAULTS.watch);
  };

  return (
    <div className="desk-stage">
      <div className="desk-frame" style={frameStyle} data-nav={nav}>
        <GlobalHeader query={query} onQuery={setQuery} />
        <CryptoTicker filter={query} />
        <div className="desk-workspace">
          <NavigationRail active={nav} onSelect={setNav} />
          <MarketIntel />
          <div className="desk-center">
            <WorkspaceToolbar mode={mode} onMode={setMode} onReset={reset} />
            <div className="desk-center-top">
              <BTCChart tf={tf} onTf={setTf} />
              <OrderBook />
              <PerpSignalEngine />
            </div>
            <div className="desk-center-bottom">
              <IndicatorsPanel />
              <LiveTrades />
              <Watchlist selected={watch} onSelect={setWatch} />
              <WhaleAlerts />
            </div>
          </div>
          <SignalWatchSidebar />
        </div>
      </div>
    </div>
  );
}