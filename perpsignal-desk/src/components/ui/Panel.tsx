import type { ReactNode } from "react";
import { LiveBadge } from "./LiveBadge";

export function Panel({
  title,
  live,
  right,
  children,
  className = "",
}: {
  title: string;
  live?: boolean;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-header">
        <span>{title}</span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {right}
          {live ? <LiveBadge /> : null}
        </span>
      </header>
      <div className="panel-body scroll">{children}</div>
    </section>
  );
}