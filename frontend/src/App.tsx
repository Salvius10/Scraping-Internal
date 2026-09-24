import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CopyLink from "./CopyLink";
import ExtractPanel from "./ExtractPanel";
import Intelligence from "./Intelligence";
import Sidebar from "./Sidebar";
import {
  api, categoryColor, categoryTextColor, parseTime, sourceLabel,
  type ActivityDay, type Article, type Category, type Facets,
  type FilterSpec, type Status,
} from "./api";

const IST = "Asia/Kolkata";
const PAGE = 30;

const timeFmt = new Intl.DateTimeFormat("en-IN", {
  hour: "2-digit", minute: "2-digit", hour12: false, timeZone: IST,
});
const dayFmt = new Intl.DateTimeFormat("en-IN", {
  weekday: "short", day: "numeric", month: "short", timeZone: IST,
});
const shortDayFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric", timeZone: IST,
});

function dayKey(iso: string | null): string {
  if (!iso) return "undated";
  return parseTime(iso).toLocaleDateString("en-CA", { timeZone: IST });
}

function describeAge(hours: number | null, interval: number): string {
  if (hours === null) return "Not refreshed yet";
  if (hours < 1) return `Refreshed ${Math.max(1, Math.round(hours * 60))} min ago`;
  if (hours < 24) return `Refreshed ${Math.round(hours)}h ago`;
  const days = Math.floor(hours / 24);
  return `Refreshed ${days} day${days > 1 ? "s" : ""} ago — expected every ${interval}h`;
}

type View = "feed" | "intelligence";

function viewFromHash(): View {
  return window.location.hash === "#intelligence" ? "intelligence" : "feed";
}

/* ── Masthead ──────────────────────────────────────────────────────────── */

function Masthead({
  status, activity, activeDay, onPickDay, view, onView,
}: {
  status: Status | null;
  activity: ActivityDay[];
  activeDay: string | null;
  onPickDay: (date: string | null) => void;
  view: View;
  onView: (view: View) => void;
}) {
  const peak = Math.max(1, ...activity.map((d) => d.count));
  const stale =
    status?.hours_since_refresh != null &&
    status.hours_since_refresh > status.refresh_interval_hours;
  const next = status?.next_refresh
    ? `Next refresh ${timeFmt.format(parseTime(status.next_refresh))} IST`
    : undefined;

  return (
    <header className="masthead">
      <div className="masthead-left">
        <h1 className="wordmark">
          Dealflow <span>India startup ecosystem</span>
        </h1>
        <nav className="views" aria-label="Sections">
          <button aria-current={view === "feed" ? "page" : undefined} onClick={() => onView("feed")}>
            News feed
          </button>
          <button
            aria-current={view === "intelligence" ? "page" : undefined}
            onClick={() => onView("intelligence")}
          >
            Intelligence
          </button>
        </nav>
      </div>

      {view === "feed" && activity.length > 0 && (
        <div className="activity" role="group" aria-label="Stories per day">
          {activity.map((day) => {
            const active = activeDay === day.date;
            return (
              <button
                key={day.date}
                className="activity-day"
                aria-pressed={active}
                title={`${day.count} ${day.count === 1 ? "story" : "stories"} on ${day.date}`}
                onClick={() => onPickDay(active ? null : day.date)}
              >
                <span
                  className="activity-bar"
                  style={{ height: `${Math.round((day.count / peak) * 26) + 2}px` }}
                />
                <span className="activity-label">
                  {shortDayFmt.format(new Date(`${day.date}T12:00:00Z`))}
                </span>
              </button>
            );
          })}
        </div>
      )}

      <div className="freshness" data-stale={stale} title={next}>
        <span className="pulse" />
        {status
          ? `${describeAge(status.hours_since_refresh, status.refresh_interval_hours)} · ${status.article_count} stories`
          : "Loading"}
      </div>
    </header>
  );
}

/* ── Filter rail ───────────────────────────────────────────────────────── */

function Rail({
  facets, categories, sources, onToggleCategory, onToggleSource, onClear, filtered,
}: {
  facets: Facets | null;
  categories: Category[];
  sources: string[];
  onToggleCategory: (c: Category) => void;
  onToggleSource: (s: string) => void;
  onClear: () => void;
  filtered: boolean;
}) {
  return (
    <nav className="rail" aria-label="Filters">
      <div className="rail-group">
        <h2>Category</h2>
        {facets?.categories.map((facet) => {
          const value = (facet.value ?? "Other") as Category;
          return (
            <button
              key={value}
              className="rail-item"
              aria-pressed={categories.includes(value)}
              onClick={() => onToggleCategory(value)}
            >
              <span
                className="swatch"
                style={{ background: categoryColor(value) }}
              />
              {value}
              <span className="count">{facet.count}</span>
            </button>
          );
        })}
      </div>

      <div className="rail-group">
        <h2>Source</h2>
        {facets?.sources.map((facet) => (
          <button
            key={facet.value ?? ""}
            className="rail-item"
            aria-pressed={sources.includes(facet.value ?? "")}
            onClick={() => onToggleSource(facet.value ?? "")}
          >
            {sourceLabel(facet.value ?? "")}
            <span className="count">{facet.count}</span>
          </button>
        ))}
      </div>

      {filtered && (
        <button className="rail-clear" onClick={onClear}>
          Clear filters
        </button>
      )}
    </nav>
  );
}

