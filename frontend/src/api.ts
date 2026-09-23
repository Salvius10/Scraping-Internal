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

export const api = {
  articles: (query: FeedQuery) => get<FeedPage>("/articles", { ...query }),
  facets: () => get<Facets>("/facets"),
  activity: () => get<{ days: ActivityDay[] }>("/activity"),
  status: () => get<Status>("/status"),
  filter: (phrase: string) => post<FilterResponse>("/filter", { phrase }),
  chat: (request: ChatRequest) => post<ChatResponse>("/chat", request),
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
 * #dbeaff is a surface tone, not a text tone -- it is invisible on white. The
 * quiet family therefore borrows the ink, while capital, ownership and risk
 * keep their own colour because all three read clearly at label size.
 */
export function categoryTextColor(category: string | null): string {
  const isQuiet = CATEGORY_VAR[category ?? "Other"] === "--cat-quiet";
  return isQuiet ? "var(--ink-soft)" : categoryColor(category);
}

/** Parse a timestamp from the API as UTC.
 *
 * The API sends naive ISO strings ("2026-09-23T07:56:38") whose values are
 * UTC. JavaScript parses an offset-less date-time as *local* time, so in
 * India every article read 5h30m earlier than it was published. Adding the
 * missing designator when there is none fixes it without touching the API.
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
