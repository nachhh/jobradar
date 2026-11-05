"""Gemini (Google) adapter for LlmProvider: the free-tier alternative to Claude (decision D1)."""

import time

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from jobradar.llm.base import InvalidOutputError, LlmResponse, ProviderError, RefusedError, Usage

# The model the owner's ai_hello_world experiments used. Free tier: no charge, but a cap on
# requests per minute and per day (checked in the AI Studio console).
MODEL = "gemini-3.5-flash"

# Finish reasons that mean "blocked by a safety or policy filter", as opposed to a normal stop.
_BLOCKED = {
    types.FinishReason.SAFETY,
    types.FinishReason.BLOCKLIST,
    types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.SPII,
    types.FinishReason.RECITATION,
}


class GeminiProvider:
    """Calls generate_content with a response schema and JSON as the required output type."""

    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = MODEL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        # retry_options: the SDK retries rate limits (429) and server errors with backoff.
        # httpx_client lets tests inject a client with a fake transport (the SDK uses httpx).
        options = types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=4), httpx_client=http_client
        )
        self._client = genai.Client(api_key=api_key, http_options=options)

    def extract[T: BaseModel](self, instructions: str, posting_text: str, schema: type[T]) -> LlmResponse[T]:
        config = types.GenerateContentConfig(
            system_instruction=instructions,
            # Both settings are needed: the MIME type asks for JSON, the schema says which JSON. The
            # SDK converts the Pydantic class into the schema format the API expects.
            response_mime_type="application/json",
            response_schema=schema,
            # Like Claude's effort: little thinking is needed to read a posting.
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        )
        started = time.monotonic()
        try:
            response = self._client.models.generate_content(
                model=self.model, contents=posting_text, config=config
            )
        except errors.APIError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}", model=self.model) from exc
        latency_ms = round((time.monotonic() - started) * 1000)

        meta = response.usage_metadata
        # Thinking tokens are reported apart from the answer's, but billed as output like them.
        output_tokens = ((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)) if meta else 0
        usage = Usage(input_tokens=(meta.prompt_token_count or 0) if meta else 0, output_tokens=output_tokens)
        candidate = response.candidates[0] if response.candidates else None
        raw_text = (
            "".join(p.text or "" for p in candidate.content.parts or [])
            if candidate and candidate.content
            else ""
        )
        details = {"model": self.model, "usage": usage, "raw_text": raw_text, "latency_ms": latency_ms}

        # The whole prompt can be blocked (no candidate at all), or the answer stopped by a filter.
        if response.prompt_feedback and response.prompt_feedback.block_reason:
            raise RefusedError(f"prompt blocked: {response.prompt_feedback.block_reason}", **details)
        if candidate is None:
            raise InvalidOutputError("no answer returned", **details)
        if candidate.finish_reason in _BLOCKED:
            raise RefusedError(f"answer blocked: {candidate.finish_reason}", **details)
        if candidate.finish_reason == types.FinishReason.MAX_TOKENS:
            raise InvalidOutputError("answer cut off by the token limit", **details)
        try:
            parsed = schema.model_validate_json(raw_text)
        except ValidationError as exc:
            raise InvalidOutputError(str(exc), **details) from exc
        return LlmResponse(
            parsed=parsed, raw_text=raw_text, usage=usage, model=self.model, latency_ms=latency_ms
        )