/* ── Entry ─────────────────────────────────────────────────────────────── */

function Entry({
  article, selected, onSelect,
}: {
  article: Article;
  selected: boolean;
  onSelect: (a: Article) => void;
}) {
  const hue = categoryColor(article.category);
  const others = article.also_reported_by.length;

  return (
    <article className="entry" data-selected={selected}>
      <div className="entry-time">
        {article.published_at ? timeFmt.format(parseTime(article.published_at)) : "—"}
      </div>
      <div className="entry-rule" style={{ background: hue }} />
      <div className="entry-body">
        {/* Where it was reported -- the attribution sits with the story. */}
        <div className="entry-kicker">
          <span className="entry-source">{sourceLabel(article.source)}</span>
          {others > 0 && (
            <span
              className="corroboration"
              title={`Also reported by ${article.also_reported_by.map(sourceLabel).join(", ")}`}
            >
              +{others} {others === 1 ? "outlet" : "outlets"}
            </span>
          )}
          <CopyLink url={article.url} />
        </div>
        <a
          className="entry-headline"
          href={article.url}
          target="_blank"
          rel="noreferrer noopener"
        >
          {article.headline}
        </a>
        {article.description && <p className="entry-desc">{article.description}</p>}
      </div>
      <div className="entry-meta">
        <span
          className="entry-cat"
          style={{ color: categoryTextColor(article.category) }}
        >
          {article.category ?? "Other"}
        </span>
        <button className="entry-ask" onClick={() => onSelect(article)}>
          Explain
        </button>
      </div>
    </article>
  );
}

