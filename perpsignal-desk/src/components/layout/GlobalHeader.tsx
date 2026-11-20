import { Search, User } from "lucide-react";

export function GlobalHeader({
  query,
  onQuery,
  utc,
  liveMode,
}: {
  query: string;
  onQuery: (v: string) => void;
  utc?: string;
  liveMode?: boolean;
}) {
  return (
    <header className="global-header">
      <div className="gh-brand">
        <span className="gh-p">P</span>
        <strong>PerpSignal Desk</strong>
        <span className="gh-btc">{liveMode ? "LIVE" : "DEMO"}</span>
      </div>
      <label className="gh-search">
        <Search size={11} strokeWidth={1.75} />
        <input
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="Bitcoin  Search markets, views, commands…"
        />
        <kbd>⌘K</kbd>
      </label>
      <div className="gh-right">
        <span className="num">{utc || "20:12:43"} UTC</span>
        <span className="gh-plat">PLATINUM</span>
        <a className="gh-acct" href="/admin">
          <User size={11} strokeWidth={1.75} />
          Account
        </a>
      </div>
    </header>
  );
}
