"""Whole runs through every stage, with fake sources and a stubbed LLM. Offline.

Protected: the stages run in the order drawn; a second run over the same
items costs nothing; one failing source never stops the others; a source
read recently is skipped; Guardrail #1 blocks go to the log and an
unreachable guardrail holds items without losing them; a spent budget leaves
items pending for the next run; an item the model never answers is rejected
after `max_item_attempts`; Guardrail #2 and #3 blocks never reach storage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db import session_scope
from app.llm import bedrock
from app.llm.budget import BudgetExceeded
from app.models import (
    BlockedItem, FundedStartup, ItemStatus, PipelineRun, SeenItem, SourceRun, StartupEvent,
)
from app.pipeline import guardrails, run as pipeline_run, structure
from app.pipeline.items import EVENT, FUNDING, RawItem
from app.pipeline.sources import FetchContext, FetchResult, Source, SourceError
from app.pipeline.sources.events_text import event_text

NOW = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class FakeSource(Source):
    items: tuple = ()
    fail: str | None = None

    def fetch(self, ctx: FetchContext) -> FetchResult:
        if self.fail:
            raise SourceError(self.fail)
        return FetchResult(items=list(self.items))


def news(n: int, title: str) -> RawItem:
    return RawItem(kind=FUNDING, source_key="rss:a", source_type="rss", source_label="A News",
                   url=f"https://a.test/{n}", title=title, text=f"{title}. Details {n}.",
                   published_at=NOW)


def meetup() -> RawItem:
    payload = {"url": "https://lu.ma/m", "title": "Pitch Night", "starts_at": "2099-11-01T12:30:00+00:00",
               "ends_at": None, "date_only": False, "venue": "Hub", "city": "Mumbai",
               "country_code": "IN", "online": False, "organiser": "Club", "price": "Free"}
    return RawItem(kind=EVENT, source_key="luma", source_type="luma", source_label="Luma",
                   url="https://lu.ma/m", title="Pitch Night", text=event_text(payload),
                   payload=payload)


class FakeModel:
    """Answers like gpt-oss: a round for every story whose title says 'raises'."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.skip: set[str] = set()
        self.fail: Exception | None = None

    def __call__(self, feature, prompt, *, system=None, max_tokens=0, premium=False):
        self.calls.append(feature)
        if self.fail:
            raise self.fail
        items = re.findall(r"<item (\d+)>\n(.*?)\n</item \1>", prompt, re.S)
        answer = []
        for number, body in items:
            title = body.split("\n")[1]
            if title in self.skip:
                continue
            if feature == "structure_funding":
                rounds = []
                if " raises " in title:
                    rounds = [{"company": title.split(" raises ")[0], "stage": "Seed",
                               "round": "Seed", "amount": "$1M", "investors": ["Fund"],
                               "summary": title}]
                answer.append({"i": int(number), "rounds": rounds})
            else:
                answer.append({"i": int(number), "relevant": True, "in_india": True,
                               "category": "Pitch / Demo day", "summary": "Pitches."})
        result = type("R", (), {"cost_usd": 0.0001})()
        return json.loads(json.dumps(answer)), result


@pytest.fixture
def model(monkeypatch, clean_db) -> FakeModel:
    fake = FakeModel()
    monkeypatch.setattr(structure, "invoke_json", fake)
    return fake


def use_sources(monkeypatch, *sources: Source) -> None:
    monkeypatch.setattr(pipeline_run, "all_sources", lambda: list(sources))
    # The fakes call no service, but the orchestrator skips Apify-backed types
    # when no token is set.
    monkeypatch.setattr(settings, "apify_api_token", "test-token")


def rss(*items, key="rss:a", fail=None, every=0.0) -> FakeSource:
    return FakeSource(key=key, type="rss", label="A News", every_hours=every, paid=False,
                      items=tuple(items), fail=fail)


def count(model_cls, *where) -> int:
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(model_cls).where(*where)) or 0


