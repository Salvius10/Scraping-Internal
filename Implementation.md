# Implementation — GPS (Ganit Pursuit OS)

**Startup Funding & Events Intelligence Pipeline.** Two requirements:

- newly funded startup accounts (Indian startups that announced a round)
- startup-related events happening in India

This document is the low-level design of the `application/archi` branch: every
box in the architecture diagram, the module that implements it, and the rules
it follows. The previous design (news feed, Intelligence, Insights, chat) is in
git history before this branch.

---

## 1. Constraints

| Constraint | Consequence |
|---|---|
| **$7 total Bedrock spend, no top-up** | Every model *and guardrail* call is cap-checked before it is made and ledgered after (`llm/budget.py`). Already-seen items never reach a paid stage. |
| **Apify and Firecrawl bill separately** | Every Apify run carries `maxItems` and `maxTotalChargeUsd`; paid sources run at most every `every_hours`. Defaults stay inside Apify's $5/month free usage. |
| **12-hour refresh, plus on demand** | The scheduler and "Refresh now" run the same code, under one cross-process lock. |
| **Production** | One failing source or stage never stops the rest; nothing is lost when a stage is unavailable — items wait and are retried. |

Models: `gpt-oss-120b` (`openai.gpt-oss-120b-1:0`, $0.15/$0.60 per 1M tokens)
does all structuring at `reasoning_effort: low`. Sonnet is not used.

---

## 2. Architecture

```
  Scheduler (12h)          Manual Refresh (POST /api/refresh)
        └──────────┬───────────────┘           scheduler.py
                   ▼
  Ingestion Orchestrator — fans out to every source      pipeline/orchestrator.py
                   ▼
  1 · Information sources                                pipeline/sources/*
     RSS feeds · LinkedIn (Apify) · Web search (Firecrawl)      funding
     Luma (Apify) · Eventbrite (Apify) · other portals (Firecrawl)   events
                   ▼
  Guardrail #1 — raw scraped content ────────┐           pipeline/guardrails.py
                   ▼                         │
  Dedup Stage 1 — URL + content hash ◀──▶ Seen-items index (seen_items)   pipeline/seen.py
                   ▼                         │
  Guardrail #2 — LLM input ──────────────────┤ blocked   pipeline/process.py
                   ▼                         ▼
  LLM Structuring — Bedrock            Blocked / Rejected log (blocked_items)
    + Dedup Stage 2 (storage.py)             ▲           pipeline/structure.py, storage.py
                   ▼                         │
  Guardrail #3 — LLM output ─────────────────┘
                   ▼
  Storage — every row tagged with source URL + date (funded_startups, startup_events)
                   ▼
  Platform — Funded startups feed · India startup events feed      api/*, frontend/
                   ▼
  Export to Excel                                         api/export.py
```

`pipeline/run.py` runs the stages in this order. Layering: `api` → `pipeline`
→ `llm`/`search` → `db`/`models`; nothing lower imports anything higher, and
`llm/bedrock.py` is the only module that builds a Bedrock client.

---

## 3. Stages

### 3.1 Scheduler and Manual Refresh — `scheduler.py`, `api/refresh.py`

- Runs `run_pipeline()` every `refresh_hours` (12), inside the API process or
  standalone (`python -m app.scheduler`).
- The cadence is anchored to the last recorded run (`pipeline_runs`), so a
  restart does not trigger a paid run on fresh data.
- Overlap guards: an in-process lock, an OS file lock beside the database
  (`proclock.py`, released by the OS if a process dies), and a freshness check.
- `POST /api/refresh` starts the same run in a background thread; refused (in
  words) while one runs or within `manual_refresh_cooldown_minutes` (10).

### 3.2 Ingestion Orchestrator — `pipeline/orchestrator.py`

One fetch job per source, `fetch_workers` (4) in parallel, each isolated. A
source is **skipped**, with the reason recorded and nothing paid, when it was
read successfully within its `every_hours`, the run is `--free-only`, or its
service has no credentials. Every attempt is a `source_runs` row: items,
estimated Apify cost, Firecrawl credits, warning, error. Nothing is written
until every job is back.

### 3.3 Information sources — `pipeline/sources/`, registry `pipeline/sources.yaml`

The registry is the only file to edit. A broken registry (duplicate key,
non-http URL, a LinkedIn link that is not an account) is refused as a whole.

