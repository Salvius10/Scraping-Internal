import { useCallback, useEffect, useRef, useState } from "react";
import { api, parseTime, type RefreshScope, type RefreshStatus } from "./api";

// How often a running refresh is checked on. A database read in memory: free.
const POLL_MS = 3000;

// Phosphor Icons (MIT), regular weight: "arrows-clockwise".
const ARROWS =
  "M224,48V96a8,8,0,0,1-8,8H168a8,8,0,0,1,0-16h28.69L182.06,73.37a79.56,79.56,0,0,0-56.13-23.43h-.45A79.52,79.52,0,0,0,69.59,72.71,8,8,0,0,1,58.41,61.27a96,96,0,0,1,135,.79L208,76.69V48a8,8,0,0,1,16,0ZM186.41,183.29a80,80,0,0,1-112.47-.66L59.31,168H88a8,8,0,0,0,0-16H40a8,8,0,0,0-8,8v48a8,8,0,0,0,16,0V179.31l14.63,14.63A95.43,95.43,0,0,0,130,222.06h.53a95.36,95.36,0,0,0,67.07-27.33,8,8,0,0,0-11.18-11.44Z";

const SCOPE_TEXT: Record<RefreshScope, { idle: string; busy: string; title: string }> = {
  feed: {
    idle: "Refresh now",
    busy: "Refreshing",
    title: "Fetch the news sources now, then classify new stories and read funding "
      + "rounds and VC firm news. Usually a few minutes; costs a fraction of a cent.",
  },
  vcs: {
    idle: "Refresh now",
    busy: "Refreshing",
    title: "Read every VC firm's website news now. Firecrawl-read sites are read at most "
      + "once a day, so those that ran recently are skipped.",
  },
  events: {
    idle: "Refresh now",
    busy: "Refreshing",
    title: "Read the event sources that are due now. Each is read at most once a day "
      + "(Apify for Luma calendars, Firecrawl for websites), so recent ones are skipped.",
  },
};

function elapsed(since: string | null): string {
  if (!since) return "";
  const s = Math.max(0, Math.round((Date.now() - parseTime(since).getTime()) / 1000));
  return ` ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** "Refresh now": starts a refresh on the server instead of waiting for the
 *  12-hour scheduler, follows it, and hands the outcome to the page.
 *
 *  Only one refresh runs at a time across every page. If one is already
 *  running when the page opens -- started here or elsewhere -- the button
 *  shows it and follows it too.
 */
export default function RefreshButton({
  scope, onDone, onMessage, tone = "light",
}: {
  scope: RefreshScope;
  onDone: () => void;                         // reload the page's data
  onMessage: (text: string) => void;          // outcome or refusal, for the page's note
  tone?: "light" | "dark";
}) {
  const [run, setRun] = useState<RefreshStatus | null>(null);
  const [, tick] = useState(0);
  const following = useRef(false);
  const callbacks = useRef({ onDone, onMessage });
  callbacks.current = { onDone, onMessage };

  const follow = useCallback(async () => {
    if (following.current) return;
    following.current = true;
    try {
      for (;;) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        const next = await api.refreshStatus();
        setRun(next);
        if (!next.running) {
          callbacks.current.onDone();
          callbacks.current.onMessage(next.error ?? next.summary ?? "Refresh finished.");
          return;
        }
      }
    } catch {
      callbacks.current.onMessage("Lost touch with the server during the refresh.");
    } finally {
      following.current = false;
    }
  }, []);

  // A refresh may already be running (another page, another tab).
  useEffect(() => {
    api.refreshStatus().then((s) => {
      setRun(s);
      if (s.running) void follow();
    }).catch(() => undefined);
  }, [follow]);

  // Keep the elapsed time moving while running.
  useEffect(() => {
    if (!run?.running) return;
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [run?.running]);

  const start = async () => {
    try {
      const answer = await api.refresh(scope);
      setRun(answer.status);
      if (!answer.started) {
        callbacks.current.onMessage(answer.reason ?? "Could not start a refresh.");
        if (answer.status.running) void follow();
        return;
      }
      void follow();
    } catch {
      callbacks.current.onMessage("Could not reach the server to start a refresh.");
    }
  };

  const running = Boolean(run?.running);
  const text = SCOPE_TEXT[scope];
  const otherScope = running && run?.scope !== scope;

  return (
    <button
      className={`refresh-btn refresh-btn--${tone}`}
      onClick={() => void start()}
      disabled={running}
      aria-busy={running}
      aria-label={running ? text.busy : text.idle}
      title={otherScope ? "Another refresh is running; this page updates when it is done." : text.title}
    >
      <svg
        className={running ? "refresh-spin" : undefined}
        viewBox="0 0 256 256" width="15" height="15" fill="currentColor" aria-hidden="true"
      >
        <path d={ARROWS} />
      </svg>
      <span className="refresh-label">
        {running ? `${text.busy}${elapsed(run?.started_at ?? null)}` : text.idle}
      </span>
    </button>
  );
}
