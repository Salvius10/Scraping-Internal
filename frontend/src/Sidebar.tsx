import { useState } from "react";
import { api, type Article, type ChatResponse, type FilterSpec } from "./api";

interface Props {
  visible: Article[];
  selected: Article | null;
  onClearSelection: () => void;
  onApplyFilter: (spec: FilterSpec, explanation: string | null) => void;
  onClose: () => void;
}

type Entry = {
  id: number;
  question: string;
  answer: string | null;
  error: string | null;
  model: string | null;
  cost: number;
};

const FILTER_EXAMPLES = [
  "funding rounds this week",
  "IPO news from Entrackr",
  "anything about Zepto",
];

/** Turn the model's light markdown into plain readable lines. */
function renderAnswer(text: string) {
  return text.split("\n").filter((line) => line.trim()).map((line, i) => {
    const bullet = /^\s*[-*]\s+/.test(line);
    const clean = line.replace(/^\s*[-*]\s+/, "").replace(/\*\*/g, "");
    return (
      <p key={i} className={bullet ? "answer-bullet" : "answer-line"}>
        {clean}
      </p>
    );
  });
}

export default function Sidebar({
  visible, selected, onClearSelection, onApplyFilter, onClose,
}: Props) {
  const [phrase, setPhrase] = useState("");
  const [filterNote, setFilterNote] = useState<string | null>(null);
  const [filtering, setFiltering] = useState(false);

  const [question, setQuestion] = useState("");
  const [log, setLog] = useState<Entry[]>([]);
  const [busy, setBusy] = useState(false);
  const [premium, setPremium] = useState(false);
  const [spent, setSpent] = useState(0);

  async function runFilter(text: string) {
    const value = text.trim();
    if (!value || filtering) return;
    setFiltering(true);
    setFilterNote(null);
    try {
      const result = await api.filter(value);
      if (result.error) {
        setFilterNote(result.error);
      } else {
        onApplyFilter(result.filter, result.explanation);
        setFilterNote(
          `${result.explanation ?? ""}${result.cached ? " (cached, free)" : ""}`,
        );
        setSpent((prev) => prev + result.cost_usd);
      }
    } catch {
      setFilterNote("Could not reach the server.");
    } finally {
      setFiltering(false);
    }
  }

  async function ask(mode: "explain" | "summarise" | "ask", prompt?: string) {
    if (busy) return;
    setBusy(true);
    const label =
      mode === "explain" ? `Explain: ${selected?.headline ?? ""}`
      : mode === "summarise" ? `Summarise ${visible.length} stories on screen`
      : prompt ?? "";

    let result: ChatResponse;
    try {
      result = await api.chat({
        mode,
        article_id: mode === "explain" ? selected?.id : undefined,
        article_ids: mode === "explain" ? [] : visible.map((a) => a.id),
        question: mode === "ask" ? prompt : undefined,
        premium,
      });
    } catch {
      result = {
        answer: null, model: null, cost_usd: 0,
        error: "Could not reach the server.", budget_remaining: 0,
      };
    }

    setLog((prev) => [
      ...prev,
      {
        id: Date.now(), question: label, answer: result.answer,
        error: result.error, model: result.model, cost: result.cost_usd,
      },
    ]);
    setSpent((prev) => prev + result.cost_usd);
    setBusy(false);
    setQuestion("");
  }

  return (
    <aside className="sidebar" aria-label="Assistant">
      <header className="sidebar-head">
        <div>
          <h2>Ask</h2>
          <p>Filter, summarise or question the stories in the feed.</p>
        </div>
        <button className="sidebar-close" onClick={onClose} aria-label="Close assistant">
          Close
        </button>
      </header>

      <div className="sidebar-body">
        {/* Filtering in words. Compiles to a query, so cost does not grow
            with the size of the feed. */}
        <section className="side-block">
          <label className="side-label" htmlFor="side-filter">Filter in your own words</label>
          <form
            className="side-field"
            onSubmit={(e) => { e.preventDefault(); void runFilter(phrase); }}
          >
            <input
              id="side-filter"
              className="side-input"
              value={phrase}
              placeholder="funding rounds this week"
              onChange={(e) => setPhrase(e.target.value)}
            />
            <button type="submit" className="side-go" disabled={filtering || !phrase.trim()}>
              Apply
            </button>
          </form>
          <div className="chips">
            {FILTER_EXAMPLES.map((example) => (
              <button
                key={example}
                className="chip"
                onClick={() => { setPhrase(example); void runFilter(example); }}
              >
                {example}
              </button>
            ))}
          </div>
          {filtering && <p className="side-note">Reading that</p>}
          {filterNote && !filtering && <p className="side-status">{filterNote}</p>}
        </section>

        <section className="side-block">
          <span className="side-label">The stories on screen</span>
          <div className="side-actions">
            <button
              className="side-primary"
              disabled={busy || visible.length === 0}
              onClick={() => void ask("summarise")}
            >
              Summarise {visible.length} stories
            </button>
            {selected && (
              <button
                className="side-secondary"
                disabled={busy}
                onClick={() => void ask("explain")}
              >
                Explain selected
              </button>
            )}
          </div>

          {selected && (
            <p className="side-selected">
              {selected.headline}
              <button className="side-unselect" onClick={onClearSelection}>
                clear
              </button>
            </p>
          )}

          <label className="side-label" htmlFor="side-question">Ask a question</label>
          <form
            className="side-field"
            onSubmit={(e) => { e.preventDefault(); void ask("ask", question); }}
          >
            <input
              id="side-question"
              className="side-input"
              value={question}
              placeholder="Which of these are Series B or later?"
              disabled={busy}
              onChange={(e) => setQuestion(e.target.value)}
            />
            <button type="submit" className="side-go" disabled={busy || !question.trim()}>
              Ask
            </button>
          </form>

          <label className="side-switch">
            <input
              type="checkbox"
              role="switch"
              checked={premium}
              onChange={(e) => setPremium(e.target.checked)}
            />
            <span className="side-switch-track" aria-hidden="true" />
            <span>
              <strong>Sonnet 4.6</strong>
              <span className="side-switch-note">Better answers, about 20x the cost</span>
            </span>
          </label>
        </section>

        <section className="side-log" aria-live="polite">
          {busy && (
            <div className="side-thinking">
              <span className="pulse" />
              Thinking
            </div>
          )}
          {[...log].reverse().map((entry) => (
            <article key={entry.id} className="answer">
              <h4>{entry.question}</h4>
              {entry.error
                ? <p className="answer-error">{entry.error}</p>
                : entry.answer && renderAnswer(entry.answer)}
              {!entry.error && (
                <p className="answer-meta">
                  {entry.model?.includes("sonnet") ? "Sonnet 4.6" : "gpt-oss"} · $
                  {entry.cost.toFixed(6)}
                </p>
              )}
            </article>
          ))}
        </section>

        {spent > 0 && (
          <p className="side-spent">This session: ${spent.toFixed(6)}</p>
        )}
      </div>
    </aside>
  );
}
