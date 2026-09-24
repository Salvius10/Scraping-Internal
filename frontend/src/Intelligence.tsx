import { useState, type ReactNode } from "react";
import CopyLink from "./CopyLink";
import {
  api, categoryTextColor, parseTime, sourceLabel,
  type Citation, type IntelligenceResponse, type SourceStatus,
} from "./api";

const EXAMPLES = [
  "What is happening with Zepto's IPO?",
  "Which startups raised funding this week?",
  "Any layoffs or shutdowns recently?",
];

const dateFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata",
});

type Entry = IntelligenceResponse & { key: number };

const CITE = /\[(\d+(?:\s*[,–-]\s*\d+)*)\]/g;

/** Numbers inside one bracket group: "2", "1, 4", "2-4". */
function expand(group: string): number[] {
  const out: number[] = [];
  for (const part of group.split(/\s*,\s*/)) {
    const [lo, hi = lo] = part.split(/\s*[–-]\s*/).map(Number);
    if (Number.isFinite(lo) && Number.isFinite(hi) && hi - lo <= 20) {
      for (let n = lo; n <= hi; n++) out.push(n);
    }
  }
  return out;
}

/** One line of the answer, with [n] markers turned into footnote links. */
function withCitations(line: string, entryKey: number, valid: Set<number>): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  for (const match of line.matchAll(CITE)) {
    nodes.push(line.slice(last, match.index));
    for (const n of expand(match[1])) {
      nodes.push(
        valid.has(n) ? (
          <a key={`${match.index}-${n}`} className="cite" href={`#cite-${entryKey}-${n}`}>
            {n}
          </a>
        ) : null,
      );
    }
    last = (match.index ?? 0) + match[0].length;
  }
  nodes.push(line.slice(last));
  return nodes;
}

