/** Typed client for the platform API. */

export interface SourceRef {
  url: string;
  source: string;
  type: string;
  published_at: string | null;
}

export type StageKey = "pre-seed" | "seed" | "series-a" | "series-b" | "other";
export type SourceType = "rss" | "website" | "web_search" | "linkedin";
export type AddKind = "linkedin" | "website" | "search";

export interface SourceInfo {
  key: string;
  label: string;
  type: string;
  detail: string;
  via: "rss" | "scrape" | null;
  id: number | null;            // set when added on the page: removable
  every_hours: number;
  paid: boolean;
  last_read: string | null;
  last_items: number;
  last_error: string | null;
  last_skipped: string | null;
  warning: string | null;
}

export interface SourcesResponse {
  sources: SourceInfo[];
  added: number;
  max_added: number;
  apify_ready: boolean;
  firecrawl_ready: boolean;
  registry_error: string | null;
}

export interface AddSourceResponse {
  added: boolean;
  message: string;
  source: SourceInfo | null;
}

export interface FundingRow {
  id: number;
  company: string;
  stage: string;
  round: string | null;
  amount: string | null;
  investors: string[];
  sector: string | null;
  hq_city: string | null;
  summary: string | null;
  headline: string;
  source_url: string;
  source_label: string;
  source_type: string;
  published_at: string | null;
  sources: SourceRef[];
  first_seen_at: string;
}

export interface FundingPage {
  rows: FundingRow[];
  total: number;
  limit: number;
  offset: number;
  stages: { key: StageKey; label: string; count: number }[];
}

export type When = "upcoming" | "past" | "all";

export interface EventRow {
  id: number;
  title: string;
  starts_at: string;
  ends_at: string | null;
  date_only: boolean;
  city: string | null;
  venue: string | null;
  online: boolean | null;
  organiser: string | null;
  category: string | null;
  price: string | null;
  summary: string | null;
  url: string;
  source_url: string;
  source_label: string;
  source_type: string;
  sources: SourceRef[];
  first_seen_at: string;
}

export interface EventsPage {
  rows: EventRow[];
  total: number;
  limit: number;
  offset: number;
  cities: { name: string; count: number }[];
  upcoming: number;
  past: number;
}

export type DesignationKey =
  | "cofounder" | "ceo" | "cto" | "coo" | "vp-engineering" | "head-engineering" | "head-product";

export interface ContactRow {
  id: number;
  full_name: string;
  designation: string;          // "Co-founder, CEO"
  title: string | null;         // as written on the profile
  company: string;
  company_id: number;
  email: string | null;
  phone: string | null;
  linkedin: string | null;
  other_emails: string[];
  other_phones: string[];
  location: string | null;
  company_page: string | null;  // LinkedIn page of the company they work at
  company_site: string | null;
  maybe_other: boolean;         // may be at another company with the same name
  stage: string | null;
  sector: string | null;
  hq_city: string | null;
  lookup: string;               // SignalHire's answer: "success", "failed", ...
  looked_up_at: string;
}

export interface ContactsPage {
  rows: ContactRow[];
  total: number;
  all_designations: number;     // the same search, every designation
  limit: number;
  offset: number;
  designations: { key: DesignationKey; label: string; count: number }[];
}

export interface EnrichmentStatus {
  ready: boolean;
  running: boolean;
  started_at: string | null;
  finished_at: string | null;
  current: string | null;
  companies: number;
  waiting: number;
  done: number;
  no_match: number;
  failed: number;
  contacts: number;
  reachable: number;
  credits_used: number;
  credits_left: number | null;
  per_company: number;
  message: string | null;
  error: string | null;
}

export type CompanyStatus = "queued" | "working" | "done" | "no_match" | "failed";

export interface EnrichedCompany {
  id: number;
  company: string;
  stage: string | null;
  amount: string | null;
  sector: string | null;
  hq_city: string | null;
  status: CompanyStatus;
  note: string | null;
  people_found: number;
  credits_used: number;
  requested_at: string;
  enriched_at: string | null;
}

export interface EnrichmentPreview {
  ready: boolean;
  companies: number;
  new: number;
  waiting: number;
  enriched: number;
  over_cap: number;
  max_credits: number;
  per_company: number;
  credits_left: number | null;
}

export interface EnrichmentSend {
  sent: boolean;
  message: string;
  queued: number;
  waiting: number;
  enriched: number;
  over_cap: number;
  status: EnrichmentStatus;
}

export interface EnrichmentRun {
  started: boolean;
  message: string;
  status: EnrichmentStatus;
}

export interface EnrichmentClear {
  cleared: boolean;             // false while a job runs
  companies: number;
  message: string;
  status: EnrichmentStatus;
}

