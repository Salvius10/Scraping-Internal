# India Startup Ecosystem Intelligence Dashboard — v1

## Context

A centralised dashboard for news about the Indian startup ecosystem, with two sections: a **News Feed** (aggregated, enriched articles) and an **Intelligence** section (natural-language Q&A with citations). The feed gets an **LLM sidebar** that explains an article, summarises the visible set, or filters the feed from a natural-language phrase.

Two hard constraints shape everything:

1. **$7 total LLM spend**, using only **gpt-oss-120b** and **Claude Sonnet 4.6**, both via AWS Bedrock.
2. **The feed must show recent news.** Refresh cadence is **every 12 hours**. Pagination is nice-to-have, not required.

The repo is empty (no commits) — greenfield.

### Source reality — verified by `scripts/discover_feeds.py` (2026-09-23)

Phase 1 ran first, and it overturned three assumptions. **Six of seven candidates publish RSS** — my earlier probes had simply used the wrong paths.

| Source | Verified | Strategy | Cost |
|---|---|---|---|
| Indian Startup News | ✅ `/rss` — **50 items**, newest today 12:06 IST | RSS | $0 |
| Entrackr | ✅ `/rss` — **50 items**, newest today 11:24 IST. `/feed` and `/feed/` 404, which is why I missed it | RSS | $0 |
| Inc42 | ✅ `/feed/` — 24 items, newest today. Also declares `comments/feed/` and `web-stories/feed/` (both wrong for us) | RSS | $0 |
| YourStory | ✅ `/feed` — 20 items, newest today | RSS | $0 |
| Sujata Chronicle | ✅ **`/feed.xml`** — 20 items, newest 22 Sep. `/feed` 404s; the real feed is declared in `<head>`. **No scraper needed** | RSS | $0 |
| VCCircle | ❌ No feed anywhere. `/rss.xml` and `/feed.xml` 404; `/rss/feed`, `/feeds/all.rss.xml`, `/taxonomy/term/1/feed` all return the same ~25KB catch-all page | **ScrapeGraphAI** | ~$0.15/mo |
| Moneycontrol | ❌ **RSS abandoned.** Every feed frozen at Apr 2024; `/rss/startups.xml` returns 503. Content is markets/stocks, not startups | **Dropped** | — |
| AIBoomi | ❌ Valid RSS with **zero items** — community/events org, not a publisher | **Dropped** | — |

**Consequences:** ScrapeGraphAI now runs against **one** site instead of four, cutting scraping from ~$0.58/mo to **~$0.15/mo**. Moneycontrol is dropped on topical grounds as much as technical — it would have meant scraping a bot-protected site for market news we do not want. Five of the six live sources are startup-focused and refresh daily.

**Live search (real-time Intelligence):** Inc42's `?s=<q>&feed=rss2` was **verified to return a genuinely filtered RSS feed** — free, live, targeted. The other five need either a plain feed re-fetch or HTML parsing. Recorded per source in `backend/app/ingest/sources.yaml`.

| Model | Input /1M | Output /1M |
|---|---|---|
| `gpt-oss-120b` (Bedrock) | $0.15 | $0.60 |
| Claude Sonnet 4.6 | $3.00 | $15.00 |

Sonnet 4.6 is **20× the input and 25× the output cost**. That single ratio drives the design.