def test_a_run_goes_through_every_stage_and_a_rerun_is_free(monkeypatch, model) -> None:
    luma = FakeSource(key="luma", type="luma", label="Luma", every_hours=0, paid=False,
                      items=(meetup(),))
    use_sources(monkeypatch, rss(news(1, "Acme raises $1M"), news(2, "Markets fell today")), luma)

    run = pipeline_run.run_pipeline("test")
    assert run.error is None
    assert (run.fetched, run.admitted, run.structured) == (3, 3, 3)
    assert (run.funding_new, run.events_new, run.irrelevant) == (1, 1, 1)
    assert run.pending_left == 0 and run.llm_usd == pytest.approx(0.0002)
    with session_scope() as s:
        row = s.scalars(select(FundedStartup)).one()
        event = s.scalars(select(StartupEvent)).one()
    assert row.company == "Acme" and row.source_url == "https://a.test/1"
    assert row.published_at == NOW
    assert event.city == "Mumbai" and event.category == "Pitch / Demo day"
    assert sorted(model.calls) == ["structure_events", "structure_funding"]

    model.calls.clear()
    again = pipeline_run.run_pipeline("test")
    assert (again.admitted, again.already_seen) == (0, 3)
    assert model.calls == []                       # nothing new: no model call


def test_a_failing_source_does_not_stop_the_others(monkeypatch, model) -> None:
    use_sources(monkeypatch, rss(news(1, "Acme raises $1M")),
                rss(key="rss:b", fail="the feed answered HTTP 503."))
    run = pipeline_run.run_pipeline("test")
    assert run.error is None and run.funding_new == 1
    assert pipeline_run.failed_sources(run.id) == ["rss:b"]
    assert "Could not read rss:b" in pipeline_run.describe(run)


def test_a_source_read_recently_is_skipped(monkeypatch, model) -> None:
    source = rss(news(1, "Acme raises $1M"), every=24)
    use_sources(monkeypatch, source)
    pipeline_run.run_pipeline("test")
    second = pipeline_run.run_pipeline("test")
    with session_scope() as s:
        runs = s.scalars(select(SourceRun).where(SourceRun.run_id == second.id)).all()
    assert runs[0].skipped and "every 24h" in runs[0].skipped
    forced = pipeline_run.run_pipeline("test", force=True)
    assert forced.already_seen == 1


def test_free_only_skips_paid_sources(monkeypatch, model) -> None:
    paid = FakeSource(key="luma", type="luma", label="Luma", every_hours=0, paid=True,
                      items=(meetup(),))
    use_sources(monkeypatch, paid)
    run = pipeline_run.run_pipeline("test", free_only=True)
    assert run.fetched == 0
    with session_scope() as s:
        assert "free sources only" in s.scalars(select(SourceRun)).one().skipped


def test_missing_apify_token_skips_without_a_call(monkeypatch, model) -> None:
    calls = []

    @dataclass(frozen=True)
    class Counting(FakeSource):
        def fetch(self, ctx):
            calls.append(1)
            return FetchResult()

    use_sources(monkeypatch, Counting(key="luma", type="luma", label="Luma", every_hours=0,
                                      paid=True))
    monkeypatch.setattr(settings, "apify_api_token", None)
    pipeline_run.run_pipeline("test")
    assert calls == []


def test_spent_budget_leaves_items_for_the_next_run(monkeypatch, model) -> None:
    use_sources(monkeypatch, rss(news(1, "Acme raises $1M")))
    model.fail = BudgetExceeded("daily cap reached", None)
    run = pipeline_run.run_pipeline("test")
    assert run.stopped_reason and "daily cap" in run.stopped_reason
    assert run.pending_left == 1 and run.funding_new == 0
    with session_scope() as s:
        assert s.scalars(select(SeenItem)).one().attempts == 0      # not the item's fault

    model.fail = None
    later = pipeline_run.run_pipeline("test")
    assert later.funding_new == 1 and later.pending_left == 0


def test_an_item_the_model_never_answers_is_rejected(monkeypatch, model) -> None:
    use_sources(monkeypatch, rss(news(1, "Acme raises $1M"), news(2, "Zeta raises $2M")))
    model.skip = {"Zeta raises $2M"}
    for _ in range(settings.max_item_attempts):
        pipeline_run.run_pipeline("test")
    assert count(SeenItem, SeenItem.status == ItemStatus.REJECTED) == 1
    with session_scope() as s:
        logged = s.scalars(select(BlockedItem)).one()
    assert logged.stage == "model" and logged.url == "https://a.test/2"
    assert count(FundedStartup) == 1


# --- Guardrails in the run -------------------------------------------------------------