| Source | Reads | Cost | Notes |
|---|---|---|---|
| `rss` (5 feeds) | publisher RSS | free | every run; links must be http(s) |
| `linkedin` (per account) | `harvestapi~linkedin-profile-posts` | $0.002/post | no LinkedIn login; later reads ask only for posts newer than the source's cursor |
| `web_search` | Firecrawl news search, last `lookback_days` | ~2 credits/10 results | results only, no page fetched |
| `luma` | `dami_studio~luma-events-scraper`, city feeds | $0.002125/event | India discover pages exist for bengaluru, mumbai, new-delhi (checked 2026-10-16) |
| `eventbrite` | `dami_studio~eventbrite-events-scraper` | $0.00092/event | Eventbrite browse URLs pin the place (india--bangalore …); local times → UTC |
| `page` (other portals) | Firecrawl scrape of a listing page | 1 credit | the whole page is one item; the LLM lists its events |

Startup Grants India was dropped: its data needs a paid subscription.

**Sources added on the page** (`pipeline/user_sources.py`, table
`user_sources`, `api/sources.py`). The Funded startups page has a **Sources**
panel listing every funding source with its health. The built-in ones above
stay on the 12-hour schedule and cannot be removed there; three kinds can be
added, and removed again:

| Added | Becomes | Read |
|---|---|---|
| LinkedIn account | `linkedin:<slug>` — the same adapter as the built-in account | Apify, every `user_source_hours` (24) |
| Website | `site:<id>`, type `website`. If the link is a feed, or the page declares one (its own section's feed first, e.g. `techcrunch.com/category/venture/feed/`), it is read as RSS: free, every run. Otherwise Firecrawl reads the page (1 credit, every 24h) as one `funding_page` item; the LLM lists each round with its own story link and date | free, or 1 credit |
| Search phrase | `search:<id>`, a one-query web search | Firecrawl, every 24h |

They are merged into the run by `registry.all_sources()` and go through every
stage like a built-in source. A source already read — built-in or added — is
refused, as is any link to a non-public address; at most `max_user_sources`
(50) can be added, since there is no login. An added source is read on the
next run ("Refresh now" starts one). Removing one stops it being read; what it
found stays in the feed.

A website is untrusted, so every plain fetch of it — and of every RSS feed —
goes through `netguard.py`: http(s) on ports 80/443 only, every resolved
address public, the connection made to exactly the address checked (no DNS
rebinding), redirects followed by hand and re-checked, 3 MB cap.

A page read again after it changed folds into the rows it made last time:
in Dedup Stage 2 the same company and stage at the same story link is one
deal even when undated.

Every source returns `RawItem`s (`pipeline/items.py`): kind (`funding`,
`event`, `event_page`), source, URL, title, text, publish time, and — for
events — the portal's structured fields as `payload`.

**Cursor.** `source_runs.cursor` is the newest publish time in a read whose
every item was dealt with. If Guardrail #1 could not check a source's items,
the read is marked failed and the cursor does not move, so the items are
fetched again.

### 3.4 Guardrails #1, #2, #3 — `pipeline/guardrails.py`

Amazon Bedrock `ApplyGuardrail`, one configured guardrail:

| | screens | source |
|---|---|---|
| #1 raw | title + text of every fetched item (first 4,000 chars) | INPUT |
| #2 input | exactly the text each item puts in the prompt | INPUT |
| #3 output | every free-text field of every row the model wrote | OUTPUT |

- **Off until switched on** (`GUARDRAILS_ENABLED=true` + `GUARDRAIL_ID`). While
  off every stage passes items unchecked and `/api/status` says so. Enabled
  without an id is a misconfiguration: items are **held**, never passed.
- A `BLOCKED` finding (content filter, denied topic, prompt attack, blocked
  word, PII) blocks; masking PII alone does not; an intervention with no
  nameable finding blocks.
- **Fail closed.** A guardrail that cannot be reached, or a spent budget,
  holds the item; it is checked again next run.
- **Spend.** Priced per text unit and policy from the response's `usage`,
  cap-checked with a pessimistic estimate first, ledgered as
  `guardrail_raw|input|output`. A verdict is stored per stage and text
  (`guardrail_verdicts`), so the same text is never paid for twice — an RSS
  feed repeats its last 50 stories every run.
- **Setup.** `python -m app.pipeline.guardrails --create` creates `gps-pipeline`
  (sexual and hate HIGH; violence, insults, misconduct MEDIUM; prompt attack
  HIGH on input; profanity list) and publishes version 1. A Bedrock API key
  may not be allowed to create guardrails; the message then says how to do it
  in the console.

### 3.5 Dedup Stage 1 and the seen-items index — `pipeline/seen.py`

An item is dropped when its URL (normalised: scheme, `www.`, fragment,
tracking parameters, parameter order and trailing slash aside), or its
content hash (the same words under another URL), is already in `seen_items`,
or repeats within the batch. Everything else is written as `pending`.

`seen_items` is also the **work queue**: the LLM stages read pending items
from it, so an item the model could not answer this run is retried without
being fetched again. A known *event* whose text changed (rescheduled, new
venue) updates its stored row from the portal's fields — no model call.

### 3.6 LLM Structuring — `pipeline/structure.py`

Pending items in batches of `llm_batch_size` (20), at most
`max_items_per_run` (400) a run, oldest first.

- **funding** → every round the item announces for an Indian startup:
  company, stage (Pre-Seed, Seed, Series A, Series B, Other), round, amount,
  investors, sector, HQ city, one-line summary. `[]` = irrelevant.
- **event** → relevant? in India? category, summary. Dates, venue, city,
  price and link come from the portal, never the model; a portal country code
  overrides the model's `in_india`.
- **event_page** → the startup events listed on the page, validated.

The prompts say item text is data, never instructions, and wrap each item in
`<item n>` tags. The answer is untrusted: unknown stages/categories are
coerced, `"null"`/`"N/A"` become empty, links must be http(s), a round with no
company is **rejected** (logged as `validation`).

Failures: a budget stop or Bedrock outage stops structuring for the run (items
stay pending, attempts unchanged); an unreadable answer, or an item the model
left out, counts an attempt; after `max_item_attempts` (3) the item is
rejected and logged as `model`.

### 3.7 Dedup Stage 2 and Storage — `pipeline/storage.py`

- **Funding:** same company (case, punctuation, "Pvt Ltd" aside) at the same
  stage within `duplicate_days` (14) is one deal. Undated reports are never merged.
- **Events:** same title (case and punctuation aside) on the same day in
  India, or the same link.

A duplicate is folded in: missing details filled, investors combined, the
report appended to `sources`. The row's own `source_url` and date are the most
trusted report — funding: news feed › web search › LinkedIn; events: Luma ›
Eventbrite › other portals. City names are canonical (Bangalore → Bengaluru).

Each item's rows and its final status (`stored`, `irrelevant`, `blocked`,
`rejected`) are written in **one transaction**.

