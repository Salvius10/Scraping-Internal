import { useCallback, useEffect, useRef, useState } from "react";
import { api, parseTime, type RefreshStatus } from "./api";

// How often a running refresh is checked on. A read of server memory: free.
const POLL_MS = 3000;

// Phosphor Icons (MIT), regular weight: "arrows-clockwise".
const ARROWS =
  "M224,48V96a8,8,0,0,1-8,8H168a8,8,0,0,1,0-16h28.69L182.06,73.37a79.56,79.56,0,0,0-56.13-23.43h-.45A79.52,79.52,0,0,0,69.59,72.71,8,8,0,0,1,58.41,61.27a96,96,0,0,1,135,.79L208,76.69V48a8,8,0,0,1,16,0ZM186.41,183.29a80,80,0,0,1-112.47-.66L59.31,168H88a8,8,0,0,0,0-16H40a8,8,0,0,0-8,8v48a8,8,0,0,0,16,0V179.31l14.63,14.63A95.43,95.43,0,0,0,130,222.06h.53a95.36,95.36,0,0,0,67.07-27.33,8,8,0,0,0-11.18-11.44Z";

const TITLE =
  "Run the pipeline now instead of waiting for the 12-hour schedule: every source that is "
  + "due is read, checked, de-duplicated and structured. Paid sources read in the last day "
  + "are skipped. Usually a few minutes.";

function elapsed(since: string | null): string {
  if (!since) return "";
  const s = Math.max(0, Math.round((Date.now() - parseTime(since).getTime()) / 1000));
  return ` ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Manual Refresh: starts a run on the server, follows it, and hands the
 *  outcome to the page. One run at a time across every tab; if one is
 *  already running when the page opens, the button follows it too. */
export default function RefreshButton({
  onDone, onMessage,
}: {
  onDone: () => void;                         // reload the page's data
  onMessage: (text: string) => void;          // outcome or refusal
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

  useEffect(() => {
    api.refreshStatus().then((s) => {
      setRun(s);
      if (s.running) void follow();
    }).catch(() => undefined);
  }, [follow]);

  useEffect(() => {
    if (!run?.running) return;
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [run?.running]);

  const start = async () => {
    try {
      const answer = await api.refresh();
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
  return (
    <button
      className="btn btn--on-brand"
      onClick={() => void start()}
      disabled={running}
      aria-busy={running}
      title={TITLE}
    >
      <svg className={running ? "spin" : undefined} viewBox="0 0 256 256" width="15" height="15"
           fill="currentColor" aria-hidden="true">
        <path d={ARROWS} />
      </svg>
      {running ? `Refreshing${elapsed(run?.started_at ?? null)}` : "Refresh now"}
    </button>
  );
}
