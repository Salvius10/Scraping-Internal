import { useCallback, useEffect, useState } from "react";
import {
  api, parseTime, safeHref, SOURCE_TYPE_LABEL, type AddKind, type SourceInfo, type SourcesResponse,
} from "./api";

const KINDS: { key: AddKind; label: string; placeholder: string; hint: string }[] = [
  {
    key: "linkedin", label: "LinkedIn account",
    placeholder: "https://www.linkedin.com/company/... or /in/...",
    hint: "Its posts are read through Apify once a day, about $0.002 a post. Funding posts become rows.",
  },
  {
    key: "website", label: "Website",
    placeholder: "https://example.com/startup-funding-news",
    hint: "A news or blog page. If it has a feed it is read free on every run; "
      + "otherwise Firecrawl reads the page once a day (1 credit).",
  },
  {
    key: "search", label: "Search phrase",
    placeholder: "e.g. Bengaluru fintech startup raises",
    hint: "Searched in news across the web once a day, last 2 days, through Firecrawl "
      + "(about 2-4 credits).",
  },
];

function lastRead(s: SourceInfo): { text: string; bad: boolean } {
  if (s.last_error) return { text: s.last_error, bad: true };
  if (!s.last_read) {
    return { text: s.last_skipped ? `Not read yet: ${s.last_skipped}` : "Not read yet", bad: false };
  }
  const hours = (Date.now() - parseTime(s.last_read).getTime()) / 3_600_000;
  const ago = hours < 1 ? "just now" : hours < 48 ? `${Math.round(hours)}h ago`
    : `${Math.round(hours / 24)} days ago`;
  const items = `${s.last_items} ${s.last_items === 1 ? "item" : "items"}`;
  return { text: `Read ${ago}, ${items}${s.warning ? ` (${s.warning})` : ""}`, bad: false };
}

function how(s: SourceInfo): string {
  if (s.type === "website") return s.via === "rss" ? "Feed, free" : "Page via Firecrawl";
  if (s.every_hours === 0) return "Every run";
  return `Every ${s.every_hours}h`;
}

function SourceRow({ s, onRemove }: { s: SourceInfo; onRemove?: (s: SourceInfo) => void }) {
  const [confirming, setConfirming] = useState(false);
  const status = lastRead(s);
  const link = s.type === "web_search" ? undefined : safeHref(s.detail);
  return (
    <li className="source-row">
      <div className="source-main">
        <strong>{s.label}</strong>
        <span className="tag tag--plain">{SOURCE_TYPE_LABEL[s.type] ?? s.type}</span>
        <span className="muted source-how">{how(s)}</span>
        {s.detail && s.detail !== s.label && (
          <p className="sub source-detail">
            {link ? <a href={link} target="_blank" rel="noreferrer noopener">{s.detail}</a> : s.detail}
          </p>
        )}
        <p className={`sub ${status.bad ? "warn" : ""}`}>{status.text}</p>
      </div>
      {onRemove && (
        confirming ? (
          <div className="source-actions">
            <button className="btn btn--danger" onClick={() => onRemove(s)}>Remove</button>
            <button className="btn btn--ghost" onClick={() => setConfirming(false)}>Keep</button>
          </div>
        ) : (
          <button className="btn btn--ghost" onClick={() => setConfirming(true)}
                  aria-label={`Remove ${s.label}`}>
            Remove
          </button>
        )
      )}
    </li>
  );
}

/** The Sources panel: what the funding feed reads, and adding or removing sources. */
export default function SourcesPanel({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<SourcesResponse | null>(null);
  const [kind, setKind] = useState<AddKind>("linkedin");
  const [value, setValue] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.sources("funding"));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reach the server.");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!value.trim() || busy) return;
    setBusy(true);
    setMessage(null);
    try {
      const answer = await api.addSource(kind, value.trim(), label.trim());
      setMessage({ text: answer.message, ok: answer.added });
      if (answer.added) {
        setValue("");
        setLabel("");
        await load();
      }
    } catch (err) {
      setMessage({ text: err instanceof Error ? err.message : "Could not add it.", ok: false });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (s: SourceInfo) => {
    if (s.id === null) return;
    try {
      await api.removeSource(s.id);
      setMessage({ text: `Removed ${s.label}. What it already found stays in the feed.`, ok: true });
      await load();
    } catch (err) {
      setMessage({ text: err instanceof Error ? err.message : "Could not remove it.", ok: false });
    }
  };

  const current = KINDS.find((k) => k.key === kind)!;
  const added = data?.sources.filter((s) => s.id !== null) ?? [];
  const builtIn = data?.sources.filter((s) => s.id === null) ?? [];
  const full = data ? data.added >= data.max_added : false;
  const missing = [
    data && !data.apify_ready && "APIFY_API_TOKEN (LinkedIn)",
    data && !data.firecrawl_ready && "FIRECRAWL_API_KEY (websites without a feed, search phrases)",
  ].filter(Boolean);

  return (
    <section className="sources-panel" aria-labelledby="sources-title">
      <header className="sources-head">
        <h3 id="sources-title">Sources</h3>
        <button className="btn btn--ghost" onClick={onClose}>Close</button>
      </header>

      <form className="add-source" onSubmit={(e) => void add(e)}>
        <div className="presets" role="radiogroup" aria-label="What to add">
          {KINDS.map((k) => (
            <button key={k.key} type="button" role="radio" aria-checked={kind === k.key}
                    onClick={() => { setKind(k.key); setMessage(null); }}>
              {k.label}
            </button>
          ))}
        </div>
        <div className="add-row">
          <input className="search" value={value} onChange={(e) => setValue(e.target.value)}
                 placeholder={current.placeholder} aria-label={current.label}
                 maxLength={kind === "search" ? 200 : 2000} required />
          <input className="search add-label" value={label} onChange={(e) => setLabel(e.target.value)}
                 placeholder="Name (optional)" aria-label="Name" maxLength={200} />
          <button className="btn btn--primary" type="submit" disabled={busy || full || !value.trim()}>
            {busy ? (kind === "website" ? "Checking the site" : "Adding") : "Add source"}
          </button>
        </div>
        <p className="sub">{current.hint} New sources are read on the next run; Refresh now
          reads them straight away.</p>
        {full && <p className="sub warn">All {data?.max_added} slots for added sources are in use.
          Remove one to add another.</p>}
        {missing.length > 0 && (
          <p className="sub warn">Not set on the server: {missing.join("; ")}. Those sources
            are kept but skipped until it is.</p>
        )}
        {message && <p className={`sub ${message.ok ? "ok" : "warn"}`} role="status">{message.text}</p>}
      </form>

      {error && <p className="error">{error}</p>}
      {data?.registry_error && <p className="sub warn">Built-in sources could not be loaded:
        {" "}{data.registry_error}</p>}

      <h4>Added by you <span className="count">{added.length}</span></h4>
      {added.length === 0
        ? <p className="sub">None yet. Add a LinkedIn account, a website or a search phrase above.</p>
        : <ul className="source-list">{added.map((s) => (
            <SourceRow key={s.key} s={s} onRemove={(x) => void remove(x)} />))}</ul>}

      <h4>Built in, on the 12-hour schedule <span className="count">{builtIn.length}</span></h4>
      <ul className="source-list">{builtIn.map((s) => <SourceRow key={s.key} s={s} />)}</ul>
    </section>
  );
}
