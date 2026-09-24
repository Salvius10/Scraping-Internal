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

    # --- Intelligence (real-time search of our own six sources) ---
    live_search_timeout: float = 8.0  # per source; a slow site is dropped
    live_search_ttl: int = 600        # seconds a live result is reused

    # --- HTTP ---
    user_agent: str = (
        "IndiaStartupNewsBot/0.1 (+aggregator; contact: melvinsalvius@gmail.com)"
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
