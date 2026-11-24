import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { emptyLive, fetchLiveDesk, POLL_MS, type LiveDeskSnapshot } from "../api/adminLive";

type Ctx = {
  live: LiveDeskSnapshot;
  refresh: () => Promise<void>;
};

const LiveDeskContext = createContext<Ctx>({
  live: emptyLive("boot"),
  refresh: async () => undefined,
});

export function LiveDeskProvider({ children }: { children: ReactNode }) {
  const [live, setLive] = useState<LiveDeskSnapshot>(() => emptyLive("Connecting…"));

  const refresh = async () => {
    const snap = await fetchLiveDesk();
    setLive(snap);
  };

  useEffect(() => {
    let dead = false;
    let inFlight: Promise<void> | null = null;
    const tick = async () => {
      // Coalesce — a slow /overview must not stack and freeze the Desk.
      if (inFlight) return inFlight;
      inFlight = (async () => {
        try {
          const snap = await fetchLiveDesk();
          if (!dead) setLive(snap);
        } catch {
          /* keep last snap */
        } finally {
          inFlight = null;
        }
      })();
      return inFlight;
    };
    tick();
    const id = setInterval(() => {
      tick();
    }, POLL_MS);
    return () => {
      dead = true;
      clearInterval(id);
    };
  }, []);

  return <LiveDeskContext.Provider value={{ live, refresh }}>{children}</LiveDeskContext.Provider>;
}

export function useLiveDesk() {
  return useContext(LiveDeskContext);
}
