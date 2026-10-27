import { useCallback, useEffect, useState } from "react";
import {
  api, safeHref, type EnrichmentPreview, type FundingPage, type FundingQuery, type SourceType,
  type StageKey,
} from "./api";
import { DateInputs, ExportLink, SearchBox, Skeleton, Sources } from "./controls";
import { istDay, PAST_PRESETS, published, showDay } from "./dates";
import EnrichPanel from "./EnrichPanel";
import SourcesPanel from "./SourcesPanel";

const SOURCES: { key: SourceType | ""; label: string }[] = [
  { key: "", label: "All sources" },
  { key: "rss", label: "News feed" },
  { key: "website", label: "Your websites" },
  { key: "web_search", label: "Web search" },
  { key: "linkedin", label: "LinkedIn" },
];

/** Funded startups feed: every round, newest first, from every funding source. */
export default function Funding({ reloadKey, onEnrichment }: {
  reloadKey: number;
  onEnrichment: (message: string | null) => void;   // show the Enrichment section
}) {
  const [stage, setStage] = useState<StageKey | "">("");
  const [source, setSource] = useState<SourceType | "">("");
  const [start, setStart] = useState(istDay(29));
  const [end, setEnd] = useState("");
  const [q, setQ] = useState("");
  const [data, setData] = useState<FundingPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showSources, setShowSources] = useState(false);
  const [enrich, setEnrich] = useState<EnrichmentPreview | null>(null);
  const [enrichBusy, setEnrichBusy] = useState(false);
  const [enrichError, setEnrichError] = useState<string | null>(null);

  const badRange = Boolean(start && end && start > end);
  const filters: FundingQuery = {
    stage: stage || undefined, source: source || undefined,
    start: start || undefined, end: end || undefined, q: q || undefined,
  };
  const key = JSON.stringify(filters);

  const load = useCallback(async (f: FundingQuery) => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.funding(f));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reach the server.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!badRange) void load(JSON.parse(key) as FundingQuery);
  }, [key, badRange, load, reloadKey]);

  // The confirmation counts the companies in view; other filters, other companies.
  useEffect(() => {
    setEnrich(null);
    setEnrichError(null);
  }, [key]);

  const openEnrich = async () => {
    if (enrichBusy) return;
    setEnrichBusy(true);
    setEnrichError(null);
    try {
      setEnrich(await api.enrichmentPreview(filters));
    } catch (e) {
      setEnrichError(e instanceof Error ? e.message : "Could not reach the server.");
    } finally {
      setEnrichBusy(false);
    }
  };

  const preset = (days: number | null) => {
    setStart(days === null ? "" : istDay(days - 1));
    setEnd("");
  };
  const activePreset = PAST_PRESETS.find((p) =>
    p.days === null ? !start && !end : start === istDay(p.days - 1) && !end);
  const total = data?.stages.reduce((sum, s) => sum + s.count, 0) ?? 0;
  const rangeText = start || end
    ? `${start ? showDay(start) : "the beginning"} to ${end ? showDay(end) : "today"}`
    : "all time";

  return (
    <section className="view" aria-labelledby="funding-title">
      <header className="view-head">
        <div>
          <h2 id="funding-title">Funded startups</h2>
          <p className="muted">
            Indian startups that announced a funding round, from news feeds, web search,
            LinkedIn and the websites you add. One row per deal; every source that reported
            it is kept.
          </p>
        </div>
        <div className="head-actions">
          <button className="btn btn--secondary" aria-expanded={showSources}
                  aria-controls="sources-title" onClick={() => setShowSources(!showSources)}>
            {showSources ? "Hide sources" : "Sources"}
          </button>
          <button className="btn btn--secondary" aria-expanded={enrich !== null}
                  aria-controls="enrich-title" aria-busy={enrichBusy}
                  disabled={badRange || !data?.total || enrichBusy}
                  title="Find the leaders of the companies in this view, with their email, phone and LinkedIn, through SignalHire"
                  onClick={() => (enrich ? setEnrich(null) : void openEnrich())}>
            {enrichBusy ? "Counting companies" : "Send data for enrichment"}
          </button>
          <ExportLink href={api.fundingExcel(filters)} disabled={badRange || !data?.total} />
        </div>
      </header>

      {showSources && <SourcesPanel onClose={() => setShowSources(false)} />}
      {enrichError && <p className="error">{enrichError}</p>}
      {enrich && (
        <EnrichPanel preview={enrich} filters={filters} onClose={() => setEnrich(null)}
                     onSent={(message) => onEnrichment(message)}
                     onOpen={() => onEnrichment(null)} />
      )}

      <div className="filters">
        <div className="presets" role="group" aria-label="Published">
          {PAST_PRESETS.map((p) => (
            <button key={p.label} aria-pressed={activePreset?.label === p.label}
                    onClick={() => preset(p.days)}>
              {p.label}
            </button>
          ))}
        </div>
        <DateInputs start={start} end={end} onStart={setStart} onEnd={setEnd} max={istDay()}
                    label="Published between" />
        <select value={source} aria-label="Source"
                onChange={(e) => setSource(e.target.value as SourceType | "")}>
          {SOURCES.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
        </select>
        <SearchBox value={q} onChange={setQ} placeholder="Search company, sector, city" />
      </div>

      <nav className="tabs" role="tablist" aria-label="Funding stage">
        <button role="tab" aria-selected={stage === ""} onClick={() => setStage("")}>
          All stages <span className="count">{total}</span>
        </button>
        {(data?.stages ?? []).map((s) => (
          <button key={s.key} role="tab" aria-selected={stage === s.key}
                  onClick={() => setStage(s.key)}>
            {s.label} <span className="count">{s.count}</span>
          </button>
        ))}
      </nav>

      {badRange && <p className="error">The From date is after the To date.</p>}
      {error && <p className="error">{error}</p>}
      {!error && loading && !data && <Skeleton />}
      {!error && !badRange && data && data.rows.length === 0 && (
        <p className="empty">No funding rounds published {rangeText === "all time" ? "yet" : `from ${rangeText}`}
          {q ? ` matching "${q}"` : ""}. New rounds appear after each refresh.</p>
      )}

      {!badRange && data && data.rows.length > 0 && (
        <div className="table-wrap" aria-busy={loading}>
          <table>
            <thead>
              <tr>
                <th>Company</th>
                <th>Round</th>
                <th>Amount</th>
                <th>Investors</th>
                <th>Published</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.id}>
                  <td className="lead">
                    <a href={safeHref(r.source_url)} target="_blank" rel="noreferrer noopener"
                       title={r.headline}>{r.company}</a>
                    {r.summary && <p className="sub">{r.summary}</p>}
                    {(r.sector || r.hq_city) && (
                      <p className="tags">
                        {r.sector && <span className="tag">{r.sector}</span>}
                        {r.hq_city && <span className="tag tag--plain">{r.hq_city}</span>}
                      </p>
                    )}
                  </td>
                  <td>
                    <span className={`stage stage--${r.stage.toLowerCase().replace(/[^a-z]+/g, "-")}`}>
                      {r.stage}
                    </span>
                    {r.round && r.round !== r.stage && <p className="sub">{r.round}</p>}
                  </td>
                  <td className="num">{r.amount ?? <span className="muted">Undisclosed</span>}</td>
                  <td>{r.investors.length ? r.investors.join(", ")
                    : <span className="muted">Not named</span>}</td>
                  <td className="when">{published(r.published_at)}</td>
                  <td><Sources url={r.source_url} label={r.source_label} type={r.source_type}
                               others={r.sources} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          {data.total > data.rows.length && (
            <p className="more muted">
              Showing the newest {data.rows.length} of {data.total}. Export to Excel for all of them.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
