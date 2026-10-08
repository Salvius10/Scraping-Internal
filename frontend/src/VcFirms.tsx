import { useCallback, useEffect, useState } from "react";
import CopyLink from "./CopyLink";
import DownloadIcon from "./DownloadIcon";
import { AddPastedSource, PastedSourceList } from "./PastedSources";
import RefreshButton from "./RefreshButton";
import {
  api, parseTime, type VcFirm, type VcKind, type VcOrigin, type VcPost, type VcsResponse,
} from "./api";
import { approxFmt, istDay, PRESETS, publishedFmt, readAgo, showDay } from "./dates";
import { useRefreshSync } from "./useRefreshSync";

const KIND_TONE: Record<VcKind, string> = {
  "Investment": "brand",
  "Portfolio news": "brand-2",
  "Fund news": "accent",
  "Other": "wash",
};

const ORIGINS: { key: VcOrigin | ""; label: string }[] = [
  { key: "", label: "All sources" },
  { key: "site", label: "Firm website" },
  { key: "added", label: "Added by you" },
  { key: "search", label: "News search" },
  { key: "news", label: "News feed" },
];

function Published({ post }: { post: VcPost }) {
  if (!post.published_at) return <span className="ins-na">Not given</span>;
  const at = parseTime(post.published_at);
  return post.date_approx
    ? <>{approxFmt.format(at)}</>
    : <>{publishedFmt.format(at)} IST</>;
}

/** How a firm is read: its own website first, then any news search. */
function FirmNote({ firm }: { firm: VcFirm }) {
  return (
    <p className="vc-firm-note">
      <a href={firm.home} target="_blank" rel="noreferrer noopener">{firm.label}</a>
      {firm.reads.map((r, i) => (
        <span key={`${r.via}-${i}`} className={r.last_error ? "vc-error" : undefined}>
          {" · "}{r.label[0].toUpperCase() + r.label.slice(1)}, {readAgo(r.last_read)}
          {r.last_error && ` (failed: ${r.last_error})`}
        </span>
      ))}
      {firm.no_site && <span className="vc-no-site"> · No website news: {firm.no_site}</span>}
    </p>
  );
}