### 3.8 Blocked / Rejected log — `pipeline/blocklog.py`

`blocked_items`: stage (`guardrail_raw`, `guardrail_input`,
`guardrail_output`, `validation`, `model`), source, URL, title, reason. Logged
once per stage, URL and title. Read with `GET /api/blocked?stage=`.

---

## 4. Platform

| Endpoint | Purpose |
|---|---|
| `GET /api/funding?stage=&source=&start=&end=&q=` | funded startups, newest first, counts per stage |
| `GET /api/events?when=upcoming\|past\|all&city=&start=&end=&q=` | events; upcoming soonest first; counts per city, upcoming and past |
| `GET /api/export/funding.xlsx`, `/api/export/events.xlsx` | the same filters, every matching row, as Excel |
| `GET /api/status` | freshness, last run, guardrail state, per-source health, budget |
| `POST /api/refresh`, `GET /api/refresh` | Manual Refresh |
| `GET /api/blocked` | the blocked / rejected log |
| `GET /api/sources?section=funding\|events`, `POST /api/sources`, `DELETE /api/sources/{id}` | the Sources panel: list with health, add, remove |

Dates are IST days, inclusive. An event is upcoming until it ends; one with
no end until its start; a day-only event until that day is over.

**Excel.** One sheet per file, frozen filterable header, the source URL as a
clickable link, dates as real Excel dates in IST. No cell is ever written as
a formula: scraped text starting with `=` would otherwise run when the file
opens.

**Frontend** (`frontend/src`): two feeds (Funded startups, Events in India)
with stage / when tabs, date, source and city filters, search, Export to
Excel, Refresh now, a Sources panel on Funded startups, and a health line
(guardrails, budget, failing sources).
It follows scheduled runs by polling `/api/status` once a minute.

Hardening carried over: the SPA route never serves a file outside `dist/`;
security headers on every response; a 500 never carries a traceback; LIKE
search takes `%` and `_` literally; request filters are length-capped.
**There is no authentication** — bind to `127.0.0.1` or put the server behind
an authenticating proxy before exposing it.

---

## 5. Data

New tables (created by `init_db()`; earlier tables are left untouched, and
`llm_calls` — the spend ledger — is shared, so the $7 cap counts all past spend):

