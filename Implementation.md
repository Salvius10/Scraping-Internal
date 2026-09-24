# Implementation — GPS (Ganit Pursuit OS)

A dashboard for the Indian startup ecosystem. It aggregates news from six
sources, removes duplicate coverage, classifies every story, and lets a reader
filter and interrogate the feed in plain English.

This document is the low-level design: what every module does, why it is shaped
that way, and what the real data forced us to change.

**Current state:** 267 articles ingested (236 by the pipeline, 31 written
through by live Intelligence queries), 170 tests passing, **$0.018794 of the
$7.00 budget spent (0.3%)**.

---

## Contents

1. [Constraints](#1-constraints)
2. [Architecture](#2-architecture)
3. [Phase log](#3-phase-log)
4. [Backend design](#4-backend-design)
5. [Frontend design](#5-frontend-design)
6. [Data flows](#6-data-flows)
7. [Cost model](#7-cost-model)
8. [Testing](#8-testing)
9. [Running it](#9-running-it)
10. [Known gaps](#10-known-gaps)

---

## 1. Constraints

Three constraints shaped every decision.

| Constraint | Consequence |
|---|---|
| **$7 total LLM spend, no top-up** | Cost is a design input, not an afterthought. Free paths are exhausted before any paid one runs. |
| **Two models only: `gpt-oss-120b` and Claude Sonnet 4.6** | Sonnet is **20× the input cost and 25× the output cost**, so it is never a default. |
| **Feed must show recent news; 12-hour refresh** | Staleness must be visible in the UI, and ingest must not silently lose stories between runs. |

### Model economics

| Model | Bedrock ID | Input /1M | Output /1M |
|---|---|---|---|
| gpt-oss-120b | `openai.gpt-oss-120b-1:0` | $0.15 | $0.60 |
| Claude Sonnet 4.6 | `global.anthropic.claude-sonnet-4-6` | $3.00 | $15.00 |

**The governing rule:** gpt-oss does all bulk mechanical work. Sonnet is an
explicit, user-facing opt-in and nothing else.

---

## 2. Architecture

```
                    ┌──────────────────────────────────────┐
  RSS × 5  ────────▶│                                      │
  (free)            │   ingest/pipeline.py                 │
                    │                                      │
  VCCircle ────────▶│   fetch → dedupe → store             │
  (ScrapeGraphAI)   │      → describe (free)               │
                    │      → enrich  (batched, paid)       │
                    │      → dedupe by company (free)      │
                    └───────────────┬──────────────────────┘
                                    │
                            ┌───────▼────────┐
                            │  SQLite + FTS5 │
                            └───────┬────────┘
                                    │
      ┌─────────────────────────────┼─────────────────────────────┐
      │                             │                             │
┌─────▼──────┐              ┌───────▼───────┐            ┌────────▼────────┐
│ /api/feed  │              │  /api/filter  │            │   /api/chat     │
│ /api/status│              │  NL → SQL     │            │ explain·summarise│
│ (free)     │              │  (gpt-oss)    │            │ (gpt-oss│Sonnet) │
└─────┬──────┘              └───────┬───────┘            └────────┬────────┘
      │               ┌─────────────▼──────────────┐              │
      │               │  /api/intelligence         │              │
      │               │  plan → live search (free) │              │
      │               │  → FTS5 → cited answer     │              │
      │               └─────────────┬──────────────┘              │
      │                             │                             │
      └─────────────────────────────┼─────────────────────────────┘
                                    │
                        ┌───────────▼────────────┐
                        │  React (Vite) frontend │
                        │  feed · rail · sidebar │
                        └────────────────────────┘

  Every paid call passes through llm/budget.py, which meters and can refuse it.
  app/scheduler.py runs the pipeline every 12h, inside the API or standalone.
```

**Layering rule:** `api` → `ingest`/`llm` → `db`/`models`. Nothing lower
imports anything higher. `llm/bedrock.py` is the only module that constructs a
Bedrock client, which is what makes the spend figures trustworthy.

---

## 3. Phase log

### Phase 1 — Source discovery

`scripts/discover_feeds.py`, stdlib only so it runs before any install. For
each candidate domain it probes `/feed`, `/feed/`, `/rss`, `/rss.xml`,
`/atom.xml` plus any `<link rel="alternate">` in the homepage, using a
browser User-Agent, and separately probes search endpoints.

**It overturned three assumptions.** Six of seven candidates publish RSS:

| Source | Finding | Strategy |
|---|---|---|
| Indian Startup News | `/rss` — 50 items | RSS, free |
| Entrackr | `/rss` — 50 items. `/feed` and `/feed/` 404, which is why the first probe missed it | RSS, free |
| Inc42 | `/feed/` — 24 items, carries `<category>` tags | RSS, free |
| YourStory | `/feed` — 20 items | RSS, free |
| Sujata Chronicle | **`/feed.xml`** — 20 items, declared in `<head>`; `/feed` 404s | RSS, free |
| VCCircle | No feed anywhere | **ScrapeGraphAI** |
| Moneycontrol | **RSS abandoned** — every feed frozen at Apr 2024, `/rss/startups.xml` returns 503 | **Dropped** |
| AIBoomi | Valid RSS with **zero items** — a community/events org, not a publisher | **Dropped** |

Scraping therefore runs against **one** site, not four. Inc42's
`?s=<query>&feed=rss2` was verified to return a genuinely filtered RSS feed —
a free, live, targeted search path recorded in `sources.yaml`.

### Phase 2 — Storage and RSS ingest

SQLAlchemy models, SQLite with an FTS5 index, and the RSS pipeline. No LLM
calls, no cost. Deduplication went through three rounds of correction against
real data — see [§4.6](#46-ingestdedupepy).

### Phase 3 — Budget rails

Built **before any LLM feature existed**, so no call could be written that
bypasses the meter. A live smoke test then revealed three things only a real
call could:

1. **gpt-oss-120b is a reasoning model.** Its Converse response puts a
   `reasoningContent` block *before* the answer, so `content[0]["text"]`
   returns nothing. More importantly it bills the chain of thought as output:
   answering `"OK"` cost 56 output tokens, 54 of them reasoning.
2. **`reasoning_effort` is tunable and is a real cost lever.** On a
   classification prompt: `low` = 14 output tokens, default = 41, `high` = 68
   — all three returning the same correct answer. Defaulted to `low`,
   **~3× cheaper on every bulk call**.
3. **Sonnet 4.6 is INFERENCE_PROFILE-only.** The bare id
   `anthropic.claude-sonnet-4-6` is rejected by `converse()`; the usable
   identifier is `global.anthropic.claude-sonnet-4-6`.

### Phase 4 — ScrapeGraphAI on VCCircle

Three integration problems, each caught by running it:

- **It was billing the wrong AWS account.** Configuring ScrapeGraphAI the
  documented way (`"model": "bedrock/..."`) makes `langchain-aws` build its own
  boto3 client, which resolves SigV4 credentials from `~/.aws` and ignores
  `AWS_BEARER_TOKEN_BEDROCK`. Fixed by passing a prebuilt `model_instance`
  wrapping our own client — which also upgraded it from the legacy Invoke API
  to Converse and enabled `reasoning_effort`.
- **Converse returns typed content blocks, not a string.** ScrapeGraphAI
  expects a string and silently yields `{'content': []}`.
  `_FlatChatBedrockConverse` flattens it.
- **It failed ~60% of runs** with three distinct failure modes. Not the site
  blocking us — plain HTTP returns the full 633KB page with all 72 article
  links — but VCCircle's *visible* text being ~8.6KB of mostly navigation, so
  what reaches the model varies between runs on an unchanged page.
  `fallback_anchors()` recovers the same 72 articles for free when the model
  returns nothing.

### Phase 5 — Descriptions and enrichment

**The free path covered everything.** Of 218 canonical articles at the time,
146 descriptions came from the RSS feed and 72 from the publisher's own
`og:description`. **Zero were generated by a model** — the fallback exists but
never fired.

Enrichment quality was measured, not assumed (`scripts/eval_enrich.py`, 46
hand-labelled articles). The first run showed executive appointments and legal
settlements defaulting to `Other`; adding one line of guidance per category
fixed both:

| | initial prompt | with category guidance |
|---|---|---|
| category, acceptable | 93.5% | **97.8%** |
| category, exact | 84.8% | **89.1%** |
| company | 91.3% | **95.7%** |

### Phase 6 — API and frontend

FastAPI endpoints and the React feed. Two problems showed up only in a
screenshot: descriptions ran to seven lines (clamped to two), and the activity
bar meant to be the one bold element was the quietest thing on the page.

The rendered feed also exposed a **third dedupe gap** — see
[§4.6](#46-ingestdedupepy).

### Phase 7 — The sidebar

Natural-language filtering, explain, and summarise. The filter compiles to SQL
rather than reading articles, which is why it costs $0.00009 instead of scaling
with the corpus.

### Phase 8 — Closing the v1 gaps

Five gaps were left after Phase 7. All are now closed:

| Gap | Fix |
|---|---|
| No scheduler | `app/scheduler.py` (APScheduler). Runs inside the API process by default, or standalone. The cadence is anchored to the last recorded refresh, so a restart never triggers a paid run on a fresh feed |
| Intelligence not built | `/api/intelligence`, `search/corpus.py`, `search/live.py`, and an Intelligence page in the frontend |
| Chunks unused | Chunks are now the citation passages. They were also **stale**: 72 of 236 still held the feed description after `describe.py` had replaced it. `ingest/chunks.py` keeps them in sync |
| VCCircle spend not in the ledger | Each ScrapeGraphAI call is now cap-checked and recorded under `scrape` |
| API sent datetimes with no timezone | `UTCDateTime` in `models.py`; the activity bar now counts days in the reader's timezone |

**What live verification changed:**

- **Three of the recorded live-search URLs were wrong.** `indianstartupnews.com/search?q=`
  and `vccircle.com/search?q=` return 404, and `entrackr.com/?s=` ignores the query. The
  first two sites share a CMS whose search is `/search?title={q}`. VCCircle's
  search is client-rendered, but its `/tag/{slug}` pages are server-rendered.
- **Inc42's "verified" search feed is disallowed.** Its robots.txt has
  `Disallow: /*?*` for every agent. Python's `robotparser` ignores wildcards, which is
  how this was missed. Inc42 now re-fetches its main feed instead.
- **gpt-oss cites as `【9】` or `【8†L1-L4】`** whatever the prompt asks, so no
  citation was being matched. `normalise_citations()` rewrites them to `[n]`.
- **Ranking needed the entity.** OR-ing the planned terms ("ipo, listing,
  stock, market") ranked other companies' IPOs above Zepto's. Retrieval now
  ranks stories in three tiers: the entity plus a term, then the entity alone,
  then any term.

### Post-phase — UI and a timezone bug

The UI was rebuilt on a four-colour brand palette, light-only. While verifying
it, a **real bug** surfaced: the API serialises naive datetimes
(`"2026-09-23T07:56:38"`), and JavaScript parses an offset-less date-time as
*local* time. Every timestamp was **5 hours 30 minutes early** in India. Fixed
frontend-side with `parseTime()`.

---

## 4. Backend design

```
backend/app/
├── config.py          80 lines   settings, model ids, caps, credentials
├── models.py         181 lines   5 ORM tables + 2 enums
├── db.py             105 lines   engine, pragmas, FTS5 index + triggers
├── schemas.py         80 lines   API response shapes
├── main.py                       FastAPI app, CORS, SPA static serving
├── scheduler.py                  12h refresh (in-process or standalone)
├── llm/
│   ├── budget.py     249 lines   spend ledger + cap enforcement
│   ├── bedrock.py    335 lines   the only Bedrock client
│   └── __main__.py               spend CLI: report, preflight, reset
├── ingest/
│   ├── sources.yaml              source registry (the only file to edit)
│   ├── sources.py     88 lines   registry loader
│   ├── rss.py        129 lines   feedparser adapter
│   ├── scraper_sgai.py 396 lines ScrapeGraphAI + free anchor fallback
│   ├── describe.py   177 lines   og:description + article:published_time
│   ├── chunks.py                 citation passages kept in sync
│   ├── dedupe.py     340 lines   two-pass deduplication
│   ├── enrich.py     223 lines   batched classification
│   └── pipeline.py   225 lines   orchestration + CLI
├── search/
│   ├── corpus.py                 FTS5 retrieval → citable evidence (free)
│   └── live.py                   parallel live search of the six sites (free)
└── api/
    ├── feed.py       167 lines   /articles /facets /activity
    ├── status.py      75 lines   /status
    ├── filter.py     212 lines   /filter  (NL → SQL)
    ├── chat.py       149 lines   /chat    (explain · summarise · ask)
    └── intelligence.py           /intelligence (cited Q&A, real-time)
```

### 4.1 `config.py`

A single pydantic-settings `Settings` object, loaded from `.env`. Everything
that affects spend lives here so it can be audited in one place.

| Group | Fields |
|---|---|
| Storage | `db_path`, `sources_file` |
| Feed | `recency_window_days=7`, `refresh_hours=12`, `dedupe_window_hours=72`, `dedupe_threshold=0.60` |
| HTTP | `user_agent`, `http_timeout=20`, `per_domain_delay=2.0` |
| Bedrock | `aws_region=ap-south-1`, `model_cheap`, `model_premium`, `reasoning_effort="low"` |
| Credentials | `aws_bearer_token_bedrock`, plus key/secret/profile fallbacks |
| Prices | `price_cheap_in/out`, `price_premium_in/out` |
| Rails | `total_usd_cap=7.00`, `daily_usd_cap=0.50`, `llm_dry_run=False` |

**Credential note.** The project authenticates with a **Bedrock long-term API
key** (`ABSK…`), which is bearer-token auth scoped to Bedrock — it carries no
AWS identity, so STS cannot describe it. botocore reads it from
`AWS_BEARER_TOKEN_BEDROCK`, but pydantic-settings loads `.env` into `settings`
rather than `os.environ`, so `bedrock.py` forwards it explicitly. Without that
step it silently falls back to whatever is in `~/.aws`.

### 4.2 `models.py`

```
Article        the feed row; self-referencing FK for duplicates
Chunk          text chunks backing FTS5 and chunk-level citations
LlmCall        the spend ledger — one row per model call, no exceptions
IngestRun      per-source run stats; detects a lost ingest window
FilterCache    natural-language phrase → compiled JSON filter
```

**`Article`** — `url` (unique), `url_hash`, `source`, `headline`,
`description`, `description_origin`, `published_at`, `company`, `category`,
`summary`, `story_key`, `canonical_id` (FK self), `content_hash`,
`ingested_at`, `enriched_at`. Indexed on `published_at DESC, canonical_id` for
the feed query.

**`Category`** is a closed enum of ten values. This matters: letting a model
invent labels produces "Funding" / "Funding News" / "Fundraise" as separate
categories and silently breaks every filter built on top of them. Anything
unrecognised is coerced to `Other`.

**`DescriptionOrigin`** records provenance — `FEED`, `META` or `GENERATED` —
so it is always visible whether a description came from the publisher or a
model.

**`IngestRun.window_overflowed`** returns `items_seen > 0 and items_new ==
items_seen`. On a 12-hour cadence a busy source can publish more than its feed
window holds; when every item is new, stories were almost certainly lost. The
CLI suppresses this on a source's first run, where every item is legitimately
new.

### 4.3 `db.py`

SQLite with `journal_mode=WAL` (concurrent reads during ingest),
`foreign_keys=ON`, `synchronous=NORMAL`, and `busy_timeout=10000`.

The busy timeout exists for a specific reason: the ledger writes on its own
connection during ingest, and a brief write lock should wait rather than fail.

**FTS5** is an external-content table over `articles`, kept in sync by three
triggers (`ai`, `ad`, `au`):

```sql
CREATE VIRTUAL TABLE articles_fts USING fts5(
    headline, description, company, summary,
    content='articles', content_rowid='id', tokenize='porter unicode61'
);
```

This is what makes corpus retrieval cost **$0** — no embeddings, no vector
store, no per-query model call just to find candidates.

### 4.4 `llm/budget.py`

The contract: with $7 and no top-up, **no code path may reach a model without
first passing a cap check**, and the recorded cost must be real rather than
estimated.

| Function | Purpose |
|---|---|
| `price_for(model)` | Returns (in, out) price. **Unknown models are charged at the premium rate** — guessing high is the safe direction. |
| `cost_of(model, in, out)` | Exact cost from real token counts. |
| `estimate_cost(model, chars, max_tokens)` | Deliberately pessimistic pre-call estimate: input at 4 chars/token, output assumed to hit `max_tokens` in full. |
| `get_spend(session)` | Returns a `Spend` snapshot. |
| `ensure_budget(estimated)` | Raises `BudgetExceeded` **before** any money moves. |
| `record_call(...)` | Writes one ledger row. **Failed calls are recorded too** — an unrecorded failure hides a retry loop. |
| `spend_by_feature()` | The "where did it go" view. |
| `format_report()` | Human-readable CLI summary. |

`Spend` exposes `remaining_total`, `remaining_today`, `headroom` (the tighter
of the two) and `exhausted`.

**Two caps.** `total_usd_cap` is the real ceiling. `daily_usd_cap` is a rolling
24-hour speed limit so a retry loop cannot drain everything overnight before
anyone notices. The daily window is rolling rather than calendar-based, which
avoids timezone arguments entirely.

**Graceful degradation.** When a cap is reached, the news feed keeps serving —
it is built on RSS and costs nothing — while LLM features return a clear
"budget cap reached" state rather than crashing or, worse, silently returning
something wrong.

### 4.5 `llm/bedrock.py`

The only module that constructs a Bedrock client. Uses the **Converse API**
rather than a provider SDK, because it is one code path for both an OpenAI
open-weight model and an Anthropic model, and it reports exact token usage.

```python
invoke(feature, prompt, *, system=None, premium=False,
       max_tokens=1024, temperature=0.0, model=None,
       reasoning_effort=None) -> LlmResult
```

Order of operations inside `invoke`, which is the whole point of the module:

1. Resolve model (`premium` → Sonnet, else gpt-oss).
2. `estimate_cost` → `ensure_budget` — **raises before a client is even built**.
3. Attach `reasoning_effort` for gpt-oss only (Sonnet rejects the field).
4. Call, with one retry on `Throttling`/`ServiceUnavailable`/`ModelTimeout`/`InternalServerError`.
5. `_extract_text` — collects every `text` block and **ignores
   `reasoningContent`**, which is what made responses look empty.
6. `record_call` with the real token counts.

Supporting pieces:

- **`parse_json`** — models wrap JSON in ``` fences or add a sentence before
  it, so a bare `json.loads` is not enough. Falls back to the outermost array
  or object.
- **`_explain`** — turns an opaque boto3 error into something actionable
  (model not enabled, wrong region, bad id, no credentials).
- **`whoami`** — free identity check via STS; reports honestly that a bearer
  token has no IAM identity rather than failing.
- **`llm_dry_run`** — returns an empty stub and ledgers $0, so features can be
  wired up without spending.

`python -m app.llm` gives `--preflight` (identity + model access, free, no
inference), the spend report, `--check` (exit 1 when capped) and
`--reset-ledger`.

### 4.6 `ingest/dedupe.py`

The hardest part of the system, and the one that changed most against real
data. The same funding round gets written up by five outlets within an hour;
without this the feed shows the same story five times and looks broken on day
one.

**Pass 1 — headline similarity, at ingest.**

```
normalise_tokens()   lowercase, strip punctuation, drop stopwords
numeric_signature()  every number in the headline
story_key()          sha1 of the 8 most distinctive tokens, sorted
jaccard()            |A∩B| / |A∪B|
containment()        |A∩B| / min(|A|,|B|)
similarity()         best of the two, containment gated on length
is_multi_topic()     roundup detection
headline_similarity() the full comparison callers should use
find_canonical()     scans a 72h window, returns the canonical article
```

Four corrections the data forced, in order:

1. **Short numbers were being dropped.** A `len(w) > 2` filter deleted the very
   dates that distinguish editions of a recurring column, so "Ecosystem Pulse —
   Sept 22" and "— Sept 10" merged into one story. Numeric tokens are now kept
   regardless of length, and numeric signatures must match.

2. **Jaccard was the wrong measure.** "Pune-based fintech and brokerage startup
   Definedge raises Rs 22 crore in funding" vs "Fintech and brokerage startup
   Definedge raises Rs 22 Cr in pre-Series A" scored **0.556** — under
   threshold — because Jaccard punishes the extra context words one outlet
   adds. That asymmetry is the *normal* pattern across outlets. Containment
   scores it 0.71. Duplicates found jumped **5 → 18**.

3. **Containment let roundups swallow stories.** An individual story is
   genuinely "contained" in a digest that mentions it, so
   `"Indian Startup IPO Sprint, Zetwerk-Ayr Settle Dispute & More"` absorbed
   the standalone Zetwerk article, hiding a real story behind a summary.
   `is_multi_topic()` falls back to Jaccard when a headline carries a roundup
   marker *or* splits into clauses where one shares almost nothing with the
   other headline. The naive version of this rule also killed a genuine
   Moneyview duplicate, so the clause test checks subjects rather than just
   punctuation.

4. **Funding jargon was creating matches.** "Creedom, Guickly raise
   early-stage funding" and "Yuma Energy, Kepler Aerospace, DocPharma, others
   raise early-stage funding" scored 0.67 on shared boilerplate while sharing
   no actual subject. Round vocabulary (`raise`, `round`, `seed`, `series`,
   `capital`…) joined the stopword list, forcing the match onto company names.

**Pass 2 — `dedupe_by_company()`, after enrichment.**

Headline similarity cannot catch every rewrite. Two outlets covering the same
round wrote "Ultraviolette raises $85 Mn, targets US expansion in 2027" and
"EV company Ultraviolette raises $85M in Series E led by deeptech fund Yali
Capital" — sharing only the company and the amount, scoring 0.40.

Enrichment has since extracted the company, so a second free pass uses it:
same company, same category, inside 48h, not multi-topic, and no conflicting
figures.

Two refinements that pass needed:

- **Calendar years are not deal figures.** `{85, 2027}` vs `{85}` failed strict
  equality and kept one round split in two. `significant_figures()` drops
  1900–2100; a company's Series F and Series G still differ on the amount.
- **Company + category is too weak without figures.** It merged two separate
  remarks by one executive at one event. A figure-free match now also requires
  headline similarity above `MIN_SIMILARITY_WITHOUT_FIGURES = 0.25`.

**Result: 32 of 236 articles deduplicated**, up from 5 at the first attempt.

### 4.7 `ingest/rss.py`

The free path, covering five of six sources. The feed already contains
headline, link, published date and a publisher-written description — paying a
model to re-derive any of that would be spending money to get a worse answer.

`fetch_feed()` fetches with an identifying User-Agent, parses with
`feedparser`, and normalises to `FeedItem`. Dates come from feedparser's
`published_parsed` struct first, with `dateparser` only as a fallback.
`clean_text()` strips markup and entities and truncates on a word boundary.

### 4.8 `ingest/scraper_sgai.py`

The entire paid surface of ingest — VCCircle only.

**Listing pages, not article pages.** One `/news` index already holds 20+
headlines with dates and links, so cost scales with the **number of sites**,
not the number of articles. That is the difference between ~$0.15/month and
something that grows every time a source gets busier.

```python
_graph_config()      builds ScrapeGraphAI around OUR Bedrock client
_FlatChatBedrockConverse  flattens Converse content blocks to a string
_coerce_rows()       normalises every response shape observed
_salvage_objects()   recovers whole objects from a truncated JSON array
_strip_reasoning()   removes inline <reasoning> tags
scrape_listing()     the main entry point
fallback_anchors()   free deterministic recovery
estimate_run_cost()  quantifies the unledgered spend
```

`MODEL_TOKENS = 128_000` must be passed explicitly: ScrapeGraphAI's token
table has no gpt-oss entry, and its `KeyError` fallback is a **silent 8192**
that would truncate the page and drop articles without raising anything.

**Response shapes actually observed on the same page**, each of which produced
zero articles until handled: a list; a dict wrapping a list; a dict wrapping an
unparsed JSON *string*; a LangChain message; a truncated array with no closing
bracket; and a bare `"NA"`. All are covered by `tests/test_scraper.py`.

**Spend is ledgered here too.** ScrapeGraphAI drives its own LangChain chat
model, so `invoke()` never sees these calls. Instead, `_FlatChatBedrockConverse`
checks the cap before each call and records the real `usage_metadata` after it,
under the feature `scrape`. Failed calls are recorded as well. If the cap refuses
the run, the free `fallback_anchors()` path takes over, so a spent budget
loses the model extraction but not the source.

### 4.9 `ingest/describe.py`

Free. Publishers already write a summary for social previews
(`og:description`) and stamp the publish time in `article:published_time`.
Both are exact and publisher-authored — strictly better than asking a model to
invent them.

`extract_meta()` tries `og:description`, `twitter:description`, then
`meta[name=description]`; and `article:published_time`, several variants, then
a `<time datetime>` element. `describe_pending()` is scoped to **canonical
articles only** — duplicates are hidden from the feed, so fetching their pages
would be wasted requests.

`MIN_DESCRIPTION_CHARS = 40`: below that a description is boilerplate and
worth replacing with a generated one.

### 4.10 `ingest/enrich.py`

The only routinely-paid step, batched precisely so it stays negligible:
**30 articles per call ≈ $0.000048 each**, roughly 50× cheaper than
classifying them one at a time.

Three economies beyond batching:

- Only canonical articles are enriched.
- Summaries are requested only where the publisher description was missing or
  too thin.
- `CATEGORY_GUIDE` gives one line per category, which measurably moved
  acceptable accuracy from 93.5% to 97.8%.

**Transaction structure matters here.** Reads, the model call, and writes are
three separate steps:

```python
_load_pending(limit)          # short read transaction, then closed
  → invoke_json(...)          # network call, no DB lock held
    → _apply_batch(...)       # short write transaction
```

An earlier version held one write transaction open across the whole loop while
`record_call` needed its own connection for the ledger — SQLite returned
`database is locked`. Holding a write lock across a multi-second network call
is wrong regardless of the database.

`_coerce_category()` and `_clean_company()` treat model output as untrusted:
unknown categories become `Other`, and the literal strings `"null"`, `"none"`,
`"N/A"` become `None`.

### 4.11 `ingest/pipeline.py`

Orchestration and CLI.

```
store_item()    → "new" | "duplicate" | "known"
ingest_source() → always records an IngestRun, even on failure
run_once()      → all sources, or one, or skip paid
_report()       → per-source table + corpus summary
```

One bad source never stops the run — the exception is caught and recorded on
the `IngestRun` row.

```bash
python -m app.ingest.pipeline --once
python -m app.ingest.pipeline --once --source entrackr
python -m app.ingest.pipeline --once --skip-paid      # RSS only, $0
python -m app.ingest.pipeline --once --no-enrich      # skip the paid step
```

The full run is: ingest → describe (free) → enrich (paid) → dedupe by company
(free).

### 4.12 API layer

All endpoints read the database only. Nothing in `feed.py` or `status.py` ever
fetches a page or calls a model, so **browsing the feed is free and instant no
matter how much of the budget is gone**.

#### `api/feed.py`

| Endpoint | Purpose |
|---|---|
| `GET /api/articles` | Newest first, duplicates folded away. Filters: `days`, `category[]`, `source[]`, `company`, `q`. Paginated by `limit`/`offset`. |
| `GET /api/articles/{id}` | One article. |
| `GET /api/activity` | Stories per day, for the masthead activity bar. |
| `GET /api/facets` | Counts per category and source, for the filter rail. |

`_duplicate_sources()` returns which other outlets covered each canonical
story, surfaced as `also_reported_by`. This turns dedupe from invisible
plumbing into a signal about how big a story is.

Articles with **no date are kept**, not hidden — a scraped listing may not
carry one, and dropping them would silently lose a source.

#### `api/status.py`

Freshness and budget. With a 12-hour refresh a user must be able to see how old
the feed is, otherwise stale news is indistinguishable from no news. Returns
`hours_since_refresh`, per-source health including `window_overflowed`, and the
current `Spend`.

#### `api/filter.py`

The expensive way to do natural-language filtering is to hand the model a pile
of articles and ask which ones match. The cheap way — and the correct one — is
to hand it only the **schema** and the user's phrase, get back a JSON filter,
and run that as SQL.

```
POST /api/filter  {"phrase": "funding rounds this week"}
→ {"filter": {"categories": ["Funding"], "since_days": 7, ...},
   "cached": false, "cost_usd": 0.00008,
   "explanation": "Showing stories Funding, from the last 7 days"}
```

Verified behaviour:

| phrase | compiled to |
|---|---|
| funding rounds this week | `categories=[Funding] since_days=7` |
| IPO news from entrackr | `categories=[IPO] sources=[entrackr]` |
| anything about Zepto | `company=Zepto` |
| fintech stories from the last 3 days | `query=fintech since_days=3` |

The last row matters: **fintech becomes a search term, not an invented
category**, because the prompt states explicitly that topics are not
categories.

`_coerce()` treats the response as untrusted — unknown categories and sources
are dropped, and `since_days` outside 1–365 is discarded. `_describe()` echoes
the filter back in plain language so the reader can see what was understood.
Results are cached by `sha1(phrase.lower().strip())` in `FilterCache`, so the
same phrase is **never recompiled**.

#### `api/intelligence.py`

`POST /api/intelligence {"question", "premium", "live"}`. It is real-time and never
waits for the scheduler:

```
1. plan      gpt-oss → {search, terms, since_days}; cached per question
             (QuestionCache); free keyword fallback when capped
2. live      search/live.py: all six sites in parallel, 8s each, cached 10 min
               search_html   Indian Startup News, Entrackr  /search?title={q}
                             VCCircle                       /tag/{slug}
               feed_refetch  Inc42, YourStory, Sujata Chronicle
             results must name the searched entity; a slow site → "timeout"
3. write     new finds get og:description (free) and go through store_item():
             deduped, chunked and ready for the feed and later enrichment
4. retrieve  search/corpus.py: FTS5 bm25 (headline 3, company 4), entity-first
             tiers, 4 slots reserved for live finds, newest first
5. answer    numbered passages → gpt-oss (Sonnet opt-in) → [n] citations
```

Each citation carries an `article_id` and a `chunk_id`, so every claim
resolves to stored text. When the budget is exhausted the response still
returns the matching stories and an `error`; only the prose is withheld.
Measured on the first live run: **$0.000467** uncached, **$0.000325** with
the plan cached, and about 7s with all six sites answering.

#### `api/extract.py` + `extract.py` — extract from any URL

`POST /api/extract {"url", "prompt"}`, surfaced on the main dashboard as
**Extract from a URL**. The reader pastes any page and says in plain words
what to pull out; ScrapeGraphAI returns it, as a table where the result is a
list of records (with CSV/JSON download), otherwise as text or JSON.

The URL is untrusted, so **our server fetches it, not ScrapeGraphAI**:

| Guard | Why |
|---|---|
| http/https only, ports 80/443, no `user:pass@` | nothing but ordinary web pages |
| every resolved IP must be public (`is_global`) | no localhost, private LAN, or the `169.254.169.254` cloud-metadata endpoint |
| redirects followed by hand, each hop re-checked | a public page cannot bounce the server onto localhost |
| 3 MB download cap, HTML/text content types only | no huge or binary downloads |

ScrapeGraphAI is then handed the **HTML itself** as its source, so it parses
locally and never makes a request of its own.

Spend: scripts, styles, SVG and every attribute except `href/src/alt/title`
are stripped (a 163 KB search page reached the model as ~10 K characters),
and the cleaned page is capped at 120 K characters -- worst case about
$0.01, typically well under $0.001 of input. Each call is cap-checked and
ledgered under `extract`; the budget is checked before the first call;
one extraction runs at a time; the same URL + request within an hour is
served from cache at $0. Pages rendered entirely by JavaScript have no
server-side text and are refused before any model call.

#### `api/chat.py`

Three modes, all working from data already in the database — headline,
publisher description, and the one-line summary written during enrichment.
Nothing re-fetches an article or re-summarises text already paid for.

| Mode | Input | Cost (gpt-oss) |
|---|---|---|
| `explain` | one `article_id` | ~$0.0001 |
| `summarise` | `article_ids` of what is on screen | ~$0.0005 for 30 |
| `ask` | `article_ids` + a question | ~$0.0002 |

`premium: true` routes to Sonnet 4.6 at roughly 21× the cost. It is never a
default; the UI exposes it as a checkbox labelled with its real cost.
`MAX_SUMMARY_ARTICLES = 40` caps the prompt. Budget exhaustion returns HTTP 200
with an `error` field rather than an exception, because "budget reached" is an
expected state the UI should render cleanly.

---

## 5. Frontend design

```
frontend/src/
├── main.tsx        10 lines   React root
├── api.ts         187 lines   typed client + colour/time helpers
├── App.tsx        441 lines   Masthead, Rail, Entry, App
├── Sidebar.tsx    231 lines   the assistant panel
└── styles.css     773 lines   the whole design system
```

React 18 + TypeScript + Vite, no UI framework and no state library — the app
has one screen and a handful of state atoms, so a store would be ceremony.
FastAPI serves the built `dist/` in production; Vite proxies `/api` in dev.

### 5.1 `api.ts`

Typed mirrors of every response shape, plus two small `get`/`post` helpers that
serialise arrays as repeated query params.

Three pieces of real logic live here:

**`parseTime(iso)`** — the timezone fix. The API sends naive ISO strings whose
values are UTC; JavaScript parses an offset-less date-time as *local* time, so
in India every article read 5h30m earlier than it was published. The helper
appends the missing `Z` when there is no offset.

**`categoryColor()` / `categoryTextColor()`** — categories are grouped into
four families, one per brand colour. `#dbeaff` is a surface tone and invisible
as text, so the quiet family's *label* borrows the ink while its *rule* keeps
the colour.

**`sourceLabel()`** — maps internal source names to display names.

### 5.2 `App.tsx`

Four components.

**`Masthead`** — wordmark, the day-activity bar, and the freshness indicator.
`describeAge()` writes it in plain words ("Refreshed 2h ago"), and the
indicator turns the accent colour when the feed is older than the refresh
interval.

**`Rail`** — category and source filters with live counts from `/api/facets`.
Each is a toggle; `aria-pressed` carries the state.

**`Entry`** — one story. A four-column grid: `time │ category rule │ body │
meta`. The meta column is right-aligned so a wide row reads as a ledger line
rather than leaving the right half of the panel empty.

**`App`** — holds all state and composes the rest.

```
status, facets, activity      fetched once on mount
articles, total, hasMore      the feed page
categories, sources, query,
  company, days, activeDay    filter state
sidebarOpen, selected         assistant state
offsetRef                     pagination cursor (a ref, not state —
                              it must not trigger a re-render)
```

`load(append)` is a `useCallback` keyed on the filter state, so changing any
filter resets the offset and refetches. Typing is debounced 250ms so every
keystroke is not a request. `grouped` builds day sections with `useMemo`.

### 5.3 `Sidebar.tsx`

Self-contained: it owns the phrase box, the conversation log, the quality
toggle and a running session cost, and talks to the parent through four
callbacks (`onApplyFilter`, `onClearSelection`, `onClose`, plus the `visible`
and `selected` props).

`renderAnswer()` converts the model's light markdown into plain readable
lines — bullets become styled paragraphs. Answers accumulate newest-first, each
labelled with the model used and what it cost.

### 5.4 `styles.css` — the design system

**Light theme only.** No `prefers-color-scheme` block; `color-scheme: light`.

**Brand palette — exactly four values:**

```
#1a00d9   primary     Funding · IPO · buttons · selected · focus
#5e9eff   secondary   M&A · Partnership · activity bars
#dbeaff   wash        selection, focus ring, the quiet category rule
#fe6e06   accent      Policy/Regulation · Shutdown · stale-feed warning
```

Two light brand colours fail WCAG AA as *text* (`#5e9eff` 2.7:1, `#fe6e06`
2.8:1 on white). They stay for bars, rules and dots; anything written in those
hues uses a text shade of the same hue: `--brand-2-ink #2563c7` and
`--accent-ink #b34700` (both 5.4:1+).

Everything else is a neutral: one near-black (`#16181f`), two greys (the
softer one `#656c81`, raised from 3.3:1 to 4.9:1), two hairlines, one page
tint, and an off-white surface (`#fdfdfe`). Radius rule: panels 6px, every
control, tag and badge 4px, nothing pill-shaped. Colour is spent on **meaning** — category,
state, risk — never on prose. That is what separates a professional tool from a
styled one.

**Category families**, one per brand colour:

| family | colour | categories |
|---|---|---|
| Capital | `#1a00d9` | Funding, IPO |
| Ownership | `#5e9eff` | M&A, Partnership |
| Risk | `#fe6e06` | Shutdown, Policy/Regulation |
| Quiet | `#dbeaff` | Product Launch, Hiring/Layoffs, Market/Analysis, Other |

Ten categories cannot have ten hues from a four-colour palette, and grouping
them is better information design anyway: a reader scanning deal flow wants to
know whether something is capital, ownership or trouble. The exact category is
already written next to the rule.

**Scales.** One spacing scale (`--s1`…`--s6` = 4/8/12/16/24/32), one radius
family (`--r` 6px, `--r-sm` 4px), one easing curve. One typeface, Instrument
Sans, with tabular figures so times and amounts align.

**Layout principles**, in order of how much they shape the page:

1. **The outlet sits above the headline, the category closes the row.** Each
   story's kicker line is the outlet plus a copy-link button; the category has
   a fixed-width column at the end so categories align down the page. (An
   earlier version led with the extracted company name; it was removed at the
   reader's request -- the company is still extracted and used by search,
   filters and Intelligence.)
2. **Category is a coloured rule, not a pill** — it is a filter dimension, so
   it earns structural encoding.
3. **A time spine, not cards** — deal flow is chronological.
4. **Staleness is visible**, because a 12-hour refresh makes age meaningful.

Responsive at three breakpoints: 1240px drops the rail when the sidebar is
open, 1100px collapses the meta column back under the body, 860px stacks
everything and turns the rail into a horizontal scroller. Verified at 390px
with zero horizontal overflow. `prefers-reduced-motion` is respected.

---

## 6. Data flows

### 6.1 Ingest (every 12 hours)

```
for each source in sources.yaml:
    rss     → fetch_feed()        free
    scrape  → scrape_listing()    ~$0.003, falls back to anchors on failure
        ↓
    for each item:
        already known by url?  → refresh description if better, skip
        find_canonical()       → 72h window, headline similarity
        insert Article + Chunk → FTS5 triggers fire
        ↓
    record IngestRun(items_seen, items_new, items_duplicate, error)

describe_pending()    → og:description + article:published_time   free
enrich_pending()      → batches of 30 via gpt-oss                 ~$0.0015/batch
dedupe_by_company()   → second pass using extracted companies     free
```

### 6.2 Feed request

```
GET /api/articles?category=Funding&days=7&limit=30
  → SELECT ... WHERE canonical_id IS NULL
                 AND (published_at IS NULL OR published_at >= now-7d)
                 AND category IN (...)
    ORDER BY published_at DESC NULLS LAST
  → _duplicate_sources() for the page's ids
  → FeedPage
```

No model call. No network call. Free.

### 6.3 Natural-language filter

```
POST /api/filter {"phrase": "funding rounds this week"}
  → sha1 lookup in FilterCache
      hit  → return, $0
      miss → invoke_json(gpt-oss, schema + phrase)   ~$0.00009
             _coerce() drops anything unrecognised
             cache it
  → frontend maps FilterSpec onto /api/articles query params
```

The corpus is never sent to the model, which is why this stays cheap as the
database grows.

### 6.4 Scheduled refresh

```
startup (SCHEDULER_ENABLED) → next_due(last IngestRun):
    never run or overdue → now      fresh → last + 12h
every 12h → refresh_job():
    in-process lock held?           → skip (never overlap)
    refreshed < 12h ago elsewhere?  → skip (never pay twice)
    pipeline.refresh(): ingest → describe → enrich → dedupe → sync chunks
```

### 6.5 Sidebar answer

```
POST /api/chat {"mode":"summarise","article_ids":[...],"premium":false}
  → load articles, preserve the reader's order
  → build prompt from headline + company + category + stored summary
  → invoke() → ensure_budget → Bedrock → record_call
  → {answer, model, cost_usd, budget_remaining}
```

---

## 7. Cost model

### Measured spend

```
enrich            9 calls   $0.011965     218 articles classified
eval_enrich       4 calls   $0.003613     quality measurement
chat_summarise    2 calls   $0.000970
filter            4 calls   $0.000349
chat_explain      2 calls   $0.000201
chat_ask          1 call    $0.000187
                            ─────────
                            $0.017285     of $7.00  (0.2%)
```

VCCircle scraping is now recorded under `scrape`. Intelligence spend is recorded
under `intelligence_plan` and `intelligence`.

### Unit economics

| Operation | Model | Cost |
|---|---|---|
| RSS ingest | none | **$0** |
| Publisher description + date | none | **$0** |
| Corpus search (FTS5) | none | **$0** |
| Feed browsing | none | **$0** |
| Enrichment | gpt-oss | $0.000048/article |
| NL filter | gpt-oss | $0.00009/phrase, then cached at $0 |
| Explain | gpt-oss | ~$0.0001 |
| Summarise 30 | gpt-oss | ~$0.0005 |
| VCCircle scrape | gpt-oss | ~$0.003/run |
| Any of the above | Sonnet 4.6 | **≈21×** |

**Steady-state ingest: ~$0.24/month.** The remaining ~$6.98 buys roughly
70,000 filter phrases or 35,000 explanations.

### The decisions that made this fit

1. **RSS supplies 5 of 7 fields free.** Paying a model to re-derive a
   `pubDate` the feed already gave us would be spending money for a worse
   answer.
2. **Scrape listing pages, not article pages.** Cost scales with sites, not
   articles — 10–20× cheaper and it does not grow as sources get busier.
3. **Batch enrichment 30 at a time.** ~50× cheaper than per-article calls.
4. **`reasoning_effort: low`.** ~3× cheaper output on every bulk call, with no
   measured quality loss.
5. **Compile filters to SQL.** Constant cost instead of one that grows with
   the corpus.
6. **Cache by content hash and phrase hash.** Nothing is ever paid for twice.
7. **Sonnet strictly opt-in.**

---

## 8. Testing

**202 tests, all offline — no test spends money.**

| File | Tests | Covers |
|---|---|---|
| `test_budget.py` | 21 | Pricing arithmetic, both caps, rolling 24h window, ledger, and that `invoke` refuses **before building a client** when over budget |
| `test_dedupe.py` | 27 | Every real headline pair that broke the matcher, kept as regressions |
| `test_enrich.py` | 37 | Closed taxonomy, company cleaning, meta extraction, fixture validity |
| `test_filter.py` | 22 | Untrusted model output, cache keys, plain-language echo |
| `test_scraper.py` | 17 | Every ScrapeGraphAI response shape observed live; scrape calls cap-checked and ledgered |
| `test_intelligence.py` | 30 | Untrusted plans, FTS5 injection, entity-first ranking, live-result parsing and relevance, one slow site never stalls, write-through, citation parsing incl. gpt-oss `【n】`, budget degradation |
| `test_scheduler.py` | 10 | Restart does not re-run a fresh feed, runs never overlap, a failing run is survived |
| `test_extract.py` | 32 | URL guard (private IPs, metadata endpoint, schemes, ports, redirect-to-localhost), size/type limits, HTML cleaning, result shapes, budget checked before any call, cache, one run at a time |
| `test_storage.py` | 6 | Timestamps come back aware, API emits an offset, activity uses the reader's day, chunk sync |

Two deliberate choices:

- **`conftest.py` points at a throwaway database** before anything imports
  settings, so tests can never touch the real ledger. It also sets
  `SCHEDULER_ENABLED=false`, so starting the app under test can never
  trigger a paid ingest.
- **Model quality is measured by a script, not a test.**
  `scripts/eval_enrich.py` makes real calls; a pytest that quietly spends money
  on every run is a bad idea on a $7 budget.

The eval scores against 46 hand-labelled headlines and allows defensible
alternates per article — scoring "is a stake sale M&A or Market/Analysis?" as a
hard error would measure the labeller's opinion, not the model.

---

## 9. Running it

### Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium   # ScrapeGraphAI
cd frontend; npm install; npm run build; cd ..
```

Copy `.env.example` to `.env` and add `AWS_BEARER_TOKEN_BEDROCK`. `.env` is
gitignored.

### Daily use

```powershell
# verify credentials and model access -- free, no inference
.\.venv\Scripts\python.exe -m app.llm --preflight

# refresh the feed
Push-Location backend
..\.venv\Scripts\python.exe -m app.ingest.pipeline --once
..\.venv\Scripts\python.exe -m app.llm                 # where the money went
Pop-Location

# serve the dashboard at http://127.0.0.1:8000
# the 12h refresh runs inside it; an overdue feed refreshes on startup (paid)
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --app-dir backend

# or: serve without the scheduler, and run the schedule in its own process
$env:SCHEDULER_ENABLED = "false"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --app-dir backend
Push-Location backend; ..\.venv\Scripts\python.exe -m app.scheduler; Pop-Location
..\.venv\Scripts\python.exe -m app.scheduler --status   # when it is next due (free)
```

PowerShell has no `&&`; use `;`, or `--app-dir` as above to avoid `cd`
entirely.

### Adding a source

Edit `backend/app/ingest/sources.yaml` — nothing else. Run
`python scripts/discover_feeds.py` first to find whether it publishes a feed.

---

## 10. Known gaps

| Gap | Impact | Notes |
|---|---|---|
| **Valuation vs round size** | "Brahma AI at $2 bn valuation" and "raises $150M" read as conflicting figures and stay unmerged | Errs toward showing a duplicate rather than hiding a story — the right direction |
| **No open-web search** | Intelligence searches only the six sources | Deliberate. It avoids a search API and a credit card |
| **VCCircle live search is tag-based** | Finds a company only if VCCircle has a tag page for it | Its `/search` page is rendered client-side, so there is nothing to parse without a headless browser |
| **URL extraction cannot read JavaScript-only pages** | Single-page apps return an empty shell over plain HTTP | Refused for free with a clear message; a headless browser would lift it at the cost of a much larger attack surface |
| **Live finds lack company/category until the next refresh** | Newly written-through stories show no company and "Other" in the feed for up to 12h | The next scheduled `enrich_pending()` classifies them |
| **Listing-window overflow** | A very busy source could publish more in 12h than its feed holds | Detected and reported by `IngestRun.window_overflowed`, not yet auto-remediated |

---

## Appendix — verified external facts

Everything below was confirmed by a live call, not assumed.

- `indianstartupnews.com/rss`, `entrackr.com/rss`, `inc42.com/feed/`,
  `yourstory.com/feed`, `sujatachronicle.com/feed.xml` all serve valid RSS.
- `moneycontrol.com` RSS is frozen at April 2024; `/rss/startups.xml` returns
  503.
- `aiboomi.org/feed/` is a valid feed with zero items.
- `inc42.com/?s=<query>&feed=rss2` returns a genuinely filtered RSS feed.
- `vccircle.com` serves the full 633KB page with all 72 article links over
  plain HTTP — it does not block us.
- gpt-oss-120b on Bedrock is ON_DEMAND in `ap-south-1`; Sonnet 4.6 is
  INFERENCE_PROFILE-only and must be addressed as
  `global.anthropic.claude-sonnet-4-6`.
- ScrapeGraphAI 2.2.4 requires Python `>=3.12,<4.0`; this project runs 3.14.