function Prose({ entry }: { entry: Entry }) {
  const valid = new Set(entry.citations.map((c) => c.n));
  return (
    <div className="intel-prose">
      {(entry.answer ?? "").split("\n").filter((l) => l.trim()).map((raw, i) => {
        const bullet = /^\s*[-*•]\s+/.test(raw);
        const line = raw.replace(/^\s*[-*•]\s+/, "").replace(/\*\*/g, "").replace(/^#+\s*/, "");
        return (
          <p key={i} className={bullet ? "answer-bullet" : "answer-line"}>
            {withCitations(line, entry.key, valid)}
          </p>
        );
      })}
    </div>
  );
}

function statusText(s: SourceStatus): string {
  switch (s.status) {
    case "ok": return `${s.found} found${s.new ? `, ${s.new} new` : ""}`;
    case "empty": return "nothing";
    case "timeout": return "timed out";
    case "error": return "unreachable";
    default: return "not searched";
  }
}

function Sources({ sources }: { sources: SourceStatus[] }) {
  if (sources.length === 0) return null;
  return (
    <ul className="intel-sources" aria-label="Sources searched live">
      {sources.map((s) => (
        <li
          key={s.name}
          data-status={s.status}
          title={s.error ?? (s.cached ? "Reused from a search in the last 10 minutes" : undefined)}
        >
          <span className="intel-source-name">{s.label}</span>
          {statusText(s)}
        </li>
      ))}
    </ul>
  );
}

function CitationItem({ citation, entryKey }: { citation: Citation; entryKey: number }) {
  const meta = [
    sourceLabel(citation.source),
    citation.published_at ? dateFmt.format(parseTime(citation.published_at)) : null,
    citation.company,
  ].filter(Boolean).join(" · ");
  return (
    <li id={`cite-${entryKey}-${citation.n}`} className="intel-cite">
      <span className="intel-cite-n">{citation.n}</span>
      <div className="intel-cite-body">
        <a href={citation.url} target="_blank" rel="noreferrer noopener">
          {citation.headline}
        </a>
        <div className="intel-cite-meta">
          {meta}
          {citation.category && (
            <span style={{ color: categoryTextColor(citation.category) }}>
              {citation.category}
            </span>
          )}
          {citation.discovered && (
            <span className="intel-new" title="Found by this search; now in the feed too">
              new
            </span>
          )}
          <CopyLink url={citation.url} />
        </div>
      </div>
    </li>
  );
}

function Answer({ entry }: { entry: Entry }) {
  const cited = entry.citations.filter((c) => c.cited);
  const rest = entry.citations.filter((c) => !c.cited);
  // Without an answer (budget spent, model down) every match is the result.
  const primary = entry.answer ? cited : entry.citations;
  const secondary = entry.answer ? rest : [];

  return (
    <article className="intel-answer">
      <h3 className="intel-q">{entry.question}</h3>
      {entry.search && (
        <p className="intel-plan">
          Searched for “{entry.search}”
          {entry.since_days ? ` · last ${entry.since_days} days` : ""}
          {entry.terms.length > 0 && ` · ${entry.terms.join(", ")}`}
        </p>
      )}

      {entry.error && <p className="answer-error">{entry.error}</p>}
      {entry.answer && <Prose entry={entry} />}

      <Sources sources={entry.sources} />

      {primary.length > 0 && (
        <>
          <h4 className="intel-list-head">
            {entry.answer ? "Sources cited" : "Matching stories"}
          </h4>
          <ol className="intel-cites">
            {primary.map((c) => <CitationItem key={c.n} citation={c} entryKey={entry.key} />)}
          </ol>
        </>
      )}

      {secondary.length > 0 && (
        <details className="intel-more">
          <summary>Also retrieved, not cited ({secondary.length})</summary>
          <ol className="intel-cites">
            {secondary.map((c) => <CitationItem key={c.n} citation={c} entryKey={entry.key} />)}
          </ol>
        </details>
      )}

      {entry.answer && entry.model && (
        <p className="answer-meta">
          {entry.model.includes("sonnet") ? "Sonnet 4.6" : "gpt-oss"} · $
          {entry.cost_usd.toFixed(6)}
          {entry.plan_cached ? " · search plan cached" : ""}
        </p>
      )}
    </article>
  );
}

export default function Intelligence() {
  const [question, setQuestion] = useState("");
  const [live, setLive] = useState(true);
  const [premium, setPremium] = useState(false);
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState<Entry[]>([]);

  async function ask(text: string) {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    let result: IntelligenceResponse;
    try {
      result = await api.intelligence({ question: value, live, premium });
    } catch {
      result = {
        question: value, answer: null, model: null, search: null, terms: [],
        since_days: null, citations: [], sources: [], cost_usd: 0,
        plan_cached: false, error: "Could not reach the server.",
        budget_remaining: 0,
      };
    }
    setLog((prev) => [{ ...result, key: Date.now() }, ...prev]);
    setBusy(false);
    setQuestion("");
  }

  const spent = log.reduce((sum, e) => sum + e.cost_usd, 0);

  return (
    <main className="intel">
      <div className="intel-intro">
        <h2>Ask across all six sources</h2>
        <p>
          Each question searches Indian Startup News, Entrackr, Inc42, YourStory,
          Sujata Chronicle and VCCircle as it is asked, then answers only from
          what they published — every claim numbered back to its story.
        </p>
      </div>

      <form
        className="intel-form"
        onSubmit={(e) => { e.preventDefault(); void ask(question); }}
      >
        <input
          type="search"
          value={question}
          placeholder="What is happening with Zepto's IPO?"
          aria-label="Your question"
          disabled={busy}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <button type="submit" disabled={busy || !question.trim()}>
          {busy ? "Searching" : "Ask"}
        </button>
      </form>

      <div className="intel-controls">
        <div className="chips">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              className="chip"
              disabled={busy}
              onClick={() => { setQuestion(example); void ask(example); }}
            >
              {example}
            </button>
          ))}
        </div>
        <label className="side-toggle">
          <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
          Search the sites live — free, adds a few seconds
        </label>
        <label className="side-toggle">
          <input
            type="checkbox"
            checked={premium}
            onChange={(e) => setPremium(e.target.checked)}
          />
          Better answers with Sonnet 4.6 — about 20× the cost
        </label>
      </div>

      {busy && (
        <div className="intel-pending" aria-live="polite">
          <span className="pulse" />
          {live ? "Searching six sites live, then reading what they found…" : "Reading the feed…"}
        </div>
      )}

      {log.map((entry) => <Answer key={entry.key} entry={entry} />)}

      {log.length === 0 && !busy && (
        <p className="side-note">
          Answers use only stories from these six sources. Anything a live search
          finds is added to the feed as well.
        </p>
      )}

      {spent > 0 && <p className="side-spent">This session: ${spent.toFixed(6)}</p>}
    </main>
  );
}