```
seen_items         url_hash (unique), content_hash, item text + payload, status, attempts
guardrail_verdicts stage + text hash (unique), allowed, reason
blocked_items      stage, source, url, title, reason
funded_startups    company, stage, round, amount, investors[], sector, hq_city, summary,
                   source_url, source_label, source_type, published_at, sources[]
startup_events     title, starts_at, ends_at, date_only, city, venue, online, organiser,
                   category, price, summary, url, source_url, source_type, sources[]
user_sources       sources added on the page: kind, value, label, how a website is read
pipeline_runs      one row per run, with every stage's counts and spend
source_runs        one row per source per run: items, cursor, cost, warning, error
```

---

## 6. Cost model

| Item | Cost |
|---|---|
| RSS, dedup, storage, the platform, Excel | $0 |
| LLM structuring | ~$0.00005 an item; only new items |
| Guardrails (when on) | ~$0.00015 per 1,000 chars per policy, once per text and stage |
| LinkedIn | $0.002 a new post; ~$0.001 a quiet day per account |
| Luma | ≤ 40 events/day ≈ $2.60/month |
| Eventbrite | ≤ 40 events/day ≈ $1.15/month |
| Web search | 4 queries × 20 results ≈ 8–16 Firecrawl credits a day |

**First run on an empty index** reads every source in full: ~164 RSS items,
up to 80 web results, 20 LinkedIn posts, up to 80 events — about 15–20
structuring calls, **~$0.02–0.04 of Bedrock**, and ~$0.15 of Apify.

---

## 7. Running it

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd frontend; npm install; npm run build; cd ..
```

Copy `.env.example` to `.env`: `AWS_BEARER_TOKEN_BEDROCK`, `APIFY_API_TOKEN`,
`FIRECRAWL_API_KEY`.

```powershell
Push-Location backend
..\.venv\Scripts\python.exe -m app.llm --preflight             # credentials, free
..\.venv\Scripts\python.exe -m app.pipeline --once --free-only --no-llm   # RSS only, $0
..\.venv\Scripts\python.exe -m app.pipeline --once             # a full run (paid)
..\.venv\Scripts\python.exe -m app.pipeline.guardrails --status
..\.venv\Scripts\python.exe -m app.llm                         # where the money went
Pop-Location

# serve at http://127.0.0.1:8000; the 12h schedule runs inside it
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --app-dir backend
```

---

## 8. Testing

`pytest tests` — **136 tests, all offline**: Bedrock, Apify and Firecrawl are
stubbed, and a test that forgets to stub Bedrock fails instead of calling AWS.

| File | Covers |
|---|---|
| `test_sources.py` | each adapter's mapping, caps, costs and errors; URL normalisation; registry validation |
| `test_guardrails.py` | off / misconfigured / blocked / masked / cached / unreachable / budget; pricing; setup config |
| `test_stages.py` | Dedup Stage 1, event updates, structuring validation and failure kinds, Dedup Stage 2 merges |
| `test_pipeline.py` | whole runs: every stage, rerun is free, failing source isolated, due-ness, free-only, budget stop and resume, rejection after max attempts, guardrails #1–#3 in a run, broken registry |
| `test_user_sources.py` | the network guard (private addresses, rebinding), feed-or-page detection, add/refuse/cap/remove, added sources in the run, rounds read off a page, the API |
| `test_api.py` | feeds and counts, upcoming/past split, Excel content and formula safety, status, refresh, log |
| `test_scheduler.py`, `test_hardening.py`, `test_budget.py` | scheduling and locks, static serving, headers, budget rails, caches |

Checked live on 2026-10-16 at $0: the five RSS feeds (164 items) through
Guardrail #1 (off), Dedup Stage 1 and the index; a second run found all 164
already seen. The paid sources and the LLM stage have not been run live on
this branch.

Checked live on 2026-10-17 at $0: adding TechCrunch Venture found its section
feed and a free run read 20 items from it; Inc42 Buzz resolved to its own
feed; VCCircle (no feed) fell to Firecrawl; `127.0.0.1` and the cloud metadata
address were refused.

---

## 9. Known gaps

| Gap | Notes |
|---|---|
| Paid paths not yet run live | The Eventbrite actor's output was read from its documentation; check the first real run's `source_runs` and `blocked_items`. |
| Guardrails off | Built and tested; switch on with the setup command when there is budget. |
| Event title dedupe is exact | The same event named differently on two portals stays two rows (errs toward showing both). |
| No authentication | See §4. |