/* ── App ───────────────────────────────────────────────────────────────── */

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [facets, setFacets] = useState<Facets | null>(null);
  const [activity, setActivity] = useState<ActivityDay[]>([]);

  const [articles, setArticles] = useState<Article[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [categories, setCategories] = useState<Category[]>([]);
  const [sources, setSources] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [activeDay, setActiveDay] = useState<string | null>(null);
  const [company, setCompany] = useState<string | undefined>(undefined);
  const [days, setDays] = useState<number | undefined>(undefined);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [extractOpen, setExtractOpen] = useState(false);
  const [selected, setSelected] = useState<Article | null>(null);
  const [filterNote, setFilterNote] = useState<string | null>(null);
  const [view, setView] = useState<View>(viewFromHash);

  const offsetRef = useRef(0);

  // The section lives in the URL hash so a reload or a shared link keeps it.
  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const showView = (next: View) => {
    if (next === viewFromHash()) return;
    if (next === "intelligence") window.location.hash = "intelligence";
    else window.history.pushState(null, "", window.location.pathname + window.location.search);
    setView(next);
  };

  useEffect(() => {
    api.status().then(setStatus).catch(() => undefined);
    api.facets().then(setFacets).catch(() => undefined);
    api.activity().then((r) => setActivity(r.days)).catch(() => undefined);
  }, []);

  // Debounce typing so every keystroke is not a request.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 250);
    return () => clearTimeout(timer);
  }, [search]);

  const load = useCallback(
    async (append: boolean) => {
      setLoading(true);
      setError(null);
      const offset = append ? offsetRef.current : 0;
      try {
        const page = await api.articles({
          limit: PAGE, offset, category: categories, source: sources,
          q: query || undefined, company, days,
        });
        offsetRef.current = offset + page.articles.length;
        setArticles((prev) => (append ? [...prev, ...page.articles] : page.articles));
        setTotal(page.total);
        setHasMore(page.has_more);
      } catch {
        setError("Could not reach the server. Is the backend running on port 8000?");
      } finally {
        setLoading(false);
      }
    },
    [categories, sources, query, company, days],
  );

  useEffect(() => {
    offsetRef.current = 0;
    void load(false);
  }, [load]);

  const visible = useMemo(
    () => (activeDay ? articles.filter((a) => dayKey(a.published_at) === activeDay) : articles),
    [articles, activeDay],
  );

  const grouped = useMemo(() => {
    const groups: { key: string; label: string; items: Article[] }[] = [];
    for (const article of visible) {
      const key = dayKey(article.published_at);
      let group = groups.at(-1);
      if (!group || group.key !== key) {
        group = {
          key,
          label: key === "undated"
            ? "No date given"
            : dayFmt.format(new Date(`${key}T12:00:00Z`)),
          items: [],
        };
        groups.push(group);
      }
      group.items.push(article);
    }
    return groups;
  }, [visible]);

  const filtered =
    categories.length > 0 || sources.length > 0 || query !== "" ||
    activeDay !== null || company !== undefined || days !== undefined;

  const clear = () => {
    setCategories([]); setSources([]); setSearch(""); setActiveDay(null);
    setCompany(undefined); setDays(undefined); setFilterNote(null);
  };

  /** Apply a filter the assistant compiled from a phrase. */
  const applySpec = (spec: FilterSpec, explanation: string | null) => {
    setCategories(spec.categories ?? []);
    setSources(spec.sources ?? []);
    setCompany(spec.company ?? undefined);
    setDays(spec.since_days ?? undefined);
    setSearch(spec.query ?? "");
    setActiveDay(null);
    setFilterNote(explanation);
  };

  const selectArticle = (article: Article) => {
    setSelected(article);
    setSidebarOpen(true);
  };

  const toggle = <T,>(list: T[], value: T) =>
    list.includes(value) ? list.filter((v) => v !== value) : [...list, value];

  if (view === "intelligence") {
    return (
      <div className="shell" data-view="intelligence">
        <Masthead
          status={status}
          activity={activity}
          activeDay={activeDay}
          onPickDay={setActiveDay}
          view={view}
          onView={showView}
        />
        <Intelligence />
      </div>
    );
  }

  return (
    <div className="shell" data-sidebar={sidebarOpen ? "open" : "closed"}>
      <Masthead
        status={status}
        activity={activity}
        activeDay={activeDay}
        onPickDay={setActiveDay}
        view={view}
        onView={showView}
      />

      <Rail
        facets={facets}
        categories={categories}
        sources={sources}
        onToggleCategory={(c) => setCategories((prev) => toggle(prev, c))}
        onToggleSource={(s) => setSources((prev) => toggle(prev, s))}
        onClear={clear}
        filtered={filtered}
      />

      <main className="tape">
        <div className="searchbar">
          <input
            type="search"
            value={search}
            placeholder="Search companies, headlines, investors"
            aria-label="Search stories"
            onChange={(e) => setSearch(e.target.value)}
          />
          <button
            className="ask-toggle"
            aria-pressed={extractOpen}
            onClick={() => setExtractOpen((open) => !open)}
          >
            Extract from a URL
          </button>
          <button
            className="ask-toggle"
            aria-pressed={sidebarOpen}
            onClick={() => setSidebarOpen((open) => !open)}
          >
            {sidebarOpen ? "Hide assistant" : "Ask"}
          </button>
        </div>

        {/* Kept mounted while hidden so results survive closing the panel. */}
        <div hidden={!extractOpen}>
          <ExtractPanel onClose={() => setExtractOpen(false)} />
        </div>

        {filterNote && (
          <p className="filter-note">
            {filterNote}
            <button onClick={clear}>clear</button>
          </p>
        )}

        {error && (
          <div className="notice">
            <h2>Nothing is loading</h2>
            <p>{error}</p>
          </div>
        )}

        {!error && loading && articles.length === 0 && (
          <div aria-busy="true" aria-label="Loading stories">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="skeleton" />
            ))}
          </div>
        )}

        {!error && !loading && visible.length === 0 && (
          <div className="notice">
            <h2>No stories match</h2>
            <p>
              {filtered
                ? "Nothing here fits the current filters."
                : `Nothing has been ingested yet. Run the pipeline to fill the feed.`}
            </p>
            {filtered && (
              <button className="rail-clear" onClick={clear}>
                Clear filters
              </button>
            )}
          </div>
        )}

        {grouped.map((group) => (
          <section key={group.key}>
            <h2 className="daymark">
              {group.label}
              <span className="n">
                {group.items.length} {group.items.length === 1 ? "story" : "stories"}
              </span>
            </h2>
            <div className="day-panel">
              {group.items.map((article) => (
                <Entry
                  key={article.id}
                  article={article}
                  selected={selected?.id === article.id}
                  onSelect={selectArticle}
                />
              ))}
            </div>
          </section>
        ))}

        {hasMore && !activeDay && (
          <button className="more" onClick={() => void load(true)} disabled={loading}>
            {loading ? "Loading" : `Load more — ${total - articles.length} older stories`}
          </button>
        )}
      </main>

      {sidebarOpen && (
        <Sidebar
          visible={visible}
          selected={selected}
          onClearSelection={() => setSelected(null)}
          onApplyFilter={applySpec}
          onClose={() => setSidebarOpen(false)}
        />
      )}
    </div>
  );
}
