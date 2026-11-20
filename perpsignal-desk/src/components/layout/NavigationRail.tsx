import {
  BookOpen,
  Boxes,
  Droplets,
  FlaskConical,
  Globe2,
  LayoutGrid,
  Newspaper,
  Radar,
  Shield,
  Terminal,
  Waves,
  UserRound,
} from "lucide-react";
import { NAV_ITEMS } from "../../data/mockMarketData";

const ICONS = [
  LayoutGrid,
  Terminal,
  Globe2,
  Waves,
  Boxes,
  Droplets,
  Radar,
  Newspaper,
  Shield,
  FlaskConical,
  BookOpen,
];

export function NavigationRail({
  active,
  onSelect,
}: {
  active: string;
  onSelect: (id: string) => void;
}) {
  return (
    <nav className="nav-rail">
      <div className="nav-rail-list">
        {NAV_ITEMS.map((item, i) => {
          const Icon = ICONS[i];
          const on = active === item.id;
          return (
            <button
              key={item.id}
              type="button"
              className={`nav-rail-btn ${on ? "on" : ""}`}
              onClick={() => onSelect(item.id)}
              title={item.label}
            >
              <span className="nav-n">{item.n}</span>
              <Icon size={12} strokeWidth={1.7} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </div>
      <button type="button" className="nav-rail-btn you">
        <UserRound size={12} strokeWidth={1.7} />
        <span>YOU</span>
      </button>
    </nav>
  );
}