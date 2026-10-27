import { useCallback, useEffect, useState } from "react";
import { api, parseTime, type Status } from "./api";
import { describeAge, timeFmt } from "./dates";
import Enrichment from "./Enrichment";
import Events from "./Events";
import Funding from "./Funding";
import RefreshButton from "./RefreshButton";
import { useRefreshSync } from "./useRefreshSync";

type View = "funding" | "events" | "enrichment";

function viewFromHash(): View {
  if (window.location.hash === "#events") return "events";
  if (window.location.hash === "#enrichment") return "enrichment";
  return "funding";
}

function Masthead({ status, view, onView, onRefreshed, onMessage }: {
  status: Status | null;
  view: View;
  onView: (v: View) => void;
  onRefreshed: () => void;
  onMessage: (text: string) => void;
}) {
  const stale = status?.hours_since_refresh != null
    && status.hours_since_refresh > status.refresh_interval_hours;
  const next = status?.next_refresh
    ? `Next refresh ${timeFmt.format(parseTime(status.next_refresh))} IST` : undefined;
  return (
    <header className="masthead">
      <div className="masthead-inner">
        <h1 className="wordmark">
          <span className="brandmark" aria-hidden="true">G</span>
          GPS <span className="wordmark-sub">Ganit Pursuit OS</span>
        </h1>
        <nav className="views" aria-label="Feeds">
          <a href="#funding" aria-current={view === "funding" ? "page" : undefined}
             onClick={() => onView("funding")}>
            Funded startups
            {status && <span className="count">{status.funding_count}</span>}
          </a>
          <a href="#events" aria-current={view === "events" ? "page" : undefined}
             onClick={() => onView("events")}>
            Events in India
            {status && <span className="count">{status.upcoming_events}</span>}
          </a>
          <a href="#enrichment" aria-current={view === "enrichment" ? "page" : undefined}
             onClick={() => onView("enrichment")}>
            Enrichment
            {status && <span className="count">{status.enriched_contacts}</span>}
          </a>
        </nav>
        <div className="masthead-right">
          <span className={`freshness${stale ? " freshness--stale" : ""}`} title={next}>
            {status ? describeAge(status.hours_since_refresh) : " "}
          </span>
          <RefreshButton onDone={onRefreshed} onMessage={onMessage} />
        </div>
      </div>
    </header>
  );
}

/** Pipeline health under the feeds: what a reader needs to trust what they see. */
function Health({ status }: { status: Status | null }) {
  if (!status) return null;
  const failing = status.sources.filter((s) => s.last_error);
  const spend = status.spend;
  return (
    <footer className="health">
      <p>
        <strong>Guardrails:</strong> {status.guardrails}
        {" · "}<strong>LLM budget:</strong> ${spend.total_usd.toFixed(4)} of ${spend.total_cap.toFixed(2)}
        {spend.exhausted && <span className="warn"> (cap reached: new items wait)</span>}
        {status.pending > 0 && <>{" · "}{status.pending} items waiting for the next run</>}
        {status.blocked > 0 && <>{" · "}{status.blocked} blocked or rejected</>}
      </p>
      {status.registry_error && (
        <p className="warn">The source registry is broken: {status.registry_error}</p>
      )}
      {failing.length > 0 && (
        <details>
          <summary className="warn">
            {failing.length} {failing.length === 1 ? "source" : "sources"} failed on the last read
          </summary>
          <ul>
            {failing.map((s) => <li key={s.key}><strong>{s.label}:</strong> {s.last_error}</li>)}
          </ul>
        </details>
      )}
    </footer>
  );
}

export default function App() {
  const [view, setView] = useState<View>(viewFromHash);
  const [status, setStatus] = useState<Status | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [note, setNote] = useState<string | null>(null);

  const loadStatus = useCallback(() => {
    api.status().then(setStatus).catch(() => undefined);
  }, []);

  useEffect(() => { loadStatus(); }, [loadStatus]);
  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const reload = useCallback(() => {
    setReloadKey((n) => n + 1);
    loadStatus();
  }, [loadStatus]);

  // "Send data for enrichment" went through: show where the contacts arrive.
  const openEnrichment = useCallback((message: string | null) => {
    setNote(message);
    window.location.hash = "#enrichment";
    setView("enrichment");
    window.scrollTo({ top: 0 });
    loadStatus();
  }, [loadStatus]);

  // A scheduled run elsewhere finished: bring both feeds up to date.
  useRefreshSync((next) => {
    setStatus(next);
    setReloadKey((n) => n + 1);
    setNote("Updated with the latest run.");
  });

  return (
    <>
      <a className="skip-link" href="#main">Skip to the feed</a>
      <Masthead status={status} view={view} onView={setView} onRefreshed={reload}
                onMessage={setNote} />
      <main id="main" className="page">
        {note && (
          <p className="note" role="status">
            {note}
            <button onClick={() => setNote(null)}>Dismiss</button>
          </p>
        )}
        {view === "funding" && <Funding reloadKey={reloadKey} onEnrichment={openEnrichment} />}
        {view === "events" && <Events reloadKey={reloadKey} />}
        {view === "enrichment" && (
          <Enrichment reloadKey={reloadKey} onChanged={loadStatus} onMessage={setNote} />
        )}
        <Health status={status} />
      </main>
    </>
  );
}
