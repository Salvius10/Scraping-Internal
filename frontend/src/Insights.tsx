import { useCallback, useEffect, useState } from "react";
import CopyLink from "./CopyLink";
import {
  api, parseTime, type DateRange, type RoundsResponse, type SearchWebResponse, type StageKey,
} from "./api";
import { useRefreshSync } from "./useRefreshSync";

const publishedFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", month: "short", year: "numeric",
  hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata",
});

// Phosphor Icons (MIT), regular weight: "download-simple".
const DOWNLOAD =
  "M224,144v64a8,8,0,0,1-8,8H40a8,8,0,0,1-8-8V144a8,8,0,0,1,16,0v56H208V144a8,8,0,0,1,16,0Zm-101.66,5.66a8,8,0,0,0,11.32,0l40-40a8,8,0,0,0-11.32-11.32L136,124.69V32a8,8,0,0,0-16,0v92.69L93.66,98.34a8,8,0,0,0-11.32,11.32Z";

/** Today in India as YYYY-MM-DD, optionally some days back. */
function istDay(daysAgo = 0): string {
  const at = new Date(Date.now() - daysAgo * 86_400_000);
  return at.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

const PRESETS: { label: string; days: number | null }[] = [
  { label: "Last 7 days", days: 7 },
  { label: "Last 30 days", days: 30 },
  { label: "Last 90 days", days: 90 },
  { label: "All time", days: null },
];

const approxFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata",
});

// Phosphor Icons (MIT), regular weight: "globe".
const GLOBE =
  "M128,24h0A104,104,0,1,0,232,128,104.12,104.12,0,0,0,128,24Zm88,104a87.61,87.61,0,0,1-3.33,24H174.16a157.44,157.44,0,0,0,0-48h38.51A87.61,87.61,0,0,1,216,128ZM102,168H154a115.11,115.11,0,0,1-26,45A115.27,115.27,0,0,1,102,168Zm-3.9-16a140.84,140.84,0,0,1,0-48h59.88a140.84,140.84,0,0,1,0,48ZM40,128a87.61,87.61,0,0,1,3.33-24H81.84a157.44,157.44,0,0,0,0,48H43.33A87.61,87.61,0,0,1,40,128ZM154,88H102a115.11,115.11,0,0,1,26-45A115.27,115.27,0,0,1,154,88Zm52.33,0H170.71a135.28,135.28,0,0,0-22.3-45.6A88.29,88.29,0,0,1,206.37,88ZM107.59,42.4A135.28,135.28,0,0,0,85.29,88H49.63A88.29,88.29,0,0,1,107.59,42.4ZM49.63,168H85.29a135.28,135.28,0,0,0,22.3,45.6A88.29,88.29,0,0,1,49.63,168Zm98.78,45.6a135.28,135.28,0,0,0,22.3-45.6h35.66A88.29,88.29,0,0,1,148.41,213.6Z";

function summarise(r: SearchWebResponse): string {
  const parts = Object.entries(r.added).map(([stage, n]) => `${n} ${stage}`);
  const added = r.total_added
    ? `Added ${r.total_added} new ${r.total_added === 1 ? "round" : "rounds"} from the web (${parts.join(", ")})`
    : "No new rounds found on the web for these dates";
  const skipped = r.duplicates ? `. ${r.duplicates} already known.` : ".";
  const cost = ` Used ${r.credits_used} Firecrawl credits and $${r.cost_usd.toFixed(4)}.`;
  const failed = r.failed_queries.length
    ? ` ${r.failed_queries.length} of 4 searches failed: ${r.failed_queries[0]}.` : "";
  return added + skipped + cost + failed;
}

const dayFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
});
const showDay = (ymd: string) => dayFmt.format(new Date(`${ymd}T00:00:00Z`));

type Section = "startups" | "vcs" | "events";

const SECTIONS: { key: Section; label: string; ready: boolean }[] = [
  { key: "startups", label: "Startup firms", ready: true },
  { key: "vcs", label: "VC firms", ready: false },
  { key: "events", label: "Events organised", ready: false },
];

