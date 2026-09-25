import { useState } from "react";
import CopyLink from "./CopyLink";
import { api, type ScrapeResponse, type WebResult, type WebSearchResponse } from "./api";

const EXAMPLES = [
  "Recently funded fintech startups",
  "Zepto IPO",
  "Indian EV startups raising Series B",
];

// Phosphor Icons (MIT), regular weight: "file-text" and "download-simple".
const FILE_TEXT =
  "M213.66,82.34l-56-56A8,8,0,0,0,152,24H56A16,16,0,0,0,40,40V216a16,16,0,0,0,16,16H200a16,16,0,0,0,16-16V88A8,8,0,0,0,213.66,82.34ZM160,51.31,188.69,80H160ZM200,216H56V40h88V88a8,8,0,0,0,8,8h48V216Zm-32-80a8,8,0,0,1-8,8H96a8,8,0,0,1,0-16h64A8,8,0,0,1,168,136Zm0,32a8,8,0,0,1-8,8H96a8,8,0,0,1,0-16h64A8,8,0,0,1,168,168Z";
const DOWNLOAD =
  "M224,144v64a8,8,0,0,1-8,8H40a8,8,0,0,1-8-8V144a8,8,0,0,1,16,0v56H208V144a8,8,0,0,1,16,0Zm-101.66,5.66a8,8,0,0,0,11.32,0l40-40a8,8,0,0,0-11.32-11.32L136,124.69V32a8,8,0,0,0-16,0v92.69L93.66,98.34a8,8,0,0,0-11.32,11.32Z";

function Icon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 256 256" width="15" height="15" fill="currentColor" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

/* A site's initial on one of the four brand colours, chosen by its name so
   the same site always gets the same badge. No third-party favicon calls. */
const BADGES = ["brand", "brand-2", "accent", "wash"] as const;
function badgeTone(domain: string) {
  let hash = 0;
  for (const ch of domain) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return BADGES[hash % BADGES.length];
}