export default function VcFirms() {
  const [firm, setFirm] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [origin, setOrigin] = useState<VcOrigin | "">("");
  const [showOther, setShowOther] = useState(false);
  const [data, setData] = useState<VcsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const badRange = Boolean(start && end && start > end);
  const query = {
    firm: firm || undefined, start: start || undefined, end: end || undefined,
    origin: origin || undefined, all: showOther,
  };

  const load = useCallback(async (q: typeof query) => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.vcs(q));
    } catch {
      setError("Could not reach the server.");
    } finally {
      setLoading(false);
    }
  }, []);

  // Any change of firm, dates or toggle reloads straight away.
  useEffect(() => {
    if (!badRange) void load(query);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, firm, start, end, origin, showOther, badRange]);

  // Firms are read on each scheduled refresh; follow it.
  useRefreshSync(() => {
    if (!badRange) void load(query);
    setNote("Updated with the latest VC firm news");
  });

  const preset = (days: number | null) => {
    setStart(days === null ? "" : istDay(days - 1));
    setEnd("");
  };
  const activePreset = PRESETS.find((p) =>
    p.days === null ? !start && !end : start === istDay(p.days - 1) && !end);
  const rangeText = start || end
    ? `${start ? showDay(start) : "the beginning"} to ${end ? showDay(end) : "today"}`
    : null;

  const firms = data?.firms ?? [];
  const chosen = firms.find((f) => f.key === firm);
  const rows = data?.posts ?? [];
  const tracked = firms.filter((f) => !f.pasted);
  const failing = tracked.filter((f) => f.reads.some((r) => r.last_error));
  const withSite = tracked.filter((f) => f.reads.some((r) => r.is_site)).length;

  return (
    <section className="ins-main" aria-label="VC firms">
      <header className="ins-head">
        <div>
          <h2>VC firm activity</h2>
          <p>
            Who each firm funded or helped, newest first: from each firm&apos;s own website
            news and the websites you add, plus news searches and funding news in the feed that
            names it as an investor.
          </p>
        </div>
        <div className="ins-actions">
          <button
            type="button"
            className="ins-download ins-download--all"
            aria-pressed={adding}
            onClick={() => setAdding(!adding)}
          >
            {adding ? "Close" : "Add a website"}
          </button>
          <RefreshButton
            scope="vcs"
            onDone={() => { if (!badRange) void load(query); }}
            onMessage={setNote}
          />
          <a className="ins-download" href={api.vcsExcelUrl(query)} download>
            <DownloadIcon />
            Excel: {chosen?.label ?? "all firms"}
          </a>
        </div>
      </header>

      {adding && (
        <AddPastedSource
          section="vcs"
          firms={tracked}
          onAdded={(message) => { setNote(message); if (!badRange) void load(query); }}
        />
      )}

      <div className="ins-filter" role="group" aria-label="Filter VC firm news">
        <label className="ins-date vc-select">
          Firm
          <select value={firm} onChange={(e) => { setFirm(e.target.value); setNote(null); }}>
            <option value="">All firms ({firms.reduce((n, f) => n + f.count, 0)})</option>
            {firms.map((f) => (
              <option key={f.key} value={f.key}>{f.label} ({f.count})</option>
            ))}
          </select>
        </label>
        <span className="ins-filter-label">Found via</span>
        <div className="ins-presets" role="group" aria-label="Found via">
          {ORIGINS.map((o) => (
            <button key={o.key} aria-pressed={origin === o.key} onClick={() => setOrigin(o.key)}>
              {o.label}
            </button>
          ))}
        </div>
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
          <input type="date" value={start} max={end || istDay()}
            onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="ins-date">
          To
          <input type="date" value={end} min={start || undefined} max={istDay()}
            onChange={(e) => setEnd(e.target.value)} />
        </label>
        {(start || end) && (
          <button className="ins-clear" onClick={() => preset(null)}>Clear dates</button>
        )}
        <label className="vc-toggle" title="Essays, podcasts, events and other posts that are not about a deal">
          <input type="checkbox" checked={showOther} onChange={(e) => setShowOther(e.target.checked)} />
          Show other posts{data && !showOther && data.hidden_other > 0 ? ` (${data.hidden_other})` : ""}
        </label>
      </div>

      {chosen && <FirmNote firm={chosen} />}

      {note && (
        <p className="update-note" role="status">
          {note}
          <button onClick={() => setNote(null)}>Dismiss</button>
        </p>
      )}

      {data && data.pending > 0 && (
        <p className="side-note ins-pending">
          {data.pending} new {data.pending === 1 ? "post is" : "posts are"} waiting to be
          sorted. {data.pending === 1 ? "It appears" : "They appear"} after the next
          scheduled refresh.
        </p>
      )}

      {badRange && <p className="answer-error ins-empty">The From date is after the To date.</p>}
      {error && <p className="answer-error ins-empty">{error}</p>}

      {!error && loading && !data && (
        <div aria-busy="true" aria-label="Loading VC firm news">
          {Array.from({ length: 5 }, (_, i) => <div key={i} className="skeleton" />)}
        </div>
      )}

      {!error && !badRange && data && rows.length === 0 && (
        <p className="side-note ins-empty">
          {`No ${showOther ? "" : "deal "}news${origin ? ` from ${ORIGINS.find((o) => o.key === origin)!.label.toLowerCase()}` : ""} for ${chosen?.label ?? "these firms"}`}
          {rangeText ? ` published from ${rangeText}.` : " yet. It appears here after the next refresh."}
        </p>
      )}

      {rows.length > 0 && !badRange && (
        <div className="ins-table-wrap">
          <table className="ins-table vc-table">
            <thead>
              <tr>
                {!firm && <th>VC firm</th>}
                <th>News</th>
                <th>What</th>
                <th>Amount</th>
                <th>Published</th>
                <th>Found via</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.id}>
                  {!firm && <td className="vc-firm">{p.firm_label}</td>}
                  <td className="vc-news">
                    <a href={p.url} target="_blank" rel="noreferrer noopener">{p.headline}</a>
                    {p.company && <span className="vc-company">{p.company}</span>}
                  </td>
                  <td>
                    <span className={`vc-kind vc-kind--${KIND_TONE[p.kind]}`}>{p.kind}</span>
                    {p.round && <span className="vc-round">{p.round}</span>}
                  </td>
                  <td className="ins-amount">{p.amount ?? <span className="ins-na">—</span>}</td>
                  <td className="ins-time"><Published post={p} /></td>
                  <td className="ins-source">
                    {p.origin === "news" && <span className="ins-web-tag vc-news-tag">News</span>}
                    {p.origin === "added" && <span className="ins-web-tag">Added</span>}
                    {p.source_label}
                    <CopyLink url={p.url} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tracked.length > 0 && (
        <details className="vc-sources">
          <summary>
            How each firm is read: {withSite} of {tracked.length} from their own website
            {failing.length > 0 && `, ${failing.length} with a failed last read`}
          </summary>
          <ul>
            {tracked.map((f) => <li key={f.key}><FirmNote firm={f} /></li>)}
          </ul>
        </details>
      )}

      <PastedSourceList
        section="vcs"
        sources={data?.sources ?? []}
        onChanged={(message) => {
          setNote(message);
          // The chosen website may be the one removed; changing firm reloads.
          if (firm.startsWith("pasted-")) setFirm("");
          else if (!badRange) void load(query);
        }}
      />
    </section>
  );
}