def guardrail(monkeypatch, decide) -> list:
    """Switch guardrails on with a fake ApplyGuardrail. `decide(source, text)` -> blocked?"""
    monkeypatch.setattr(settings, "guardrails_enabled", True)
    monkeypatch.setattr(settings, "guardrail_id", "gr-test")
    seen_requests = []

    class Runtime:
        def apply_guardrail(self, **request):
            text = request["content"][0]["text"]["text"]
            seen_requests.append((request["source"], text))
            outcome = decide(request["source"], text)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome:
                return {"action": "GUARDRAIL_INTERVENED", "usage": {"contentPolicyUnits": 1},
                        "assessments": [{"contentPolicy": {"filters": [
                            {"type": "HATE", "confidence": "HIGH", "action": "BLOCKED"}]}}]}
            return {"action": "NONE", "assessments": [], "usage": {"contentPolicyUnits": 1}}

    monkeypatch.setattr(bedrock, "get_client", lambda: Runtime())
    return seen_requests


def test_guardrail_1_blocks_raw_content_once_and_logs_it(monkeypatch, model) -> None:
    requests = guardrail(monkeypatch, lambda source, text: "Hateful" in text)
    use_sources(monkeypatch, rss(news(1, "Acme raises $1M"), news(2, "Hateful screed raises $1M")))
    run = pipeline_run.run_pipeline("test")
    assert run.blocked_raw == 1 and run.admitted == 1 and run.funding_new == 1
    with session_scope() as s:
        logged = s.scalars(select(BlockedItem)).one()
    assert logged.stage == "guardrail_raw" and "HATE" in logged.reason
    # Raw, LLM input and LLM output were each screened for the stored story.
    assert {source for source, _ in requests} == {"INPUT", "OUTPUT"}

    before = len(requests)
    again = pipeline_run.run_pipeline("test")
    assert again.blocked_raw == 1 and count(BlockedItem) == 1     # logged once
    assert len(requests) == before                                # verdicts cached: free


def test_unreachable_guardrail_holds_items_and_rereads_the_source(monkeypatch, model) -> None:
    down = Exception("down")
    down.response = {"Error": {"Code": "ServiceUnavailableException"}}
    guardrail(monkeypatch, lambda source, text: down)
    use_sources(monkeypatch, rss(news(1, "Acme raises $1M"), every=24))
    run = pipeline_run.run_pipeline("test")
    assert run.admitted == 0 and count(FundedStartup) == 0
    with session_scope() as s:
        source_run = s.scalars(select(SourceRun)).one()
    assert "Guardrail #1 could not check 1 items" in source_run.error
    assert source_run.cursor is None

    guardrail(monkeypatch, lambda source, text: False)
    later = pipeline_run.run_pipeline("test")          # not skipped: the last read failed
    assert later.admitted == 1 and later.funding_new == 1


def test_guardrail_2_and_3_keep_blocked_content_out_of_storage(monkeypatch, model) -> None:
    def decide(source, text):
        if source == "INPUT" and text.startswith("(News story") and "Bad input" in text:
            return True                                   # Guardrail #2
        return source == "OUTPUT" and "Evil Corp" in text   # Guardrail #3
    guardrail(monkeypatch, decide)
    use_sources(monkeypatch, rss(news(1, "Bad input raises $1M"), news(2, "Evil Corp raises $1M"),
                                 news(3, "Acme raises $1M")))
    run = pipeline_run.run_pipeline("test")
    assert (run.blocked_input, run.blocked_output, run.funding_new) == (1, 1, 1)
    with session_scope() as s:
        stages = sorted(s.scalars(select(BlockedItem.stage)).all())
        assert [r.company for r in s.scalars(select(FundedStartup)).all()] == ["Acme"]
    assert stages == ["guardrail_input", "guardrail_output"]
    assert count(SeenItem, SeenItem.status == ItemStatus.BLOCKED) == 2


def test_a_broken_registry_fails_the_run_in_words(monkeypatch, model) -> None:
    from app.pipeline.registry import RegistryError

    def broken():
        raise RegistryError("duplicate source keys: rss:a")
    monkeypatch.setattr(pipeline_run, "all_sources", broken)
    run = pipeline_run.run_pipeline("test")
    assert run.error == "duplicate source keys: rss:a" and run.finished_at is not None
    assert pipeline_run.describe(run).startswith("The refresh failed")
    with session_scope() as s:
        assert s.get(PipelineRun, run.id).error == run.error


def test_guardrails_state_is_off_in_tests() -> None:
    assert not guardrails.enabled()