function DownloadIcon() {
  return (
    <svg viewBox="0 0 256 256" width="15" height="15" fill="currentColor" aria-hidden="true">
      <path d={DOWNLOAD} />
    </svg>
  );
}

function StartupFirms() {
  const [stage, setStage] = useState<StageKey>("pre-seed");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [data, setData] = useState<RoundsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);

  const badRange = Boolean(start && end && start > end);
  const range: DateRange = { start: start || undefined, end: end || undefined };

  const load = useCallback(async (which: StageKey, r: DateRange) => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.rounds(which, r));
    } catch {
      setError("Could not reach the server.");
    } finally {
      setLoading(false);
    }
  }, []);

  // Any change of tab or date reloads straight away -- no Apply button.
  useEffect(() => {
    if (!badRange) void load(stage, { start: start || undefined, end: end || undefined });
  }, [load, stage, start, end, badRange]);

  // New funding stories are read on each scheduled refresh; follow them.
  useRefreshSync(() => {
    if (!badRange) void load(stage, range);
    setNote("Updated with the latest funding news");
  });

  /** Search the whole web for rounds in the chosen dates, then reload. */
  const searchWeb = async () => {
    if (!start || badRange || searching) return;
    setSearching(true);
    setNote(null);
    try {
      const result = await api.searchWebRounds(start, end || undefined);
      setNote(result.error && !result.total_added
        ? `Web search failed: ${result.error}`
        : summarise(result));
      await load(stage, range);
    } catch {
      setNote("Web search failed: could not reach the server.");
    } finally {
      setSearching(false);
    }
  };

  const preset = (days: number | null) => {
    setStart(days === null ? "" : istDay(days - 1));
    setEnd("");
    setNote(null);
  };
  const activePreset = PRESETS.find((p) =>
    p.days === null ? !start && !end : start === istDay(p.days - 1) && !end);
  const rangeText = start || end
    ? `${start ? showDay(start) : "the beginning"} to ${end ? showDay(end) : "today"}`
    : null;

  const rows = data?.rounds ?? [];
  const current = data?.stages.find((s) => s.key === stage);

  return (
    <section className="ins-main" aria-label="Startup firms">
      <header className="ins-head">
        <div>
          <h2>Startup funding</h2>
          <p>Companies that raised money, newest first, from the funding news in the feed.</p>
        </div>
        <div className="ins-actions">
          <a className="ins-download" href={api.roundsExcelUrl(stage, range)} download>
            <DownloadIcon />
            Excel: {current?.label ?? "this stage"}
          </a>
          <a className="ins-download ins-download--all" href={api.roundsExcelUrl(undefined, range)} download>
            <DownloadIcon />
            Excel: all stages
          </a>
        </div>
      </header>

      <div className="ins-filter" role="group" aria-label="Filter by publish date">
        <span className="ins-filter-label">Published</span>
        <div className="ins-presets">
          {PRESETS.map((p) => (
            <button
              key={p.label}
              aria-pressed={activePreset?.label === p.label}
              onClick={() => preset(p.days)}
            >
              {p.label}
            </button>
          ))}
        </div>
        <label className="ins-date">
          From
          <input
            type="date"
            value={start}
            max={end || istDay()}
            onChange={(e) => { setStart(e.target.value); setNote(null); }}
          />
        </label>
        <label className="ins-date">
          To
          <input
            type="date"
            value={end}
            min={start || undefined}
            max={istDay()}
            onChange={(e) => { setEnd(e.target.value); setNote(null); }}
          />
        </label>
        {(start || end) && (
          <button className="ins-clear" onClick={() => preset(null)}>Clear dates</button>
        )}
        <button
          className="ins-web"
          onClick={() => void searchWeb()}
          disabled={!start || badRange || searching}
          title={start
            ? "Searches the whole web for Pre-Seed to Series B rounds in these dates. About 8 to 16 Firecrawl credits."
            : "Choose a From date first"}
        >
          <svg viewBox="0 0 256 256" width="15" height="15" fill="currentColor" aria-hidden="true">
            <path d={GLOBE} />
          </svg>
          {searching ? "Searching the web" : "Search web"}
        </button>
      </div>

      {searching && (
        <div className="fc-scraping ins-searching" aria-live="polite">
          <span className="pulse" />
          Searching the web for Pre-Seed, Seed, Series A and Series B rounds
          from {rangeText ?? "these dates"}. This takes about half a minute.
        </div>
      )}

      {badRange && (
        <p className="answer-error ins-empty">The From date is after the To date.</p>
      )}

      <nav className="ins-tabs" role="tablist" aria-label="Funding stage">
        {(data?.stages ?? []).map((s) => (
          <button
            key={s.key}
            role="tab"
            aria-selected={stage === s.key}
            onClick={() => { setStage(s.key); setNote(null); }}
          >
            {s.label}
            <span className="ins-count">{s.count}</span>
          </button>
        ))}
      </nav>

      {note && (
        <p className="update-note" role="status">
          {note}
          <button onClick={() => setNote(null)}>Dismiss</button>
        </p>
      )}

      {data && data.pending > 0 && (
        <p className="side-note ins-pending">
          {data.pending} funding {data.pending === 1 ? "story is" : "stories are"} waiting
          to be read. {data.pending === 1 ? "It appears" : "They appear"} after the next
          scheduled refresh.
        </p>
      )}

      {error && <p className="answer-error ins-empty">{error}</p>}

      {!error && loading && !data && (
        <div aria-busy="true" aria-label="Loading rounds">
          {Array.from({ length: 5 }, (_, i) => <div key={i} className="skeleton" />)}
        </div>
      )}

      {!error && !badRange && data && rows.length === 0 && (
        <p className="side-note ins-empty">
          {rangeText
            ? `No ${current?.label ?? ""} rounds published from ${rangeText}.`
            : `No ${current?.label ?? ""} rounds in the feed yet. New ones appear here as funding news comes in.`}
        </p>
      )}

      {rows.length > 0 && !badRange && (
        <div className="ins-table-wrap">
          <table className="ins-table">
            <thead>
              <tr>
                <th>Company</th>
                <th>Round</th>
                <th>Investors</th>
                <th>Amount</th>
                <th>Published</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="ins-company">
                    <a href={r.url} target="_blank" rel="noreferrer noopener" title={r.headline}>
                      {r.company ?? "Unnamed company"}
                    </a>
                  </td>
                  <td>{r.round ?? r.stage}</td>
                  <td className="ins-investors">
                    {r.investors.length ? r.investors.join(", ") : <span className="ins-na">Not named</span>}
                  </td>
                  <td className="ins-amount">
                    {r.amount ?? <span className="ins-na">Undisclosed</span>}
                  </td>
                  <td className="ins-time">
                    {!r.published_at
                      ? <span className="ins-na">Not given</span>
                      : r.date_approx
                        ? <span title="The site gave a relative time, like 3 days ago">
                            About {approxFmt.format(parseTime(r.published_at))}
                          </span>
                        : `${publishedFmt.format(parseTime(r.published_at))} IST`}
                  </td>
                  <td className="ins-source">
                    {r.origin === "web" && <span className="ins-web-tag">Web</span>}
                    {r.source_label}
                    <CopyLink url={r.url} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function ComingSoon({ label }: { label: string }) {
  return (
    <section className="ins-main" aria-label={label}>
      <div className="ins-soon">
        <h2>{label}</h2>
        <p>This section is planned. Startup firms is ready to use now.</p>
      </div>
    </section>
  );
}

export default function Insights() {
  const [section, setSection] = useState<Section>("startups");
  const active = SECTIONS.find((s) => s.key === section)!;

  return (
    <div className="ins" id="main">
      <nav className="ins-side" aria-label="Insights">
        <h2>Insights</h2>
        {SECTIONS.map((s) => (
          <button
            key={s.key}
            className="ins-side-item"
            aria-current={section === s.key ? "page" : undefined}
            onClick={() => setSection(s.key)}
          >
            {s.label}
            {!s.ready && <span className="ins-soon-tag">Soon</span>}
          </button>
        ))}
      </nav>
      {active.ready ? <StartupFirms /> : <ComingSoon label={active.label} />}
    </div>
  );
}
