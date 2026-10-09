import { useCallback, useEffect, useState } from "react";
import CopyLink from "./CopyLink";
import DownloadIcon from "./DownloadIcon";
import RefreshButton from "./RefreshButton";
import {
  api, parseTime, type AddLinkedinSourceResponse, type LinkedinKind, type LinkedinKindKey,
  type LinkedinPost, type LinkedinResponse, type LinkedinSource,
} from "./api";
import { istDay, PRESETS, publishedFmt, readAgo, showDay } from "./dates";
import { useRefreshSync } from "./useRefreshSync";

const KIND_TONE: Record<LinkedinKind, string> = {
  Funding: "brand",
  News: "brand-2",
  Other: "wash",
};

const KINDS: { key: LinkedinKindKey | ""; label: string }[] = [
  { key: "", label: "Funding and news" },
  { key: "funding", label: "Funding" },
  { key: "news", label: "News" },
];

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function addedText(r: AddLinkedinSourceResponse): string {
  if (r.error && r.new === 0) {
    return `Added ${r.label}. The first read failed: ${r.error} It is tried again on the next refresh.`;
  }
  const text = `Added ${r.label}. Read ${plural(r.found, "post", "posts")}: `
    + `${r.funding} funding, ${r.news} news, for about $${r.apify_usd.toFixed(3)} of Apify `
    + `and $${r.llm_usd.toFixed(4)} of gpt-oss.`;
  return r.error ? `${text} ${r.error}` : text;
}