/** Model output is light markdown: keep bullets, drop the markup. */
function Summary({ text }: { text: string }) {
  return (
    <div className="fc-summary-text">
      {text.split("\n").filter((l) => l.trim()).map((raw, i) => {
        const bullet = /^\s*[-*•]\s+/.test(raw);
        const line = raw.replace(/^\s*[-*•]\s+/, "").replace(/\*\*/g, "").replace(/^#+\s*/, "");
        return <p key={i} className={bullet ? "answer-bullet" : "answer-line"}>{line}</p>;
      })}
    </div>
  );
}

type ScrapeState = { busy: boolean; data?: ScrapeResponse };

function ResultRow({
  result, scrape, onScrape,
}: {
  result: WebResult;
  scrape: ScrapeState | undefined;
  onScrape: () => void;
}) {
  const data = scrape?.data;
  let path = result.url;
  try {
    const u = new URL(result.url);
    path = `${result.domain}${u.pathname === "/" ? "" : u.pathname}${u.search}`;
  } catch { /* keep raw */ }

  return (
    <li className="fc-result">
      <div className="fc-result-main">
        <span className="fc-badge" data-tone={badgeTone(result.domain)} aria-hidden="true">
          {result.domain.charAt(0).toUpperCase()}
        </span>
        <div className="fc-result-body">
          <h3 className="fc-title">
            <span className="fc-rank">#{result.n}</span>
            <a href={result.url} target="_blank" rel="noreferrer noopener">{result.title}</a>
          </h3>
          <div className="fc-url">
            <span>{path}</span>
            <CopyLink url={result.url} />
          </div>
          {result.description && <p className="fc-desc">{result.description}</p>}
        </div>
        <button
          className="fc-scrape"
          onClick={onScrape}
          disabled={scrape?.busy}
          aria-expanded={Boolean(data)}
        >
          <Icon d={FILE_TEXT} />
          {scrape?.busy ? "Scraping" : data ? "Scraped" : "Scrape page"}
        </button>
      </div>

      {scrape?.busy && (
        <div className="fc-scraping" aria-live="polite">
          <span className="pulse" />
          Reading this page and writing a summary
        </div>
      )}

      {data && (
        <div className="fc-scraped">
          {data.summary && (
            <section>
              <h4>Summary</h4>
              <Summary text={data.summary} />
            </section>
          )}
          {data.error && <p className="answer-error">{data.error}</p>}
          {data.content && (
            <details className="fc-content">
              <summary>
                Page content{data.content_truncated ? " (first part)" : ""}
              </summary>
              <pre>{data.content}</pre>
            </details>
          )}
          {!data.error && (
            <p className="answer-meta">
              {data.cached
                ? "Reused, no credits or budget spent"
                : `${data.credits_used} Firecrawl credit${data.credits_used === 1 ? "" : "s"}, $${data.cost_usd.toFixed(6)} for the summary`}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

export default function Intelligence() {
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState<WebSearchResponse | null>(null);
  const [view, setView] = useState<"results" | "json">("results");
  const [scrapes, setScrapes] = useState<Record<string, ScrapeState>>({});

  async function run(text: string) {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    setScrapes({});
    try {
      setSearch(await api.webSearch(value));
    } catch {
      setSearch({
        query: value, searched: null, results: [], credits_used: 0, cached: false,
        error: "Could not reach the server.",
      });
    }
    setView("results");
    setBusy(false);
  }

  async function scrapeOne(result: WebResult) {
    if (scrapes[result.url]?.busy || scrapes[result.url]?.data) return;
    setScrapes((prev) => ({ ...prev, [result.url]: { busy: true } }));
    let data: ScrapeResponse;
    try {
      data = await api.scrapePage(result.url, search?.query ?? "");
    } catch {
      data = {
        url: result.url, title: null, description: null, summary: null, content: null,
        content_truncated: false, model: null, cost_usd: 0, credits_used: 0,
        cached: false, error: "Could not reach the server.", budget_remaining: 0,
      };
    }
    setScrapes((prev) => ({ ...prev, [result.url]: { busy: false, data } }));
  }

  function downloadJson() {
    if (!search) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(search, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "search-results.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  const results = search?.results ?? [];

  return (
    <main className="intel" id="main">
      <div className="intel-intro intel-hero">
        <h2>Search the web for startup news</h2>
        <p>
          Results come back as a list. Nothing is read until you choose a page,
          then that page is scraped and summarised for you.
        </p>
      </div>

      <label className="field-label" htmlFor="intel-question">What are you looking for?</label>
      <form className="intel-form" onSubmit={(e) => { e.preventDefault(); void run(query); }}>
        <input
          id="intel-question"
          type="search"
          value={query}
          placeholder="Recently funded fintech startups"
          disabled={busy}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="submit" disabled={busy || !query.trim()}>
          {busy ? "Searching" : "Search"}
        </button>
      </form>

      <div className="intel-controls">
        <div className="chips">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              className="chip"
              disabled={busy}
              onClick={() => { setQuery(example); void run(example); }}
            >
              {example}
            </button>
          ))}
        </div>
      </div>

      {busy && (
        <div className="intel-pending" aria-live="polite">
          <span className="pulse" />
          Searching the web
        </div>
      )}

      {search && !busy && (
        <section className="fc-panel" aria-label="Search results">
          <header className="fc-head">
            <div>
              <h2>
                Results <span className="fc-count">({results.length})</span>
              </h2>
              <p>
                {search.searched && search.searched !== search.query
                  ? `Searched for “${search.searched}”. `
                  : ""}
                Choose Scrape page to read and summarise a result.
              </p>
            </div>
            <div className="fc-tools">
              <div className="fc-toggle" role="tablist" aria-label="View">
                <button role="tab" aria-selected={view === "results"} onClick={() => setView("results")}>
                  Results
                </button>
                <button role="tab" aria-selected={view === "json"} onClick={() => setView("json")}>
                  JSON
                </button>
              </div>
              <button className="fc-download" onClick={downloadJson} disabled={!results.length}>
                <Icon d={DOWNLOAD} />
                JSON
              </button>
            </div>
          </header>

          {search.error && <p className="answer-error fc-error">{search.error}</p>}

          {!search.error && results.length === 0 && (
            <p className="side-note fc-error">No results for that search. Try other words.</p>
          )}

          {view === "json" ? (
            <pre className="x-json fc-json">{JSON.stringify(search, null, 2)}</pre>
          ) : (
            <ol className="fc-results">
              {results.map((result) => (
                <ResultRow
                  key={result.url}
                  result={result}
                  scrape={scrapes[result.url]}
                  onScrape={() => void scrapeOne(result)}
                />
              ))}
            </ol>
          )}

          {!search.error && (
            <p className="fc-foot">
              {search.cached
                ? "Reused search, no credits spent"
                : `${search.credits_used} Firecrawl credit${search.credits_used === 1 ? "" : "s"} used`}
            </p>
          )}
        </section>
      )}
    </main>
  );
}
