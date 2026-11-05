"""Tests for the LLM layer: provider adapters (against fake HTTP), pricing, rate limiting, the
provider factory and the llm_calls row.

The adapters are tested through their SDKs with a *mock transport*: the SDK builds and sends a
real HTTP request, and a handler function answers it in place of the network. That checks what
we send (model, system message, schema, effort) and how we read each kind of answer, without
calls that cost money or depend on the network. The response bodies are hand-written in each
API's documented shape.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, insert, select

from jobradar.config import Settings
from jobradar.llm import MissingApiKeyError, make_provider
from jobradar.llm.base import InvalidOutputError, LlmError, LlmResponse, ProviderError, RefusedError, Usage
from jobradar.llm.claude import ClaudeProvider
from jobradar.llm.gemini import GeminiProvider
from jobradar.llm.log import record_call
from jobradar.llm.pricing import cost_usd
from jobradar.llm.ratelimit import RateLimiter
from jobradar.models import LlmCall, LlmCallStatus, LlmPurpose, PipelineRun


class Answer(BaseModel):
    """A tiny stand-in schema: the adapters work with any Pydantic model."""

    model_config = ConfigDict(extra="forbid")
    company: str | None
    years: int


VALID = json.dumps({"company": "Acme", "years": 5})


# --- Claude ------------------------------------------------------------------------------------


def claude_message(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    """A Messages API response body, with the (empty by default) thinking block that comes first."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": text},
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 300},
    }


def claude_provider(status: int, body: dict[str, Any], sent: list[dict[str, Any]]) -> ClaudeProvider:
    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(status, json=body)

    # max_retries=0: an error is reported at once instead of retried (with real waits) by the SDK.
    client = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return ClaudeProvider(client=client)


def test_claude_sends_schema_and_low_effort() -> None:
    sent: list[dict[str, Any]] = []
    result = claude_provider(200, claude_message(VALID), sent).extract("Rules.", "A posting.", Answer)

    assert result.parsed == Answer(company="Acme", years=5)
    assert result.usage == Usage(input_tokens=1200, output_tokens=300)
    request = sent[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["system"] == "Rules."
    assert request["messages"] == [{"role": "user", "content": "A posting."}]
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert "temperature" not in request  # current Claude models reject it


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (claude_message("I can't help with that.", stop_reason="refusal"), RefusedError),
        (claude_message('{"company": "Acme", "ye', stop_reason="max_tokens"), InvalidOutputError),
        (claude_message('{"company": "Acme"}'), InvalidOutputError),  # valid JSON, missing a field
    ],
)
def test_claude_failed_answers_keep_usage_and_text(body: dict[str, Any], error: type[LlmError]) -> None:
    with pytest.raises(error) as caught:
        claude_provider(200, body, []).extract("Rules.", "A posting.", Answer)
    # Billed tokens and the raw answer are kept, so llm_calls records them even for failures.
    assert caught.value.usage == Usage(input_tokens=1200, output_tokens=300)
    assert caught.value.raw_text


def test_claude_api_error_becomes_provider_error() -> None:
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    with pytest.raises(ProviderError, match="Overloaded") as caught:
        claude_provider(529, body, []).extract("Rules.", "A posting.", Answer)
    assert caught.value.usage is None


# --- Gemini ------------------------------------------------------------------------------------


def gemini_response(text: str, finish_reason: str = "STOP") -> dict[str, Any]:
    """A generateContent response body (the REST API's camelCase field names)."""
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish_reason}
        ],
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 50},
        "modelVersion": "gemini-3.5-flash",
    }


def gemini_provider(status: int, body: dict[str, Any], sent: list[dict[str, Any]]) -> GeminiProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(status, json=body)

    return GeminiProvider(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_gemini_sends_schema_and_counts_thinking_as_output() -> None:
    sent: list[dict[str, Any]] = []
    result = gemini_provider(200, gemini_response(VALID), sent).extract("Rules.", "A posting.", Answer)

    assert result.parsed == Answer(company="Acme", years=5)
    assert result.usage == Usage(input_tokens=1000, output_tokens=250)  # 200 answer + 50 thinking
    assert sent[0]["url"].endswith("models/gemini-3.5-flash:generateContent")
    config = sent[0]["body"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert "responseSchema" in config or "responseJsonSchema" in config
    assert sent[0]["body"]["systemInstruction"]["parts"][0]["text"] == "Rules."


def test_gemini_blocked_prompt_is_a_refusal() -> None:
    body = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 1000}}
    with pytest.raises(RefusedError, match="prompt blocked"):
        gemini_provider(200, body, []).extract("Rules.", "A posting.", Answer)