/** Paste a profile or company page; it is saved as a source and read straight away. */
function AddAccount({ ready, onAdded }: { ready: boolean; onAdded: (message: string) => void }) {
  const [url, setUrl] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async () => {
    if (busy || !url.trim() || !ready) return;
    setBusy(true);
    setProblem(null);
    try {
      const r = await api.addLinkedinSource(url.trim(), label.trim());
      if (!r.added) {
        setProblem(r.error ?? "Could not add that link.");
      } else {
        setUrl(""); setLabel("");
        onAdded(addedText(r));
      }
    } catch {
      setProblem("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="ev-add" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <h3>Add a LinkedIn account as a source</h3>
      <div className="ev-add-row">
        <input
          className="side-input ev-url"
          type="url"
          inputMode="url"
          value={url}
          placeholder="https://www.linkedin.com/company/name or /in/name"
          aria-label="LinkedIn profile or company page link"
          autoFocus
          disabled={busy || !ready}
          onChange={(e) => { setUrl(e.target.value); setProblem(null); }}
        />
        <input
          className="side-input ev-label"
          value={label}
          placeholder="Name (optional)"
          aria-label="Name for this account"
          maxLength={200}
          disabled={busy || !ready}
          onChange={(e) => setLabel(e.target.value)}
        />
        <button type="submit" className="ins-web ev-go" disabled={busy || !url.trim() || !ready}>
          {busy ? "Adding and reading" : "Add to sources"}
        </button>
      </div>
      <p className="side-note">
        Read through Apify, with no LinkedIn login: the newest 20 posts now (about $0.04),
        then only new posts, once a day. gpt-oss reads each new post for funding and company
        news, about $0.0001 a post.
      </p>
      {!ready && (
        <p className="answer-error">
          LinkedIn is read through Apify, which is not set up: add APIFY_API_TOKEN to .env and
          restart the server.
        </p>
      )}
      {busy && (
        <div className="fc-scraping" aria-live="polite">
          <span className="pulse" />
          Reading this account&apos;s posts now. This can take up to a minute.
        </div>
      )}
      {problem && <p className="answer-error">{problem}</p>}
    </form>
  );
}

/** One account's health: how it is read and how the last read went. */
function AccountNote({
  source, confirming, onRemove,
}: {
  source: LinkedinSource;
  confirming: boolean;
  onRemove: (source: LinkedinSource) => void;
}) {
  return (
    <p className="vc-firm-note">
      <a href={source.url} target="_blank" rel="noreferrer noopener">{source.label}</a>
      {" · "}{source.kind_label}
      <span className={source.last_error ? "vc-error" : undefined}>
        {" · "}{readAgo(source.last_read)}
        {source.last_error && ` (failed: ${source.last_error})`}
      </span>
      {" · "}{plural(source.posts, "post", "posts")} read
      <button type="button" className="ev-remove" onClick={() => onRemove(source)}>
        {confirming ? "Click again to remove" : "Remove"}
      </button>
    </p>
  );
}

function Reposted({ post }: { post: LinkedinPost }) {
  if (!post.repost || !post.author) return null;
  return <span className="vc-company">Reposted from {post.author}</span>;
}

export default function LinkedIn() {
  const [source, setSource] = useState<number | null>(null);
  const [kind, setKind] = useState<LinkedinKindKey | "">("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [showOther, setShowOther] = useState(false);
  const [data, setData] = useState<LinkedinResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [confirming, setConfirming] = useState<number | null>(null);

  const badRange = Boolean(start && end && start > end);
  const query = {
    source: source ?? undefined, kind: kind || undefined,
    start: start || undefined, end: end || undefined, all: showOther,
  };

  const load = useCallback(async (q: typeof query) => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.linkedin(q));
    } catch {
      setError("Could not reach the server.");
    } finally {
      setLoading(false);
    }
  }, []);

  // Any change of account, kind, dates or toggle reloads straight away.
  useEffect(() => {
    if (!badRange) void load(query);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, source, kind, start, end, showOther, badRange]);

  // Accounts are read on each scheduled refresh; follow it.
  useRefreshSync(() => {
    if (!badRange) void load(query);
    setNote("Updated with the latest LinkedIn posts");
  });

  const reload = () => { if (!badRange) void load(query); };

  const preset = (days: number | null) => {
    setStart(days === null ? "" : istDay(days - 1));
    setEnd("");
  };
  const activePreset = PRESETS.find((p) =>
    p.days === null ? !start && !end : start === istDay(p.days - 1) && !end);
  const rangeText = start || end
    ? `${start ? showDay(start) : "the beginning"} to ${end ? showDay(end) : "today"}`
    : null;

  const sources = data?.sources ?? [];
  const chosen = sources.find((s) => s.id === source);
  const rows = data?.posts ?? [];
  const failing = sources.filter((s) => s.last_error).length;
  const noSources = data !== null && sources.length === 0;
  const kindCount = (key: LinkedinKindKey | "") => {
    if (!data) return null;
    return key ? data.counts[key] : data.counts.funding + data.counts.news;
  };

  const remove = async (s: LinkedinSource) => {
    if (confirming !== s.id) { setConfirming(s.id); return; }
    setConfirming(null);
    try {
      await api.removeLinkedinSource(s.id);
      setNote(`Removed ${s.label} and everything read from it.`);
      if (source === s.id) setSource(null);   // changing account reloads
      else reload();
    } catch {
      setNote("Could not remove that account.");
    }
  };

  return (
    <div className="ins" id="main">
      <nav className="ins-side" aria-label="LinkedIn accounts">
        <h2>LinkedIn accounts</h2>
        <button
          className="ins-side-item"
          aria-current={source === null ? "page" : undefined}
          onClick={() => setSource(null)}
        >
          All accounts
          <span className="li-side-count">{sources.reduce((n, s) => n + s.count, 0)}</span>
        </button>
        {sources.map((s) => (
          <button
            key={s.id}
            className="ins-side-item"
            aria-current={source === s.id ? "page" : undefined}
            title={s.last_error ? `Last read failed: ${s.last_error}` : s.url}
            onClick={() => setSource(s.id)}
          >
            <span className="li-side-label">
              {s.label}
              {s.last_error && <span className="li-side-error" aria-label="last read failed"> !</span>}
            </span>
            <span className="li-side-count">{s.count}</span>
          </button>
        ))}
        <button type="button" className="li-side-add" onClick={() => setAdding(true)}>
          + Add an account
        </button>
      </nav>

      <section className="ins-main" aria-label="LinkedIn">
        <header className="ins-head">
          <div>
            <h2>{chosen ? chosen.label : "LinkedIn activity"}</h2>
            <p>
              Funding and company news posted by the LinkedIn profiles and company pages you
              add, newest first. Each account is read again once a day.
            </p>
          </div>
          <div className="ins-actions">
            {!noSources && (
              <button
                type="button"
                className="ins-download ins-download--all"
                aria-pressed={adding}
                onClick={() => setAdding(!adding)}
              >
                {adding ? "Close" : "Add an account"}
              </button>
            )}
            <RefreshButton scope="linkedin" onDone={reload} onMessage={setNote} />
            <a className="ins-download" href={api.linkedinExcelUrl(query)} download>
              <DownloadIcon />
              Excel: {chosen?.label ?? "all accounts"}
            </a>
          </div>
        </header>

        {(adding || noSources) && (
          <AddAccount
            ready={data?.apify_ready ?? true}
            onAdded={(message) => { setNote(message); setAdding(false); reload(); }}
          />
        )}

        <div className="ins-filter" role="group" aria-label="Filter LinkedIn posts">
          <span className="ins-filter-label">Show</span>
          <div className="ins-presets" role="group" aria-label="Kind of post">
            {KINDS.map((k) => (
              <button key={k.key} aria-pressed={kind === k.key} onClick={() => setKind(k.key)}>
                {k.label}
                {kindCount(k.key) !== null && <span className="li-kind-count">{kindCount(k.key)}</span>}
              </button>
            ))}
          </div>
          <span className="ins-filter-label">Posted</span>
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
          {!kind && (
            <label className="vc-toggle" title="Opinions, hiring ads, event invitations and other posts that announce no funding or news">
              <input type="checkbox" checked={showOther} onChange={(e) => setShowOther(e.target.checked)} />
              Show other posts{data && !showOther && data.hidden_other > 0 ? ` (${data.hidden_other})` : ""}
            </label>
          )}
        </div>

        {chosen && (
          <AccountNote
            source={chosen}
            confirming={confirming === chosen.id}
            onRemove={(s) => void remove(s)}
          />
        )}

        {note && (
          <p className="update-note" role="status">
            {note}
            <button onClick={() => setNote(null)}>Dismiss</button>
          </p>
        )}

        {data && data.pending > 0 && (
          <p className="side-note ins-pending">
            {plural(data.pending, "new post is", "new posts are")} waiting to be read by the
            model. {data.pending === 1 ? "It appears" : "They appear"} after the next refresh.
          </p>
        )}

        {badRange && <p className="answer-error ins-empty">The From date is after the To date.</p>}
        {error && <p className="answer-error ins-empty">{error}</p>}

        {!error && loading && !data && (
          <div aria-busy="true" aria-label="Loading LinkedIn posts">
            {Array.from({ length: 5 }, (_, i) => <div key={i} className="skeleton" />)}
          </div>
        )}

        {!error && !badRange && data && !noSources && rows.length === 0 && (
          <p className="side-note ins-empty">
            {`No ${kind ? `${kind} ` : showOther ? "" : "funding or news "}posts from ${chosen?.label ?? "these accounts"}`}
            {rangeText ? ` posted from ${rangeText}.` : " yet. New ones appear here after each refresh."}
          </p>
        )}

        {noSources && (
          <p className="side-note ins-empty">
            No LinkedIn accounts yet. Paste a profile or company page above: its posts are
            read straight away, then every day after.
          </p>
        )}

        {rows.length > 0 && !badRange && (
          <div className="ins-table-wrap">
            <table className="ins-table vc-table li-table">
              <thead>
                <tr>
                  {source === null && <th>Account</th>}
                  <th>Post</th>
                  <th>What</th>
                  <th>Investors</th>
                  <th>Amount</th>
                  <th>Posted</th>
                  <th>Link</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.id}>
                    {source === null && (
                      <td className="vc-firm">{p.account}<Reposted post={p} /></td>
                    )}
                    <td className="vc-news">
                      <a href={p.url} target="_blank" rel="noreferrer noopener">{p.headline}</a>
                      {p.company && <span className="vc-company">{p.company}</span>}
                      {source !== null && <Reposted post={p} />}
                      <p className="li-snippet" title={p.snippet}>{p.snippet}</p>
                    </td>
                    <td>
                      <span className={`vc-kind vc-kind--${KIND_TONE[p.kind]}`}>{p.kind}</span>
                      {p.round && <span className="vc-round">{p.round}</span>}
                    </td>
                    <td className="ins-investors">
                      {p.investors.length ? p.investors.join(", ") : <span className="ins-na">—</span>}
                    </td>
                    <td className="ins-amount">{p.amount ?? <span className="ins-na">—</span>}</td>
                    <td className="ins-time">
                      {p.posted_at
                        ? `${publishedFmt.format(parseTime(p.posted_at))} IST`
                        : <span className="ins-na">Not given</span>}
                    </td>
                    <td className="ins-source"><CopyLink url={p.url} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {sources.length > 0 && (
          <details className="vc-sources">
            <summary>
              Accounts you added: {sources.length}
              {failing > 0 && `, ${failing} with a failed last read`}
            </summary>
            <ul>
              {sources.map((s) => (
                <li key={s.id}>
                  <AccountNote
                    source={s}
                    confirming={confirming === s.id}
                    onRemove={(x) => void remove(x)}
                  />
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>
    </div>
  );
}
