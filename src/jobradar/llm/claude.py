"""Claude (Anthropic) adapter for LlmProvider. Default provider (decision D1, ADR-0003)."""

import time

import anthropic
from anthropic.types import JSONOutputFormatParam
from pydantic import BaseModel, ValidationError

from jobradar.llm.base import (
    FatalProviderError,
    InvalidOutputError,
    LlmResponse,
    ProviderError,
    RefusedError,
    Usage,
)

MODEL = "claude-haiku-5-5"

# Room for the answer (~300-600 tokens) plus the model's thinking. Hitting the limit truncates the
# JSON, which then fails validation, so it's set well above what an answer needs.
MAX_TOKENS = 4096

# Errors no retry can fix: a wrong or revoked key (401), a key without access (403), a model name
# the API doesn't know (404).
_FATAL = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError)


class ClaudeProvider:
    """Calls the Messages API with a JSON schema as the required output format."""

    name = "claude"
    # The lowest published rate-limit tier allows 50 requests a minute; staying at it avoids 429s.
    requests_per_minute = 50.0

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = MODEL,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        # max_retries: the SDK itself retries connection errors, 429 (rate limited), 529
        # (overloaded) and 5xx with exponential backoff, honouring the server's retry-after header.
        # `client` lets tests pass one wired to a fake HTTP transport.
        self._client = client or anthropic.Anthropic(api_key=api_key, max_retries=4)

    def extract[T: BaseModel](self, instructions: str, posting_text: str, schema: type[T]) -> LlmResponse[T]:
        # transform_schema() adapts Pydantic's JSON schema to what the API accepts (for example,
        # additionalProperties: false on every object, and constraints like maxLength removed,
        # since the API can't enforce them; our own validation below still does). It's the same
        # step client.messages.parse() runs; we call messages.create() instead so that an answer
        # that fails validation reaches us as raw text, which llm_calls keeps for debugging.
        # JSONOutputFormatParam is the SDK's TypedDict for this dict: pyright then checks its keys.
        output_format: JSONOutputFormatParam = {
            "type": "json_schema",
            "schema": anthropic.transform_schema(schema.model_json_schema()),
        }

        started = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                # cache_control marks the end of a prefix to cache: everything up to here (the
                # instructions, plus the schema the API adds as system text, ~3,300 tokens) is
                # stored for 5 minutes after each use. Later calls read it at 0.1x the input price
                # instead of paying for it again; the first write costs 1.25x. Only the posting,
                # which comes after, is billed at the full price. The prefix must be byte-identical
                # to hit the cache, which is why nothing variable (dates, IDs) goes in instructions.
                system=[{"type": "text", "text": instructions, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": posting_text}],
                # effort: how much the model thinks before answering (and so how many tokens it
                # spends). Extraction is reading, not reasoning: "low" is enough. Current Claude
                # models don't accept `temperature`; the schema and low effort keep answers stable.
                output_config={"format": output_format, "effort": "low"},
            )
        except _FATAL as exc:
            raise FatalProviderError(f"{type(exc).__name__}: {exc}", model=self.model) from exc
        except anthropic.APIError as exc:  # anything the SDK couldn't recover from with its retries
            raise ProviderError(f"{type(exc).__name__}: {exc}", model=self.model) from exc
        latency_ms = round((time.monotonic() - started) * 1000)

        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens,  # Claude reports the uncached part only
            output_tokens=u.output_tokens,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
        )
        # The answer is in the text blocks; a "thinking" block (usually empty: reasoning isn't shown
        # by default) may come first and is skipped.
        raw_text = "".join(block.text for block in response.content if block.type == "text")
        details = {"model": self.model, "usage": usage, "raw_text": raw_text, "latency_ms": latency_ms}

        # Check why generation stopped before reading the answer as JSON.
        if response.stop_reason == "refusal":
            raise RefusedError("the model declined to answer", **details)
        if response.stop_reason == "max_tokens":
            raise InvalidOutputError(f"answer cut off at {MAX_TOKENS} tokens", **details)
        try:
            parsed = schema.model_validate_json(raw_text)
        except ValidationError as exc:
            raise InvalidOutputError(str(exc), **details) from exc
        return LlmResponse(
            parsed=parsed, raw_text=raw_text, usage=usage, model=self.model, latency_ms=latency_ms
        )
