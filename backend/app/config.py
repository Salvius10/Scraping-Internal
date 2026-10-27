"""Central settings. Every tunable that affects spend lives here."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Storage ---
    db_path: Path = REPO_DIR / "news.db"
    # The source registry: every feed, account, query and event portal the
    # orchestrator fans out to. The only file to edit to add or drop a source.
    sources_file: Path = BACKEND_DIR / "app" / "pipeline" / "sources.yaml"

    # --- Scheduler ---
    refresh_hours: int = 12           # the scheduled run, per the architecture

    # Run the 12h refresh inside the API process. Turn off when the pipeline
    # is run by `python -m app.scheduler` or by hand, so two processes do not race.
    scheduler_enabled: bool = True

    # "Refresh now": a refresh started less than this long ago (by hand or by
    # the scheduler) is not started again, so repeated clicks cannot keep
    # paying for model calls and Apify runs.
    manual_refresh_cooldown_minutes: int = 10

    # --- Pipeline ---
    fetch_workers: int = 4            # sources fetched at once by the orchestrator
    guardrail_workers: int = 4        # guardrail checks in flight at once
    llm_batch_size: int = 20          # items per structuring call
    # Items the model failed to answer are retried on later runs, at most this
    # many times; after that they are logged as rejected.
    max_item_attempts: int = 3
    # Upper bound on items structured in one run, so a backlog (a new source,
    # a long outage) is worked through over several runs instead of one bill.
    max_items_per_run: int = 400
    # Two funding rows are one deal when they name the same company at the same
    # stage within this many days of each other.
    duplicate_days: int = 14

    # Sources added on the platform's Sources panel. Paid ones (LinkedIn,
    # search phrases, websites read through Firecrawl) are read at most this
    # often; a website with a feed is free and read every run. The cap bounds
    # what anyone who can reach the page can add to the bill.
    user_source_hours: int = 24
    max_user_sources: int = 50

    # --- Guardrails (Amazon Bedrock) ---
    # Off until a guardrail exists and there is budget to run it. While off,
    # the three guardrail stages pass every item through unchecked, and the
    # status endpoint says so. `python -m app.pipeline.guardrails --create`
    # makes one and prints the id and version to put here.
    guardrails_enabled: bool = False
    guardrail_id: str | None = None
    guardrail_version: str = "1"
    # Raw content screened by Guardrail #1, per item. A text unit is up to
    # 1,000 characters, so this bounds each raw check at 4 units.
    guardrail_raw_chars: int = 4000
    # Bedrock Guardrails prices, USD per 1,000 text units, per policy type.
    # Word filters and regexes are free. Used for the pre-call cap check and
    # for the ledger, from the units each response reports.
    guardrail_price_content: float = 0.15
    guardrail_price_topic: float = 0.15
    guardrail_price_pii: float = 0.10
    guardrail_price_grounding: float = 0.10

    # --- Web search via hosted Firecrawl ---
    # Billed in Firecrawl credits, separate from the $7 LLM cap: a search of 10
    # results measured 2 credits, a scrape 1 per page. The URL can point at a
    # self-hosted Firecrawl, which needs no key.
    firecrawl_api_key: str | None = None
    firecrawl_api_url: str = "https://api.firecrawl.dev"
    firecrawl_timeout: float = 45.0

    # --- Apify (LinkedIn, Luma, Eventbrite) ---
    # Billed in Apify's own account, separate from the $7 LLM cap. Every run
    # carries both caps: maxItems and maxTotalChargeUsd.
    apify_api_token: str | None = None
    apify_api_url: str = "https://api.apify.com"
    apify_timeout: int = 240          # seconds; the sync endpoint gives up at 300
    apify_max_charge_usd: float = 0.25   # hard ceiling on any one run

    # LinkedIn: "No Cookies" profile-posts actor -- no LinkedIn login.
    # Pricing read 2026-10 (free tier): $0.002 a post, $0.00005 a run start,
    # $0.001 for a read that finds no posts.
    apify_linkedin_actor: str = "harvestapi~linkedin-profile-posts"
    linkedin_price_per_post: float = 0.002
    linkedin_price_per_run: float = 0.00005
    linkedin_price_per_empty: float = 0.001
    linkedin_first_posts: int = 20    # newest posts taken on an account's first read
    linkedin_posts_per_read: int = 20

    # Luma: city discover feeds. $0.002125 an event (free tier), $0.001 a run.
    apify_luma_actor: str = "dami_studio~luma-events-scraper"
    luma_price_per_event: float = 0.002125
    luma_price_per_run: float = 0.001

    # Eventbrite: city search. $0.00092 an event (free tier), $0.0005 a run.
    apify_eventbrite_actor: str = "dami_studio~eventbrite-events-scraper"
    eventbrite_price_per_event: float = 0.00092
    eventbrite_price_per_run: float = 0.0005

    # --- SignalHire (Enrichment: contacts of the funded startups' leaders) ---
    # Billed in SignalHire credits, separate from the $7 LLM cap. A people
    # search is free (it draws on a daily quota); each person whose contacts
    # are looked up costs credits. Lookups run without SignalHire's waterfall,
    # so results come back in the response and no public callback URL is needed.
    signalhire_api_key: str | None = None
    signalhire_api_url: str = "https://www.signalhire.com/api/v1"
    signalhire_timeout: float = 60.0
    # Where the leaders are searched: a country, state or city SignalHire
    # knows. Empty searches everywhere, at the cost of more same-named companies.
    signalhire_location: str = "India"
    signalhire_search_size: int = 30      # profiles read per company search (1-100)
    # The credit cap: at most this many people per company are looked up,
    # founders and the C-suite first.
    signalhire_contacts_per_company: int = 6
    signalhire_max_companies: int = 100   # companies one "Send data for enrichment" queues
    # Waits, in seconds, before retrying a request SignalHire rate-limited
    # (a trial account may look up only 5 people a minute).
    signalhire_retry_waits: tuple[float, ...] = (10.0, 30.0, 60.0)

    # --- HTTP ---
    user_agent: str = (
        "IndiaStartupNewsBot/0.1 (+aggregator; contact: melvinsalvius.26csb@licet.ac.in)"
    )
    http_timeout: float = 20.0

    # --- Bedrock ---
    aws_region: str = "ap-south-1"

    # Credentials. Leave these unset to use the machine's default AWS chain
    # (~/.aws/credentials); set them in .env to pin this project to a specific
    # account without touching the global AWS config. .env is gitignored.
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    aws_profile: str | None = None

    # A Bedrock long-term API key ("ABSK..."). This is bearer-token auth, not
    # SigV4: it is scoped to Bedrock alone, so it cannot call STS and carries
    # no general AWS identity. Takes precedence over the keys above.
    aws_bearer_token_bedrock: str | None = None
    model_cheap: str = "openai.gpt-oss-120b-1:0"
    # Sonnet 4.6 is INFERENCE_PROFILE-only on Bedrock: the bare model id
    # "anthropic.claude-sonnet-4-6" is rejected by converse(). Use the profile.
    model_premium: str = "global.anthropic.claude-sonnet-4-6"

    # gpt-oss-120b is a reasoning model and bills its chain of thought as
    # output tokens. Measured on a classification prompt: "low" spends 14
    # output tokens where the default spends 41 and "high" spends 68 -- all
    # three returning the same answer. Bulk work does not need deliberation.
    reasoning_effort: str = "low"

    # Prices in USD per 1M tokens, used by the spend ledger.
    price_cheap_in: float = 0.15
    price_cheap_out: float = 0.60
    price_premium_in: float = 3.00
    price_premium_out: float = 15.00

    # --- Budget rails ($7 total) ---
    # Guardrail checks are ledgered here too, so the caps cover them.
    total_usd_cap: float = 7.00
    daily_usd_cap: float = 0.50

    # Work without Bedrock credentials: calls return an empty stub and ledger
    # a zero-cost row. Useful for wiring up features before spending anything.
    llm_dry_run: bool = False

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


settings = Settings()
