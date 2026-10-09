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
    sources_file: Path = BACKEND_DIR / "app" / "ingest" / "sources.yaml"

    # --- Feed behaviour ---
    recency_window_days: int = 7      # default window the feed shows
    refresh_hours: int = 12           # scheduler cadence, per the plan
    dedupe_window_hours: int = 72     # how far back to look for the same story
    dedupe_threshold: float = 0.60    # Jaccard over title tokens

    # Run the 12h refresh inside the API process. Turn off when ingest is run
    # by `python -m app.scheduler` or by hand, so two processes do not race.
    scheduler_enabled: bool = True

    # "Refresh now" buttons: a scope refreshed less than this long ago (by hand
    # or by the scheduler) is not refreshed again, so repeated clicks cannot
    # keep paying for enrichment and scraping.
    manual_refresh_cooldown_minutes: int = 10

    # --- Intelligence: web search + on-demand scrape via hosted Firecrawl ---
    # Firecrawl bills in its own credits, separate from the $7 LLM cap: a
    # search of 10 results measured 2 credits, a scrape 1 per page. Set the
    # key in .env (gitignored). The URL can later point at a self-hosted
    # Firecrawl, which needs no key.
    firecrawl_api_key: str | None = None
    firecrawl_api_url: str = "https://api.firecrawl.dev"
    firecrawl_timeout: float = 45.0
    firecrawl_search_limit: int = 10

    # --- Insights: VC firms (ingest/vc_firms.py) ---
    vc_firms_file: Path = BACKEND_DIR / "app" / "ingest" / "vc_firms.yaml"
    # Free reads (feeds, plain HTML) run on every refresh. Firecrawl reads cost
    # credits -- 10 scrapes and 3 maps at 1, 23 searches at ~2: about 59 a round --
    # so they run at most this often.
    vc_firecrawl_hours: int = 24
    vc_items_per_firm: int = 30       # newest posts taken from each read

    # --- Insights: websites pasted on Startup firms / VC firms (ingest/pasted_sources.py) ---
    # A source with a feed or a plain news page is read free on every refresh;
    # one that needs Firecrawl (1 credit a read) at most this often.
    pasted_firecrawl_hours: int = 24
    pasted_items_per_read: int = 30   # newest posts taken from each read

    # --- Insights: Events organised (ingest/events.py) ---
    # Luma calendars are read through an Apify Store actor, billed per event
    # returned in Apify's own account -- separate from the $7 LLM cap, like
    # Firecrawl. Set the token in .env (gitignored).
    apify_api_token: str | None = None
    apify_api_url: str = "https://api.apify.com"
    apify_luma_actor: str = "dami_studio~luma-events-scraper"
    apify_timeout: int = 240          # seconds; the sync endpoint gives up at 300
    # Measured on the actor's page, 2026-10: $2.125 per 1,000 events, and a
    # run start is charged at most $0.001. Used to estimate and cap each run.
    apify_price_per_event: float = 0.002125
    apify_price_per_run: float = 0.001
    # Hard ceiling on what one Apify run may charge, passed as maxTotalChargeUsd.
    apify_max_charge_usd: float = 0.10
    # Each source is read at most this often; a failed read does not count.
    events_hours: int = 24
    # A firm's own home page rarely lists events, so it is checked weekly. It
    # is only sent to the model when its text mentions an event and a date.
    events_site_hours: int = 168
    events_upcoming_items: int = 25   # upcoming events taken per Luma read
    events_past_items: int = 25       # past events, read once per calendar

    # --- LinkedIn (ingest/linkedin.py) ---
    # Profiles and company pages the reader adds are read through an Apify
    # Store actor, billed per post returned in Apify's own account -- separate
    # from the $7 LLM cap. Uses the same APIFY_API_TOKEN as Events.
    apify_linkedin_actor: str = "harvestapi~linkedin-profile-posts"
    # Measured on the actor's pricing, 2026-10 (free tier): $0.002 per post, a
    # run start $0.00005, and $0.001 for a read that finds no posts.
    linkedin_price_per_post: float = 0.002
    linkedin_price_per_run: float = 0.00005
    linkedin_price_per_empty: float = 0.001
    # Each account is read at most this often; a failed read does not count.
    linkedin_hours: int = 24
    # The first read takes this many of the newest posts; later reads take
    # only posts newer than the newest one already stored, up to the second.
    linkedin_first_posts: int = 20
    linkedin_posts_per_read: int = 20

    # --- HTTP ---
    user_agent: str = (
        "IndiaStartupNewsBot/0.1 (+aggregator; contact: melvinsalvius.26csb@licet.ac.in)"
    )
    http_timeout: float = 20.0
    per_domain_delay: float = 2.0     # be a polite guest

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
    total_usd_cap: float = 7.00
    daily_usd_cap: float = 0.50

    # Work without Bedrock credentials: calls return an empty stub and ledger
    # a zero-cost row. Useful for wiring up features before spending anything.
    llm_dry_run: bool = False

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


settings = Settings()
