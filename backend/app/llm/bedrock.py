"""The only place in this codebase that talks to a model.

Every call goes through `invoke()`, which checks the budget before spending and
ledgers the real token usage afterwards. Nothing else may construct a Bedrock
client -- that rule is what makes the spend figures trustworthy.

Two models, chosen deliberately:
  gpt-oss-120b     $0.15 / $0.60 per 1M  -- all bulk mechanical work
  Sonnet 4.6       $3.00 / $15.00 per 1M -- user-facing prose, always opt-in

Sonnet is 20x the input cost and 25x the output cost, so `premium=True` is
never a default anywhere.

We use Bedrock's Converse API rather than a provider SDK because it is one
code path for both an OpenAI open-weight model and an Anthropic model, and it
reports exact token usage for the ledger.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass

from ..config import settings
from .budget import (
    BudgetExceeded, cost_of, ensure_budget, estimate_cost, record_call,
)

log = logging.getLogger(__name__)

_client = None
_client_lock = threading.Lock()

# Transient Bedrock failures worth one retry.
_RETRYABLE = ("ThrottlingException", "ServiceUnavailableException",
              "ModelTimeoutException", "InternalServerException")


class LlmUnavailable(RuntimeError):
    """Bedrock could not be reached or the model is not enabled."""


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float

    def json(self) -> object:
        """Parse the response as JSON, tolerating markdown fences."""
        return parse_json(self.text)


def using_bearer_token() -> bool:
    """True when auth is a Bedrock API key rather than IAM credentials."""
    return bool(settings.aws_bearer_token_bedrock)


def _apply_bearer_token() -> None:
    """Publish the Bedrock API key where botocore looks for it.

    botocore reads AWS_BEARER_TOKEN_BEDROCK from the environment, but
    pydantic-settings loads .env into `settings` rather than os.environ, so
    the value has to be forwarded explicitly.
    """
    if settings.aws_bearer_token_bedrock:
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = settings.aws_bearer_token_bedrock


def _boto_session():
    """Build a boto3 session from .env settings, else the default AWS chain.

    pydantic-settings loads .env into `settings`, not into os.environ, so
    credentials configured there must be handed to boto3 explicitly.
    """
    import boto3

    _apply_bearer_token()

    if settings.aws_bearer_token_bedrock:
        # Bearer auth carries no IAM identity; the session just supplies region.
        return boto3.Session(region_name=settings.aws_region)

    if settings.aws_profile:
        return boto3.Session(profile_name=settings.aws_profile,
                             region_name=settings.aws_region)

    if settings.aws_access_key_id and settings.aws_secret_access_key:
        return boto3.Session(
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            aws_session_token=settings.aws_session_token,
            region_name=settings.aws_region,
        )

    return boto3.Session(region_name=settings.aws_region)


def get_client():
    """Lazily build the Bedrock runtime client."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                try:
                    _client = _boto_session().client("bedrock-runtime")
                except Exception as exc:  # noqa: BLE001
                    raise LlmUnavailable(
                        f"could not create Bedrock client: {exc}"
                    ) from exc
    return _client


def reset_client() -> None:
    """Drop the cached client, e.g. after changing credentials."""
    global _client
    with _client_lock:
        _client = None


