import { useState } from "react";
import CopyLink from "./CopyLink";
import { api, type ExtractResponse } from "./api";

const EXAMPLES = [
  "List every article headline with its link and date",
  "Every funding round: company, amount, investors",
  "Summarise this page in three bullet points",
];

type Entry = ExtractResponse & { key: number };

const isUrl = (value: unknown): value is string =>
  typeof value === "string" && /^https?:\/\//.test(value);

function cellText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function toCsv(rows: Record<string, unknown>[], columns: string[]): string {
  const quote = (v: string) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return [
    columns.map(quote).join(","),
    ...rows.map((r) => columns.map((c) => quote(cellText(r[c]))).join(",")),
  ].join("\n");
}

function download(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function Table({ rows }: { rows: Record<string, unknown>[] }) {
  const columns = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  return (
    <div className="x-table-wrap">
      <table className="x-table">
        <thead>
          <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c}>
                  {isUrl(row[c]) ? (
                    <span className="x-link">
                      <a href={row[c] as string} target="_blank" rel="noreferrer noopener">
                        {row[c] as string}
                      </a>
                      <CopyLink url={row[c] as string} />
                    </span>
                  ) : cellText(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Result({ entry }: { entry: Entry }) {
  const json = JSON.stringify(entry.result, null, 2);
  const columns = entry.rows ? [...new Set(entry.rows.flatMap((r) => Object.keys(r)))] : [];
  let host = entry.url;
  try { host = new URL(entry.final_url ?? entry.url).host; } catch { /* keep raw */ }

  return (
    <article className="x-result">
      <header className="x-result-head">
        <div>
          <h4>{entry.prompt}</h4>
          <p className="x-meta">
            {entry.final_url ? (
              <a href={entry.final_url} target="_blank" rel="noreferrer noopener">{host}</a>
            ) : host}
            {!entry.error && (
              <>
                {" · "}{entry.cached ? "cached, $0" : `$${entry.cost_usd.toFixed(6)}`}
                {!entry.cached && ` · ${entry.seconds}s`}
                {entry.rows && ` · ${entry.rows.length} rows`}
              </>
            )}
          </p>
        </div>
        {!entry.error && (
          <div className="x-actions">
            {entry.rows && (
              <button
                className="side-button"
                onClick={() => download("extract.csv", toCsv(entry.rows!, columns), "text/csv")}
              >
                CSV
              </button>
            )}
            <button
              className="side-button"
              onClick={() => download("extract.json", json, "application/json")}
            >
              JSON
            </button>
          </div>
        )}
      </header>

      {entry.error && <p className="answer-error">{entry.error}</p>}
      {entry.notes.map((note) => <p key={note} className="side-note">{note}</p>)}

      {!entry.error && (
        entry.rows ? <Table rows={entry.rows} />
          : typeof entry.result === "string" ? <p className="x-text">{entry.result}</p>
            : <pre className="x-json">{json}</pre>
      )}
    </article>
  );
}

/** Paste any page, say what to pull out, get a table back. */
export default function ExtractPanel({ onClose }: { onClose: () => void }) {
  const [url, setUrl] = useState("");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState<Entry[]>([]);

  async function run(promptText = prompt) {
    if (busy || !url.trim() || !promptText.trim()) return;
    setBusy(true);
    let result: ExtractResponse;
    try {
      result = await api.extract(url.trim(), promptText.trim());
    } catch {
      result = {
        url, final_url: null, prompt: promptText, result: null, rows: null,
        cost_usd: 0, calls: 0, page_chars: 0, truncated: false, cached: false,
        seconds: 0, notes: [], error: "Could not reach the server.", budget_remaining: 0,
      };
    }
    setLog((prev) => [{ ...result, key: Date.now() }, ...prev]);
    setBusy(false);
  }

  return (
    <section className="x-panel" aria-label="Extract from a web page">
      <div className="x-panel-head">
        <h2>Extract from any page</h2>
        <button className="sidebar-close" onClick={onClose}>Close</button>
      </div>

      <form className="x-form" onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <input
          className="side-input"
          type="url"
          inputMode="url"
          value={url}
          placeholder="https://entrackr.com/tag/funding"
          aria-label="Page link"
          disabled={busy}
          onChange={(e) => setUrl(e.target.value)}
        />
        <textarea
          className="side-input x-prompt"
          value={prompt}
          rows={2}
          maxLength={500}
          placeholder="What should be pulled out? e.g. every funding round with company, amount and investors"
          aria-label="What to extract"
          disabled={busy}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void run(); }
          }}
        />
        <div className="x-form-foot">
          <div className="chips">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                className="chip"
                disabled={busy}
                onClick={() => { setPrompt(example); void run(example); }}
              >
                {example}
              </button>
            ))}
          </div>
          <button type="submit" className="x-run" disabled={busy || !url.trim() || !prompt.trim()}>
            {busy ? "Extracting" : "Extract"}
          </button>
        </div>
        <p className="side-note">
          Reads the page once with gpt-oss — usually under $0.01. Pages built
          entirely in the browser with JavaScript cannot be read.
        </p>
      </form>

      {busy && (
        <div className="intel-pending" aria-live="polite">
          <span className="pulse" />
          Fetching the page and reading it — this can take up to a minute…
        </div>
      )}

      {log.map((entry) => <Result key={entry.key} entry={entry} />)}
    </section>
  );
}
