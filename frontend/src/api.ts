/** Typed client for the dashboard API. */

export type Category =
  | "Funding" | "M&A" | "IPO" | "Product Launch" | "Policy/Regulation"
  | "Hiring/Layoffs" | "Shutdown" | "Partnership" | "Market/Analysis" | "Other";

export interface Article {
  id: number;
  headline: string;
  description: string | null;
  source: string;
  published_at: string | null;
  company: string | null;
  category: Category | null;
  url: string;
  also_reported_by: string[];
}

export interface FeedPage {
  articles: Article[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface Facet { value: string | null; count: number }
export interface Facets { categories: Facet[]; sources: Facet[] }
export interface ActivityDay { date: string; count: number }

export interface SourceHealth {
  name: string; label: string; strategy: string;
  last_run: string | null; items_seen: number; items_new: number;
  error: string | null; window_overflowed: boolean;
}

export interface Status {
  last_refresh: string | null;
  hours_since_refresh: number | null;
  refresh_interval_hours: number;
  next_refresh: string | null;
  article_count: number;
  newest_published: string | null;
  recency_window_days: number;
  sources: SourceHealth[];
  spend: {
    total_usd: number; total_cap: number; remaining_total: number;
    last_24h_usd: number; daily_cap: number; calls: number; exhausted: boolean;
  };
}

export interface FilterSpec {
  categories: Category[];
  sources: string[];
  company: string | null;
  query: string | null;
  since_days: number | null;
}

export interface FilterResponse {
  filter: FilterSpec;
  cached: boolean;
  cost_usd: number;
  explanation: string | null;
  error: string | null;
}

export interface ChatResponse {
  answer: string | null;
  model: string | null;
  cost_usd: number;
  error: string | null;
  budget_remaining: number;
}

export interface ChatRequest {
  mode: "explain" | "summarise" | "ask";
  article_id?: number;
  article_ids?: number[];
  question?: string;
  premium?: boolean;
}

export interface WebResult {
  n: number;
  url: string;
  title: string;
  description: string;
  domain: string;
  published: string | null;   // as Firecrawl words it: "3 hours ago"
}

export type SearchKind = "news" | "web";

export interface WebSearchResponse {
  query: string;
  kind: SearchKind;
  searched: string | null;
  results: WebResult[];
  credits_used: number;
  cached: boolean;
  error: string | null;
}

export interface ScrapeResponse {
  url: string;
  title: string | null;
  description: string | null;
  published_at: string | null;
  summary: string | null;
  content: string | null;
  content_truncated: boolean;
  model: string | null;
  cost_usd: number;
  credits_used: number;
  cached: boolean;
  error: string | null;
  budget_remaining: number;
}

export interface ExtractResponse {
  url: string;
  final_url: string | null;
  prompt: string;
  result: unknown;
  rows: Record<string, unknown>[] | null;
  cost_usd: number;
  calls: number;
  page_chars: number;
  truncated: boolean;
  cached: boolean;
  seconds: number;
  notes: string[];
  error: string | null;
  budget_remaining: number;
}

export interface Bucket {
  key: string;
  label: string;
  note: string;
  categories: Category[];
  days: number;
  count: number;
  new_count: number;
  latest_ingested_at: string | null;
}

export type StageKey = "pre-seed" | "seed" | "series-a" | "series-b" | "other";

export interface FundingRound {
  id: string;
  company: string | null;
  stage: string;
  round: string | null;
  amount: string | null;
  investors: string[];
  published_at: string | null;
  source: string;
  source_label: string;
  headline: string;
  url: string;
  origin: "feed" | "web" | "pasted";
  date_approx: boolean;
}

export interface SearchWebResponse {
  start: string | null;
  end: string | null;
  results_seen: number;
  added: Record<string, number>;
  total_added: number;
  duplicates: number;
  outside_range: number;
  not_startup_rounds: number;
  credits_used: number;
  cost_usd: number;
  failed_queries: string[];
  error: string | null;
}

/** Publish-date range, YYYY-MM-DD in Indian days; either end may be open. */
export interface DateRange {
  start?: string;
  end?: string;
}

export interface RoundsResponse {
  stage: StageKey;
  stages: { key: StageKey; label: string; count: number }[];
  rounds: FundingRound[];
  pending: number;
  sources: PastedSource[];      // websites pasted on this page
}

/** A website pasted on Startup firms or VC firms; each page keeps its own. */
export type PastedSection = "startups" | "vcs";

export interface PastedSource {
  id: number;
  key: string;                  // "pasted-3": its firm key on the VC page
  label: string;
  url: string;
  via: "rss" | "html" | "scrape";
  via_label: string;            // "its feed", "its news page", "its page, via Firecrawl"
  firm: string | null;          // the tracked firm it belongs to (VC firms only)
  firm_label: string | null;
  shown: number;                // rounds in the tabs, or VC posts that are not Other
  last_read: string | null;
  last_error: string | null;
}

export interface AddPastedSourceResponse {
  added: boolean;
  source_id: number | null;
  via: PastedSource["via"] | null;
  via_label: string | null;
  found: number;
  new: number;
  shown: number;
  credits_used: number;
  llm_usd: number;
  error: string | null;
}

export type VcKind = "Investment" | "Portfolio news" | "Fund news" | "Other";

export interface VcPost {
  id: string;
  firm: string;
  firm_label: string;
  kind: VcKind;
  headline: string;
  company: string | null;
  round: string | null;
  amount: string | null;
  published_at: string | null;
  date_approx: boolean;
  url: string;
  origin: VcOrigin;
  source_label: string;
}

export type VcOrigin = "site" | "added" | "search" | "news";

export interface VcRead {
  via: "rss" | "html" | "sitemap" | "scrape" | "map" | "search";
  label: string;
  is_site: boolean;
  last_read: string | null;
  last_error: string | null;
}

export interface VcFirm {
  key: string;
  label: string;
  home: string;
  count: number;
  reads: VcRead[];
  no_site: string | null;
  pasted: boolean;              // a pasted website shown as a firm of its own
}

export interface VcsResponse {
  firm: string | null;
  firms: VcFirm[];
  posts: VcPost[];
  pending: number;
  hidden_other: number;
  sources: PastedSource[];      // websites pasted on this page
}

export interface VcQuery extends DateRange {
  firm?: string;
  origin?: VcOrigin;
  all?: boolean;
}

export type EventWhen = "upcoming" | "past";

export interface EventItem {
  id: number;
  title: string;
  url: string;
  starts_at: string;
  ends_at: string | null;
  date_only: boolean;          // the source gave a day, no time
  timezone: string | null;
  venue: string | null;
  city: string | null;
  country: string | null;
  online: boolean | null;
  host: string | null;
  description: string | null;
  organiser: string;
  organiser_key: string;
  source_key: string;
  via_label: string;
}

export interface EventSourceInfo {
  key: string;
  label: string;
  url: string;
  via: "luma" | "page" | "html";
  via_label: string;
  firm: string | null;
  custom: boolean;             // pasted on the page, so it can be removed
  site: boolean;               // the firm's own home page, checked weekly
  id: number | null;
  india_only: boolean;
  note: string | null;
  events: number;
  last_read: string | null;
  last_error: string | null;
}

export interface EventsResponse {
  when: EventWhen;
  organiser: string | null;
  counts: Record<EventWhen, number>;
  events: EventItem[];
  organisers: { key: string; label: string; count: number }[];
  sources: EventSourceInfo[];
  // Every tracked firm, with how many event sources it has and, when known,
  // why it has none.
  firms: { key: string; label: string; sources: number; no_events: string | null }[];
  apify_ready: boolean;
  firecrawl_ready: boolean;
}

export interface EventsQuery extends DateRange {
  when: EventWhen;
  organiser?: string;
}

export interface AddEventSourceResponse {
  added: boolean;
  source_key: string | null;
  via: "luma" | "page" | "html" | null;
  found: number;
  new: number;
  apify_usd: number;
  credits_used: number;
  llm_usd: number;
  error: string | null;
}

/* LinkedIn: profiles and company pages the reader adds, read through Apify. */
export type LinkedinKind = "Funding" | "News" | "Other";
export type LinkedinKindKey = "funding" | "news";

export interface LinkedinPost {
  id: number;
  source_id: number;
  account: string;
  author: string | null;        // who wrote it, when the account reposted it
  repost: boolean;
  kind: LinkedinKind;
  headline: string;
  snippet: string;              // the post's own words, cut short
  company: string | null;
  round: string | null;
  amount: string | null;
  investors: string[];
  posted_at: string | null;
  url: string;
}

export interface LinkedinSource {
  id: number;
  label: string;
  url: string;
  kind: "profile" | "company";
  kind_label: string;
  count: number;                // posts shown for the current filters
  posts: number;                // every post read from it
  last_read: string | null;
  last_error: string | null;
}

export interface LinkedinResponse {
  source: number | null;
  kind: LinkedinKindKey | null;
  counts: Record<"funding" | "news" | "other", number>;
  posts: LinkedinPost[];
  sources: LinkedinSource[];
  pending: number;
  hidden_other: number;
  apify_ready: boolean;
}

export interface LinkedinQuery extends DateRange {
  source?: number;
  kind?: LinkedinKindKey;
  all?: boolean;
}

export interface AddLinkedinSourceResponse {
  added: boolean;
  source_id: number | null;
  label: string | null;
  found: number;
  new: number;
  funding: number;
  news: number;
  apify_usd: number;
  llm_usd: number;
  error: string | null;
}

export type RefreshScope = "feed" | "vcs" | "events" | "linkedin";

export interface RefreshStatus {
  scope: RefreshScope | null;
  running: boolean;
  started_at: string | null;
  finished_at: string | null;
  summary: string | null;
  error: string | null;
}

export interface RefreshStart {
  started: boolean;
  reason: string | null;
  status: RefreshStatus;
}

export interface FeedQuery {
  limit?: number;
  offset?: number;
  days?: number;
  category?: Category[];
  source?: string[];
  company?: string;
  q?: string;
}

type QueryParams = Record<string, unknown>;

async function get<T>(path: string, params?: QueryParams): Promise<T> {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)));
    else search.append(key, String(value));
  }
  const query = search.toString();
  const response = await fetch(`/api${path}${query ? `?${query}` : ""}`);
  if (!response.ok) {
    throw new Error(`${path} failed (${response.status})`);
  }
  return (await response.json()) as T;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${path} failed (${response.status})`);
  return (await response.json()) as T;
}

async function del<T>(path: string): Promise<T> {
  const response = await fetch(`/api${path}`, { method: "DELETE" });
  if (!response.ok) throw new Error(`${path} failed (${response.status})`);
  return (await response.json()) as T;
}

export const api = {
  articles: (query: FeedQuery) => get<FeedPage>("/articles", { ...query }),
  facets: () => get<Facets>("/facets"),
  // Days are bucketed in the reader's timezone, matching the tape below.
  activity: () => get<{ days: ActivityDay[] }>("/activity", {
    tz_offset: -new Date().getTimezoneOffset(),
  }),
  status: () => get<Status>("/status"),
  // seen: bucket key -> ISO time the reader last opened it, for "+N new".
  buckets: (seen: Record<string, string>) =>
    get<{ buckets: Bucket[] }>("/buckets", {
      seen: Object.entries(seen).map(([key, at]) => `${key}@${at}`),
    }),
  filter: (phrase: string) => post<FilterResponse>("/filter", { phrase }),
  chat: (request: ChatRequest) => post<ChatResponse>("/chat", request),
  searchWebRounds: (start: string, end?: string) =>
    post<SearchWebResponse>("/insights/search-web", { start, end: end || undefined }),
  rounds: (stage: StageKey, range: DateRange = {}) =>
    get<RoundsResponse>("/insights/rounds", { stage, ...range }),
  // A plain link, so the browser handles the file download itself.
  roundsExcelUrl: (stage?: StageKey, range: DateRange = {}) => {
    const params = new URLSearchParams();
    if (stage) params.set("stage", stage);
    if (range.start) params.set("start", range.start);
    if (range.end) params.set("end", range.end);
    const query = params.toString();
    return `/api/insights/rounds.xlsx${query ? `?${query}` : ""}`;
  },
  refresh: (scope: RefreshScope) => post<RefreshStart>("/refresh", { scope }),
  refreshStatus: () => get<RefreshStatus>("/refresh"),
  vcs: (query: VcQuery) => get<VcsResponse>("/insights/vcs", { ...query, all: query.all || undefined }),
  vcsExcelUrl: (query: VcQuery) => {
    const params = new URLSearchParams();
    if (query.firm) params.set("firm", query.firm);
    if (query.start) params.set("start", query.start);
    if (query.end) params.set("end", query.end);
    if (query.origin) params.set("origin", query.origin);
    if (query.all) params.set("all", "true");
    const q = params.toString();
    return `/api/insights/vcs.xlsx${q ? `?${q}` : ""}`;
  },
  events: (query: EventsQuery) => get<EventsResponse>("/insights/events", { ...query }),
  eventsExcelUrl: (query: EventsQuery) => {
    const params = new URLSearchParams({ when: query.when });
    if (query.organiser) params.set("organiser", query.organiser);
    if (query.start) params.set("start", query.start);
    if (query.end) params.set("end", query.end);
    return `/api/insights/events.xlsx?${params.toString()}`;
  },
  // Paid: the source is read straight away (Apify for Luma, else Firecrawl).
  addEventSource: (url: string, label?: string, firm?: string) =>
    post<AddEventSourceResponse>("/insights/events/sources", {
      url, label: label || undefined, firm: firm || undefined,
    }),
  removeEventSource: (id: number) =>
    del<{ removed: boolean }>(`/insights/events/sources/${id}`),
  // Read straight away: free for a feed or a plain news page, else 1 Firecrawl
  // credit; then gpt-oss reads the new posts.
  addSource: (section: PastedSection, url: string, label?: string, firm?: string) =>
    post<AddPastedSourceResponse>("/insights/sources", {
      section, url, label: label || undefined, firm: firm || undefined,
    }),
  removeSource: (id: number) => del<{ removed: boolean }>(`/insights/sources/${id}`),
  linkedin: (query: LinkedinQuery) =>
    get<LinkedinResponse>("/linkedin", { ...query, all: query.all || undefined }),
  linkedinExcelUrl: (query: LinkedinQuery) => {
    const params = new URLSearchParams();
    if (query.source !== undefined) params.set("source", String(query.source));
    if (query.kind) params.set("kind", query.kind);
    if (query.start) params.set("start", query.start);
    if (query.end) params.set("end", query.end);
    if (query.all) params.set("all", "true");
    const q = params.toString();
    return `/api/linkedin.xlsx${q ? `?${q}` : ""}`;
  },
  // Paid: read straight away through Apify (~$0.04 for the newest 20 posts),
  // then gpt-oss reads the posts.
  addLinkedinSource: (url: string, label?: string) =>
    post<AddLinkedinSourceResponse>("/linkedin/sources", { url, label: label || undefined }),
  removeLinkedinSource: (id: number) => del<{ removed: boolean }>(`/linkedin/sources/${id}`),
  webSearch: (query: string, kind: SearchKind) =>
    post<WebSearchResponse>("/intelligence/search", { query, kind }),
  scrapePage: (url: string, query: string) =>
    post<ScrapeResponse>("/intelligence/scrape", { url, query }),
  extract: (url: string, prompt: string) =>
    post<ExtractResponse>("/extract", { url, prompt }),
};

/* Four categories families, one per brand colour. A reader scanning deal flow
   wants to know whether something is capital, ownership or trouble; the exact
   category is already written next to the rule, so colour need not carry it. */
export const CATEGORY_VAR: Record<string, string> = {
  // Capital events -- #1a00d9
  "Funding": "--cat-capital",
  "IPO": "--cat-capital",
  // Ownership and alliances -- #5e9eff
  "M&A": "--cat-ownership",
  "Partnership": "--cat-ownership",
  // Risk -- #fe6e06
  "Shutdown": "--cat-risk",
  "Policy/Regulation": "--cat-risk",
  // Everything else -- #dbeaff
  "Product Launch": "--cat-quiet",
  "Hiring/Layoffs": "--cat-quiet",
  "Market/Analysis": "--cat-quiet",
  "Other": "--cat-quiet",
};

export function categoryColor(category: string | null): string {
  return `var(${CATEGORY_VAR[category ?? "Other"] ?? "--cat-quiet"})`;
}

/** Colour for a category rendered as *text*.
 *
 * Bars and rules use the brand colours as they are. Text cannot: #5e9eff and
 * #fe6e06 read at under 3:1 on white and #dbeaff is invisible, so each family
 * maps to a shade of the same hue that passes WCAG AA.
 */
const CATEGORY_INK: Record<string, string> = {
  "--cat-capital": "var(--brand)",
  "--cat-ownership": "var(--brand-2-ink)",
  "--cat-risk": "var(--accent-ink)",
  "--cat-quiet": "var(--ink-mid)",
};

export function categoryTextColor(category: string | null): string {
  return CATEGORY_INK[CATEGORY_VAR[category ?? "Other"] ?? "--cat-quiet"];
}

/** Parse a timestamp from the API as UTC.
 *
 * The API now sends an explicit offset ("2026-09-23T07:56:38Z"), fixed at the
 * ORM layer. It used to send naive strings, which JavaScript parses as
 * *local* time -- every article read 5h30m early in India -- so a missing
 * designator is still treated as UTC rather than trusted to be local.
 */
export function parseTime(iso: string): Date {
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`);
}

const SOURCE_LABELS: Record<string, string> = {
  indianstartupnews: "Indian Startup News",
  entrackr: "Entrackr",
  inc42: "Inc42",
  yourstory: "YourStory",
  sujatachronicle: "Sujata Chronicle",
  vccircle: "VCCircle",
};

export function sourceLabel(name: string): string {
  return SOURCE_LABELS[name] ?? name;
}