def test_gemini_safety_stop_is_a_refusal_and_invalid_json_is_invalid() -> None:
    with pytest.raises(RefusedError):
        gemini_provider(200, gemini_response("", "SAFETY"), []).extract("Rules.", "A posting.", Answer)
    with pytest.raises(InvalidOutputError):
        gemini_provider(200, gemini_response("not json"), []).extract("Rules.", "A posting.", Answer)


def test_gemini_client_error_becomes_provider_error() -> None:
    # 400 isn't retried by the SDK, so the test doesn't wait on backoff.
    body = {"error": {"code": 400, "message": "API key not valid", "status": "INVALID_ARGUMENT"}}
    with pytest.raises(ProviderError, match="API key not valid"):
        gemini_provider(400, body, []).extract("Rules.", "A posting.", Answer)


# --- Pricing, rate limiting, factory ------------------------------------------------------------


def test_cost_is_exact_decimal() -> None:
    # 1,200 input tokens at $0.10/MTok + 300 output at $0.50/MTok = $0.00012 + $0.00015.
    assert cost_usd("claude-haiku-5-5", Usage(1200, 300)) == Decimal("0.00027")
    assert cost_usd("unknown-model", Usage(1, 1)) is None
    assert cost_usd("claude-haiku-5-5", None) is None


def test_rate_limiter_spaces_calls_evenly() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(60, clock=lambda: now[0], sleep=sleep)  # 60/min = one per second
    for _ in range(3):
        limiter.acquire()
    assert sleeps == [1.0, 1.0]  # the first call goes at once, the next two wait their slot


def test_factory_needs_the_providers_key() -> None:
    # _env_file=None: build Settings from these arguments only, ignoring the developer's .env.
    # pydantic-settings accepts it at runtime, but it isn't a declared field, so pyright is told to
    # skip that one check.
    with pytest.raises(MissingApiKeyError, match="ANTHROPIC_API_KEY"):
        make_provider(Settings(_env_file=None, llm_provider="claude", anthropic_api_key=None))  # pyright: ignore[reportCallIssue]
    provider = make_provider(Settings(_env_file=None, llm_provider="gemini", gemini_api_key="k"))  # pyright: ignore[reportCallIssue]
    assert provider.name == "gemini"


# --- llm_calls ---------------------------------------------------------------------------------


@pytest.mark.integration
def test_record_call_stores_cost_and_failures(db_conn: Connection) -> None:
    run_id: uuid.UUID = db_conn.execute(
        insert(PipelineRun).values(pipeline="enrich", trigger="manual").returning(PipelineRun.run_id)
    ).scalar_one()
    ok = LlmResponse(
        parsed=Answer(company="Acme", years=5),
        raw_text=VALID,
        usage=Usage(1200, 300),
        model="claude-haiku-5-5",
        latency_ms=900,
    )
    failed = InvalidOutputError(
        "bad", model="claude-haiku-5-5", usage=Usage(1200, 10), raw_text="oops", latency_ms=500
    )
    common = {
        "run_id": run_id,
        "posting_id": None,
        "purpose": LlmPurpose.ENRICH,
        "provider": "claude",
        "prompt_version": "v1",
    }

    record_call(db_conn, ok, **common)
    record_call(db_conn, ok, **common, status=LlmCallStatus.UNGROUNDED)  # the caller can override
    record_call(db_conn, failed, **common)

    rows = db_conn.execute(select(LlmCall).order_by(LlmCall.id)).all()
    assert [r.status for r in rows] == ["ok", "ungrounded", "invalid_output"]
    assert rows[0].cost_usd == Decimal("0.000270")
    assert rows[0].response == {"company": "Acme", "years": 5}  # valid JSON stored as an object
    assert rows[2].response == {"text": "oops"}  # anything else kept as text
    assert rows[2].error == "bad"