def whoami() -> dict:
    """Which AWS identity is active. Free -- STS, not Bedrock.

    A Bedrock API key is bearer auth scoped to Bedrock, so STS cannot answer
    for it; that is reported rather than treated as a failure.
    """
    if using_bearer_token():
        token = settings.aws_bearer_token_bedrock or ""
        return {
            "account": "n/a (Bedrock API key)",
            "arn": "n/a - bearer token, scoped to Bedrock only",
            "region": settings.aws_region,
            "source": "Bedrock API key from .env (%s..., %d chars)" % (
                token[:4], len(token)),
        }
    try:
        sts = _boto_session().client("sts")
        ident = sts.get_caller_identity()
        return {
            "account": ident.get("Account"),
            "arn": ident.get("Arn"),
            "region": settings.aws_region,
            "source": (
                "profile:" + settings.aws_profile if settings.aws_profile
                else ".env keys" if settings.aws_access_key_id
                else "default AWS chain (~/.aws)"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        raise LlmUnavailable(_explain(exc, "sts")) from exc


def _extract_text(response: dict) -> str:
    """Pull the answer out of a Converse response.

    gpt-oss-120b is a reasoning model: its content list holds a
    `reasoningContent` block *before* the `text` block, so indexing content[0]
    returns the chain of thought (or nothing). Collect every text block and
    ignore reasoning entirely.
    """
    try:
        blocks = response["output"]["message"]["content"]
    except (KeyError, TypeError):
        return ""

    parts = [b["text"] for b in blocks
             if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(parts).strip()


def parse_json(text: str) -> object:
    """Extract JSON from a model response.

    Models wrap JSON in ```json fences or add a sentence before it, so a bare
    json.loads is not enough.
    """
    raw = (text or "").strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost array or object in the response.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                continue

    raise ValueError(f"response was not JSON: {raw[:200]!r}")


def invoke(
    feature: str,
    prompt: str,
    *,
    system: str | None = None,
    premium: bool = False,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> LlmResult:
    """Call a model, metered and capped.

    Args:
        feature: ledger label, e.g. "enrich" or "intelligence". Make it stable
            -- it is how spend is attributed in the report.
        premium: route to Sonnet 4.6 instead of gpt-oss-120b. Costs ~21x.
        reasoning_effort: gpt-oss only. Defaults to settings.reasoning_effort
            ("low"); raise it to "medium"/"high" for genuinely hard judgement.

    Raises:
        BudgetExceeded: the call was refused; nothing was spent.
        LlmUnavailable: Bedrock could not be reached.
    """
    model = model or (settings.model_premium if premium else settings.model_cheap)
    reasoning_effort = reasoning_effort or settings.reasoning_effort

    prompt_chars = len(prompt) + len(system or "")
    estimated = estimate_cost(model, prompt_chars, max_tokens)
    ensure_budget(estimated)  # raises before any money moves

    if settings.llm_dry_run:
        log.warning("LLM_DRY_RUN is on -- returning a stub for %r", feature)
        record_call(feature, model, 0, 0, ok=True, note="dry run")
        return LlmResult(text="", model=model, tokens_in=0, tokens_out=0,
                         cost_usd=0.0)

    client = get_client()
    request = {
        "modelId": model,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    if system:
        request["system"] = [{"text": system}]

    # gpt-oss bills its reasoning as output tokens; turning the effort down
    # cuts that ~3x on mechanical work. The field is gpt-oss-specific -- do
    # not send it to Sonnet.
    if model == settings.model_cheap and reasoning_effort:
        request["additionalModelRequestFields"] = {
            "reasoning_effort": reasoning_effort
        }

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = client.converse(**request)
            break
        except Exception as exc:  # noqa: BLE001 - boto3 raises botocore types
            last_error = exc
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in _RETRYABLE and attempt == 0:
                time.sleep(1.5)
                continue
            record_call(feature, model, 0, 0, ok=False,
                        note=f"{type(exc).__name__}: {exc}"[:500])
            raise LlmUnavailable(_explain(exc, model)) from exc
    else:  # pragma: no cover - loop always breaks or raises
        raise LlmUnavailable(str(last_error))

    usage = response.get("usage", {})
    tokens_in = int(usage.get("inputTokens", 0))
    tokens_out = int(usage.get("outputTokens", 0))

    text = _extract_text(response)

    cost = record_call(feature, model, tokens_in, tokens_out, ok=True)
    return LlmResult(text=text, model=model, tokens_in=tokens_in,
                     tokens_out=tokens_out, cost_usd=cost)


def _explain(exc: Exception, model: str) -> str:
    """Turn an opaque boto3 error into something actionable."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    region = settings.aws_region

    if code == "AccessDeniedException":
        return (
            f"Bedrock denied access to {model} in {region}. Enable the model "
            f"under Bedrock > Model access in the AWS console for that region, "
            f"and check the IAM policy allows bedrock:InvokeModel."
        )
    if code == "ValidationException":
        return (
            f"Bedrock rejected model id {model!r} in {region}. Verify the id "
            f"and that the model is offered in this region."
        )
    if code == "ResourceNotFoundException":
        return f"Model {model!r} not found in {region}."
    if "NoCredentials" in type(exc).__name__ or code == "UnrecognizedClientException":
        return (
            "No usable AWS credentials. Run `aws configure`, or set "
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, or set LLM_DRY_RUN=true "
            "in .env to work without Bedrock."
        )
    return f"Bedrock call failed ({code or type(exc).__name__}): {exc}"


def invoke_json(
    feature: str,
    prompt: str,
    *,
    system: str | None = None,
    premium: bool = False,
    max_tokens: int = 1024,
) -> tuple[object, LlmResult]:
    """invoke() plus JSON parsing. Returns (parsed, result)."""
    result = invoke(
        feature, prompt, system=system, premium=premium,
        max_tokens=max_tokens, temperature=0.0,
    )
    return result.json(), result


__all__ = [
    "BudgetExceeded", "LlmResult", "LlmUnavailable",
    "cost_of", "invoke", "invoke_json", "parse_json",
]
