import { Search, User } from "lucide-react";

export function GlobalHeader({ query, onQuery }: { query: string; onQuery: (v: string) => void }) {
  // Demo clock locked to reference screenshot time (not live exchange data).
  const utc = "20:12:43";

  return (
    <header className="global-header">
      <div className="gh-brand">
        <span className="gh-p">P</span>
        <strong>PerpSignal Desk</strong>
        <span className="gh-btc">BTC</span>
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
        <span className="num">{utc} UTC</span>
        <span className="gh-plat">PLATINUM</span>
        <button type="button" className="gh-acct">
          <User size={11} strokeWidth={1.75} />
          Account
        </button>
      </div>
    </header>
  );
}