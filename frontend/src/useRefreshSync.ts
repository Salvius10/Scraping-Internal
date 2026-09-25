import { useEffect, useRef } from "react";
import { api, type Status } from "./api";

// How often a page checks whether the scheduler has refreshed the feed.
// /api/status is a database read: free, no model call.
const SYNC_MS = 60 * 1000;

/** Call `onRefresh` whenever the 12-hour scheduler (or a manual run) has
 *  brought in new stories since this page loaded.
 *
 *  Checks once a minute, and again the moment the tab comes back into view,
 *  never while it is hidden. Shared by every page that shows scheduled data,
 *  so they all stay in step the same way.
 */
export function useRefreshSync(onRefresh: (next: Status, prev: Status) => void) {
  const last = useRef<Status | null>(null);
  const callback = useRef(onRefresh);
  callback.current = onRefresh;

  useEffect(() => {
    let stopped = false;

    const check = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const next = await api.status();
        if (stopped) return;
        const prev = last.current;
        last.current = next;
        if (prev && next.last_refresh !== prev.last_refresh) callback.current(next, prev);
      } catch {
        /* the next tick tries again */
      }
    };

    void check();   // the baseline to compare against
    const timer = window.setInterval(check, SYNC_MS);
    document.addEventListener("visibilitychange", check);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", check);
    };
  }, []);
}