**ScrapeGraphAI + Bedrock** is supported — the "Provider bedrock is not supported" bug (issue #633) was fixed by PR #636. Bundled examples still pin `claude-3-sonnet-20240229`; model IDs must be updated to `openai.gpt-oss-120b-1:0`.

### Decisions locked with the user

1. **Sources:** 5 RSS (Indian Startup News, Entrackr, Inc42, YourStory, Sujata Chronicle) + 1 scraped (VCCircle). Moneycontrol and AIBoomi dropped — see table above.
2. **Refresh every 12 hours.**
3. **Intelligence searches only our own 6 sites** — no external search API, no credit card, no billing risk. But it must be **real-time**: a user query triggers a live search of those sites, never waiting for the 12h scheduler. See "Real-time Intelligence" below.
4. **Extraction:** RSS where it exists (5 of 6). **ScrapeGraphAI runs live in production** on VCCircle only, against its *listing* page.
5. **Description:** real `og:description`/meta from the article page where available; LLM-generated one-liner as fallback.
6. **Stack:** Python + FastAPI backend, React frontend.

---


### Bedrock reality — verified by live smoke test (Phase 3)

Three things only a real call could reveal:

| Finding | Consequence |
|---|---|
| **gpt-oss-120b is a reasoning model.** Its Converse response puts a `reasoningContent` block *before* the `text` block, and bills the chain of thought as output tokens. | Reading `content[0]["text"]` returns nothing. `_extract_text()` now collects every text block and ignores reasoning. |
| **`reasoning_effort` is tunable on Bedrock and is a real cost lever.** On a classification prompt: `low` = 14 output tokens, default = 41, `high` = 68 — all returning the same correct answer. | Default set to `low` in config. **~3x cheaper** on every bulk call. gpt-oss only; never sent to Sonnet. |
| **Sonnet 4.6 is INFERENCE_PROFILE-only.** The bare id `anthropic.claude-sonnet-4-6` is rejected by `converse()`. The usable identifier is `global.anthropic.claude-sonnet-4-6`. | Config corrected. |

**Auth:** the project uses a **Bedrock long-term API key** (`ABSK...`), which is bearer-token auth scoped to Bedrock rather than SigV4 IAM credentials. Consequences: it carries no AWS identity (STS cannot describe it), and botocore reads it from `AWS_BEARER_TOKEN_BEDROCK`, so `bedrock.py` forwards it there from `.env` explicitly. Verified by live call on 2026-09-23: **both gpt-oss-120b and Sonnet 4.6 respond**.

## The cost model (design driver)

**Rule: gpt-oss-120b does all bulk mechanical work. Sonnet 4.6 is reserved for user-facing prose, and is always opt-in.**

The critical decision is **scraping listing pages, not article pages.** A `/news` index already carries 20+ headlines with dates and links, so one ScrapeGraphAI call harvests them all. Cost scales with **number of sites**, not number of articles — which is why article volume per source is irrelevant to the budget.

| Operation | Model | Frequency | Est. cost |
|---|---|---|---|
| RSS ingest (5 of 7 fields) | none | 5 sites × 2/day | **$0** |
| SGAI on listing page | gpt-oss | **1 site** (VCCircle) × 2/day | **~$0.15/mo** |
| Article fetch for meta description | none | per new article | **$0** (HTTP only) |
| Enrich: company + category + fallback description | gpt-oss | batched 30/call | **~$0.09/mo** |
| NL → filter JSON | gpt-oss | per filter | ~$0.0002 |
| Sidebar "explain this" | gpt-oss | per request | ~$0.0009 (Sonnet toggle: ~$0.021) |
| Intelligence answer (corpus) | gpt-oss | per query | ~$0.002 (Sonnet toggle: ~$0.03) |

**Ingest runs ~$0.24/month.** Over a 3-month project that is under $1, leaving ~$6 for Q&A — roughly **2,500 gpt-oss answers or 165 Sonnet 4.6 answers**.

**Anti-patterns this design avoids:**
- Scraping per article instead of per listing page (10–20× more expensive, scales with volume)
- Sending article bodies to an LLM to *filter* them — compile a SQL filter instead
- Re-summarising on every request — summarise once at ingest, cache by content hash
- Paying an LLM to re-derive a `pubDate` the RSS feed already supplied
- Scraping on page load — ingest is scheduled; the UI only reads the DB

**Known later optimisation, not in v1:** cache the CSS selectors SGAI derives and reuse them for free, firing SGAI only when a selector returns nothing. Cuts the $0.58/mo to ~$0.01. Not worth the extra code at 12-hour cadence; revisit only if spend becomes a problem.

---


### Phase 4 findings — ScrapeGraphAI on VCCircle

**Credentials.** ScrapeGraphAI's own model construction (`"model": "bedrock/..."`) routes through langchain-aws, which builds its own boto3 client, resolves SigV4 credentials from `~/.aws` and ignores `AWS_BEARER_TOKEN_BEDROCK` — it was billing the *old* account. Fixed by passing a prebuilt `model_instance` built on our own client. That also switched it from the legacy Invoke API to Converse, and enabled `reasoning_effort`.

**Converse returns typed content blocks**, not a string — `[{"type": "reasoning_content", ...}, {"type": "text", ...}]`. ScrapeGraphAI expects a string and silently yields `{'content': []}`. `_FlatChatBedrockConverse` flattens it.

**Reliability: ScrapeGraphAI failed ~60% of runs on this page**, with three distinct failure modes — a dict wrapping an unparsed JSON *string*, a truncated JSON array, and a bare `"NA"`. The cause is not the site blocking us: plain HTTP returns the full 633KB page with all 72 article links. It is that VCCircle's *visible* text is ~8.6KB and mostly navigation, so what reaches the model varies between runs on an unchanged page.

**Mitigation:** `fallback_anchors()` parses the listing HTML directly when the model returns nothing. Free, deterministic, recovers the same 72 articles. ScrapeGraphAI remains the primary extractor; this only prevents losing the source on a failed run.

**Known gap:** fallback-extracted articles have no `published_at` (anchors carry no date). The Phase 5 description pass fetches each article anyway and can read `article:published_time` at the same time, for free.

## Architecture

```
Scheduler (APScheduler, every 12h)
   └─> ingest/pipeline.py
         ├─ rss.py          (feedparser)              → 5 sites, free
         ├─ scraper_sgai.py (SmartScraperGraph)       → 1 listing page (VCCircle)
         ├─ describe.py     (fetch article, og:description) → free
         ├─ dedupe.py       (canonical story key)
         └─ enrich.py       (batched gpt-oss: company, category, fallback description)
                                  ↓
                            SQLite + FTS5
                                  ↓
FastAPI  ──  /api/articles     (feed: recency-windowed, filtered)
         ──  /api/filter       (NL phrase → JSON filter → SQL)
         ──  /api/chat         (sidebar: explain / summarise)
         ──  /api/intelligence (FTS5 retrieval → cited answer)
         ──  /api/status       (last refresh time, spend to date)
                                  ↓
React (Vite) — Feed page + Sidebar, Intelligence page
```

Every LLM call routes through `llm/budget.py`, which writes a ledger row and refuses the call if a cap is breached.

### Recency handling (the primary product constraint)

- Default feed view: **last 7 days**, sorted `published_at DESC`.
- Header shows **"Last refreshed N hours ago"** from `/api/status` — with a 12h cadence, users must be able to see the feed's age.
- **Listing-depth check:** at 12h cadence a busy source could publish more articles than its listing page holds, silently dropping stories. Phase 2 logs, per source per run, how many items the listing returned and how many were already known. If a run returns *zero* previously-seen articles, the window overflowed and that source needs a deeper listing page or a second page fetched.
- Relative dates ("2 hours ago") are common on listing pages — `dateparser` normalises them to absolute timestamps at ingest.
- Pagination: simple "Load more" on the feed. Not a blocker.

---

### Real-time Intelligence (query-time, never waits for the scheduler)

The 12-hour scheduler keeps the **News Feed** fresh enough. The **Intelligence** section must not inherit that staleness — a user asking a question gets a live search of the 6 sites at that moment.

Query flow, cheapest paths first:

1. **Compile the question to keywords** — one gpt-oss call, ~$0.0002. Cached by question hash.
2. **Fan out in parallel:**
   - **Corpus FTS5** over already-ingested articles — instant, free.
   - **Per-source live search**, free where the site offers one:
     - *WordPress search feed* — `?s=<query>&feed=rss2` returns a real RSS feed of search results. **Verified working on Inc42.** This is the ideal path: live, targeted, zero cost.
     - *Plain feed re-fetch* — for sites with a feed but no search feed, re-pull `/feed` to catch anything published since the last scheduler run. Free.
     - *Site search HTML* — for feedless sites, fetch their search URL and extract results.
   - Sites are probed for these capabilities in Phase 1 and the result is recorded per source in `sources.yaml` as a `live_search` strategy.
3. **Merge and dedupe** against the corpus by URL hash and canonical story key.
4. **Hydrate** only the new URLs: fetch `og:description` (free HTTP).
5. **Synthesise** a cited answer — gpt-oss ~$0.002, or Sonnet 4.6 ~$0.03 behind the quality toggle.
6. **Write-through:** persist everything newly discovered into the corpus, so the live search also warms the feed and the next query is cheaper.

**Cost control.** Free strategies (search feed, feed re-fetch, corpus) cost $0 per query. Only feedless sites whose search HTML needs LLM extraction cost anything (~$0.0024/site), and those fire **only when the free paths return too few results**. Live-search responses are cached with a short TTL (~10 min) so repeated or refined questions in one session don't re-fetch. Expected steady-state: **~$0.002–0.015 per query**.

**Degradation:** if a site is slow or blocks us, its branch is dropped after a per-source timeout (~5s) and the answer proceeds with whatever returned, noting which sources were unreachable rather than silently under-answering.

---

## Data model (`backend/app/models.py`)

**`Article`** — `id`, `url` (unique), `url_hash`, `source`, `headline`, `description`, `description_origin` (`meta` | `generated`), `published_at`, `company`, `category` (enum), `summary`, `content_hash`, `canonical_id` (FK self, null if canonical), `ingested_at`, `enriched_at`.

**`Chunk`** — `id`, `article_id`, `chunk_index`, `text`. Backs FTS5 and **chunk-level citations**.

**`LlmCall`** — `id`, `ts`, `feature`, `model`, `tokens_in`, `tokens_out`, `cost_usd`. The spend ledger.

**`IngestRun`** — `id`, `source`, `started_at`, `items_seen`, `items_new`, `error`. Powers the listing-depth check and the freshness indicator.

**`FilterCache`** — `phrase_hash`, `filter_json`. The same phrase is never recompiled.

**Fixed category enum** — prevents the model inventing "Funding" / "Funding News" / "Fundraise" and silently breaking filters:
`Funding`, `M&A`, `IPO`, `Product Launch`, `Policy/Regulation`, `Hiring/Layoffs`, `Shutdown`, `Partnership`, `Market/Analysis`, `Other`.

---

## Key files to create

| Path | Purpose |
|---|---|
| `backend/app/config.py` | Settings, model IDs, caps, recency window |
| `backend/app/ingest/sources.yaml` | **Source registry** — the only file to edit to add/remove a source |
| `backend/app/ingest/rss.py` | `feedparser` adapter → `Article` |
| `backend/app/ingest/scraper_sgai.py` | `SmartScraperGraph` w/ Bedrock gpt-oss, on listing pages |
| `backend/app/ingest/describe.py` | Fetch article, extract `og:description`/meta |
| `backend/app/ingest/dedupe.py` | Canonical story key |
| `backend/app/ingest/enrich.py` | Batched gpt-oss classification + fallback description |
| `backend/app/llm/bedrock.py` | `boto3` client; `call_cheap()` / `call_premium()` |
| `backend/app/llm/budget.py` | Ledger + cap enforcement decorator |
| `backend/app/api/{feed,filter,chat,intelligence,status}.py` | Endpoints |
| `backend/app/search/{corpus,provider}.py` | FTS5 retrieval; `WebSearchProvider` ABC (v2 seam) |
| `scripts/discover_feeds.py` | Probes candidate domains for feeds, **with browser UA** |
| `tests/fixtures/labeled_articles.json` | 50 hand-labeled articles for the enrichment eval |
| `frontend/src/pages/{Feed,Intelligence}.tsx` | The two sections |
| `frontend/src/components/{ArticleCard,Sidebar,FilterBar,Citation,FreshnessBadge}.tsx` | UI |

---

## Implementation phases

**Phase 1 — Feed discovery and fetchability (free, de-risks everything).**
`scripts/discover_feeds.py`: for each of the 7 domains, send a **browser-like User-Agent** and probe `/feed`, `/feed/`, `/rss`, `/rss.xml`, `/atom.xml`, plus any `<link rel="alternate" type="application/rss+xml">` in the homepage. Report per domain: feed found, item count, newest item date, and whether plain HTTP fetch succeeds at all. **Settle the VCCircle/Moneycontrol question here** — if either has a usable feed we skip writing a scraper; if either blocks all programmatic access, report it and drop the source rather than escalating. Populate `sources.yaml` from real results.

**Phase 2 — Storage + RSS ingest.** SQLAlchemy models, SQLite with FTS5, `rss.py`, `pipeline.py` wiring fetch → dedupe → store, `IngestRun` logging. No LLM yet. Verify the feed populates from the confirmed RSS sources.

**Phase 3 — Budget rails.** `llm/budget.py` and `llm/bedrock.py` **before any LLM feature exists**, so no call can bypass the ledger. Caps in env: `DAILY_USD_CAP`, `TOTAL_USD_CAP` (default 7.00). On breach, degrade gracefully — the feed keeps serving; Intelligence and the sidebar return a clear "budget cap reached" state rather than erroring.

**Phase 4 — ScrapeGraphAI on listing pages.** Configure `SmartScraperGraph` with Bedrock + `openai.gpt-oss-120b-1:0` (the examples' `claude-3-sonnet` ID is stale). One call per site per run against the listing URL, extracting a list of `{headline, url, date}`. Confirm Sujata Chronicle is server-rendered so Playwright can be skipped; add a per-run call cap as a circuit breaker.

**Phase 5 — Description + enrichment.** `describe.py` fetches each new article and reads `og:description`/`<meta name="description">` — free, and faithful to the source. Articles where that is missing or under ~40 chars go to the batched gpt-oss call, which already runs for company + category, and get a generated one-liner tagged `description_origin='generated'`. Then run the eval against `tests/fixtures/labeled_articles.json` and report category accuracy.

**Phase 6 — API + frontend.** Feed endpoint with recency window and filters; `ArticleCard` showing all 7 required fields; `FreshnessBadge`; "Load more"; Intelligence page rendering answers with footnote citations linking back to source URLs.

**Phase 7 — Sidebar.** NL filter compiling to JSON→SQL (cached by phrase hash); explain-this-article; summarise-visible-set (operates on cached one-line summaries, never full text). Add the gpt-oss / Sonnet 4.6 quality toggle.

---

## Scraping conduct

Respect `robots.txt`; set an identifying User-Agent; rate-limit to ~1 req/2s per domain; store only headline, short description and a link back — **no full-text republishing**. At 12-hour cadence the load on every source is trivial, which keeps us welcome.

---

## Verification

1. `python scripts/discover_feeds.py` → working/broken table for all 7 domains; VCCircle and Moneycontrol resolved either way.
2. `python -m app.ingest.pipeline --once` → `sqlite3 news.db "select source, count(*) from articles group by source"` shows rows from every configured source.
3. Recency: `select max(published_at) from articles` is within ~12h of now; `/api/status` reports last refresh.
4. Listing-depth: run ingest twice 12h apart (or replay fixtures); assert `items_new < items_seen` for each source — equality means the window overflowed.
5. Dedupe: seed the same funding story from two sources; assert exactly one row has `canonical_id IS NULL`.
6. `pytest tests/test_enrich.py` → category accuracy against the 50 labeled fixtures; assert ≥85%.
7. `uvicorn app.main:app` + `npm run dev` → feed renders all 7 fields; type "only funding news from last week" into the filter bar and confirm the SQL filter narrows the list.
8. Ask Intelligence a question answerable from ingested articles; confirm every claim carries a citation resolving to a real stored chunk and URL.
9. `sqlite3 news.db "select feature, sum(cost_usd) from llm_calls group by feature"` → total after a full end-to-end run **well under $0.10**.
10. Set `DAILY_USD_CAP=0`; confirm the feed still serves while LLM features report the cap cleanly.

---

## Explicitly out of scope for v1

- **LinkedIn scraping.** Prohibited by their User Agreement, aggressively blocked, and needs authenticated sessions you've said you won't supply. It should not gate v1 — and **the project should be renamed**, since "LinkedIn Scrapper" describes nothing we are building.
- **Open-web search** in Intelligence. Deferred by your decision; `search/provider.py` is the seam where it plugs in.
- **AIBoomi** — its feed is empty.
- Selector caching for feedless sites (noted above as a later optimisation).
- Auth, multi-user accounts, deployment/hosting.