export interface SourceHealth {
  key: string;
  label: string;
  type: string;
  every_hours: number;
  last_read: string | null;
  last_items: number;
  last_error: string | null;
  last_skipped: string | null;
  warning: string | null;
}

export interface Status {
  last_refresh: string | null;
  hours_since_refresh: number | null;
  refresh_interval_hours: number;
  next_refresh: string | null;
  last_run: {
    id: number; trigger: string; started_at: string; finished_at: string | null;
    fetched: number; admitted: number; funding_new: number; events_new: number;
    blocked: number; pending_left: number; llm_usd: number;
    stopped_reason: string | null; error: string | null;
  } | null;
  guardrails: string;
  guardrails_on: boolean;
  funding_count: number;
  upcoming_events: number;
  enriched_contacts: number;
  pending: number;
  blocked: number;
  registry_error: string | null;
  sources: SourceHealth[];
  spend: {
    total_usd: number; total_cap: number; remaining_total: number;
    last_24h_usd: number; daily_cap: number; exhausted: boolean;
  };
}

export interface RefreshStatus {
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

export interface FundingQuery {
  stage?: StageKey;
  source?: SourceType;
  start?: string;
  end?: string;
  q?: string;
}

export interface EventsQuery {
  when: When;
  city?: string;
  start?: string;
  end?: string;
  q?: string;
}

export interface ContactsQuery {
  designation?: DesignationKey;
  q?: string;
}

type Params = Record<string, string | number | undefined>;

export function query(params: Params): string {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") qs.set(key, String(value));
  }
  const text = qs.toString();
  return text ? `?${text}` : "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  funding: (f: FundingQuery, limit = 200) =>
    request<FundingPage>(`/api/funding${query({ ...f, limit })}`),
  events: (f: EventsQuery, limit = 200) =>
    request<EventsPage>(`/api/events${query({ ...f, limit })}`),
  status: () => request<Status>("/api/status"),
  refreshStatus: () => request<RefreshStatus>("/api/refresh"),
  refresh: () => request<RefreshStart>("/api/refresh", { method: "POST" }),
  sources: (section: "funding" | "events" = "funding") =>
    request<SourcesResponse>(`/api/sources${query({ section })}`),
  addSource: (kind: AddKind, value: string, label?: string) =>
    request<AddSourceResponse>("/api/sources", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, value, label: label || null }),
    }),
  removeSource: (id: number) =>
    request<{ removed: boolean }>(`/api/sources/${id}`, { method: "DELETE" }),
  fundingExcel: (f: FundingQuery) => `/api/export/funding.xlsx${query({ ...f })}`,
  eventsExcel: (f: EventsQuery) => `/api/export/events.xlsx${query({ ...f })}`,
  enrichmentPreview: (f: FundingQuery) =>
    request<EnrichmentPreview>(`/api/enrichment/preview${query({ ...f })}`),
  enrich: (f: FundingQuery) =>
    request<EnrichmentSend>(`/api/enrichment${query({ ...f })}`, { method: "POST" }),
  enrichment: (f: ContactsQuery, limit = 200) =>
    request<ContactsPage>(`/api/enrichment${query({ ...f, limit })}`),
  enrichmentStatus: () => request<EnrichmentStatus>("/api/enrichment/status"),
  enrichmentCompanies: () => request<EnrichedCompany[]>("/api/enrichment/companies"),
  resumeEnrichment: () => request<EnrichmentRun>("/api/enrichment/resume", { method: "POST" }),
  retryEnrichment: () => request<EnrichmentRun>("/api/enrichment/retry", { method: "POST" }),
  clearEnrichment: () => request<EnrichmentClear>("/api/enrichment", { method: "DELETE" }),
  enrichmentExcel: (f: ContactsQuery) => `/api/export/enrichment.xlsx${query({ ...f })}`,
};

/** A mailto: link for a plausible address, else nothing. */
export function mailHref(email: string | null | undefined): string | undefined {
  return email && /^[^\s@<>"]+@[^\s@<>"]+\.[^\s@<>"]+$/.test(email) ? `mailto:${email}` : undefined;
}

/** A tel: link with the number's digits (and a leading +), else nothing. */
export function telHref(phone: string | null | undefined): string | undefined {
  const digits = (phone ?? "").replace(/[^\d+]/g, "").replace(/(?!^)\+/g, "");
  return digits.replace("+", "").length >= 6 ? `tel:${digits}` : undefined;
}

/** The API sends UTC. Times without an offset are read as UTC, not local. */
export function parseTime(iso: string): Date {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
}

/** Only web addresses reach an href; anything else becomes a dead link. */
export function safeHref(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

export const SOURCE_TYPE_LABEL: Record<string, string> = {
  rss: "News feed",
  website: "Your website",
  web_search: "Web search",
  linkedin: "LinkedIn",
  luma: "Luma",
  eventbrite: "Eventbrite",
  page: "Event portal",
};
