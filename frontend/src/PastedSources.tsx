import { useState } from "react";
import { api, type AddPastedSourceResponse, type PastedSection, type PastedSource } from "./api";
import { readAgo } from "./dates";

const COPY: Record<PastedSection, { placeholder: string; note: string; unit: [string, string] }> = {
  startups: {
    placeholder: "https://site.com/news, or its RSS feed",
    note: "Read free when the site has a feed or a plain news page, otherwise through Firecrawl (1 credit, at most once a day). gpt-oss reads each new post for a funding round, about $0.00005 a post, and only rounds appear in the tabs.",
    unit: ["round", "rounds"],
  },
  vcs: {
    placeholder: "https://firm.com/news, or its RSS feed",
    note: "Read free when the site has a feed or a plain news page, otherwise through Firecrawl (1 credit, at most once a day). gpt-oss sorts each new post like a firm's own news, about $0.00005 a post.",
    unit: ["deal post", "deal posts"],
  },
};

const count = (n: number, [one, many]: [string, string]) => `${n} ${n === 1 ? one : many}`;

function addedText(section: PastedSection, r: AddPastedSourceResponse): string {
  if (r.error && r.new === 0) {
    return `Added. The first read failed: ${r.error} It is tried again on the next refresh.`;
  }
  const cost = r.credits_used
    ? `${r.credits_used} Firecrawl credit${r.credits_used === 1 ? "" : "s"} and $${r.llm_usd.toFixed(4)}`
    : `$${r.llm_usd.toFixed(4)}`;
  const text = `Added, reading ${r.via_label}. Found ${count(r.found, ["post", "posts"])}: `
    + `${count(r.shown, COPY[section].unit)} to show, for ${cost}.`;
  return r.error ? `${text} ${r.error}` : text;
}

/** Paste a website; it is saved as a source for this page and read straight away. */
export function AddPastedSource({
  section, firms, onAdded,
}: {
  section: PastedSection;
  firms?: { key: string; label: string }[];   // VC firms only: tie it to a tracked firm
  onAdded: (message: string) => void;
}) {
  const [url, setUrl] = useState("");
  const [label, setLabel] = useState("");
  const [firm, setFirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async () => {
    if (busy || !url.trim()) return;
    setBusy(true);
    setProblem(null);
    try {
      const r = await api.addSource(section, url.trim(), label.trim(), firm);
      if (!r.added) {
        setProblem(r.error ?? "Could not add that link.");
      } else {
        setUrl(""); setLabel(""); setFirm("");
        onAdded(addedText(section, r));
      }
    } catch {
      setProblem("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="ev-add" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <h3>Add a website as a source</h3>
      <div className="ev-add-row">
        <input
          className="side-input ev-url"
          type="url"
          inputMode="url"
          value={url}
          placeholder={COPY[section].placeholder}
          aria-label="Website link"
          autoFocus
          disabled={busy}
          onChange={(e) => { setUrl(e.target.value); setProblem(null); }}
        />
        <input
          className="side-input ev-label"
          value={label}
          placeholder="Name (optional)"
          aria-label="Name for this source"
          maxLength={200}
          disabled={busy}
          onChange={(e) => setLabel(e.target.value)}
        />
        {firms && (
          <label className="ins-date vc-select">
            Firm
            <select value={firm} disabled={busy} onChange={(e) => setFirm(e.target.value)}>
              <option value="">Not a tracked firm</option>
              {firms.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
            </select>
          </label>
        )}
        <button type="submit" className="ins-web ev-go" disabled={busy || !url.trim()}>
          {busy ? "Adding and reading" : "Add and read"}
        </button>
      </div>
      <p className="side-note">{COPY[section].note}</p>
      {busy && (
        <div className="fc-scraping" aria-live="polite">
          <span className="pulse" />
          Reading this website now. This can take up to a minute.
        </div>
      )}
      {problem && <p className="answer-error">{problem}</p>}
    </form>
  );
}

/** The websites added on this page, each with how its last read went. */
export function PastedSourceList({
  section, sources, onChanged,
}: {
  section: PastedSection;
  sources: PastedSource[];
  onChanged: (message: string) => void;
}) {
  const [confirming, setConfirming] = useState<number | null>(null);
  if (sources.length === 0) return null;
  const failing = sources.filter((s) => s.last_error).length;

  const remove = async (source: PastedSource) => {
    if (confirming !== source.id) { setConfirming(source.id); return; }
    setConfirming(null);
    try {
      await api.removeSource(source.id);
      onChanged(`Removed ${source.label} and everything read from it.`);
    } catch {
      onChanged("Could not remove that website.");
    }
  };

  return (
    <details className="vc-sources">
      <summary>
        Websites you added: {sources.length}
        {failing > 0 && `, ${failing} with a failed last read`}
      </summary>
      <ul>
        {sources.map((s) => (
          <li key={s.id}>
            <p className="vc-firm-note">
              <a href={s.url} target="_blank" rel="noreferrer noopener">{s.label}</a>
              {s.firm_label && ` · for ${s.firm_label}`}
              {" · "}from {s.via_label}
              <span className={s.last_error ? "vc-error" : undefined}>
                {" · "}{readAgo(s.last_read)}
                {s.last_error && ` (failed: ${s.last_error})`}
              </span>
              {" · "}{count(s.shown, COPY[section].unit)}
              <button type="button" className="ev-remove" onClick={() => void remove(s)}>
                {confirming === s.id ? "Click again to remove" : "Remove"}
              </button>
            </p>
          </li>
        ))}
      </ul>
    </details>
  );
}
