"""The provider interface: what the rest of the code needs from an LLM, whoever serves it.

Concepts:
- An LLM API takes a list of messages with *roles*: `system` (standing instructions, the rules),
  `user` (the input) and `assistant` (the model's reply). Here the instructions become the system
  message and the posting becomes the user message.
- *Structured output*: besides the messages, we send a JSON schema (generated from a Pydantic
  model) and the provider constrains the model so that its answer is JSON matching that schema.
  We still validate the answer with Pydantic: the schema sent to the API can't express every
  rule (e.g. min <= max), and a refusal or a truncated answer won't match it.
- *Provider abstraction*: the enrich pipeline only knows `LlmProvider.extract()`. Claude, Gemini
  and the fake used in tests are interchangeable implementations, so switching provider is a
  setting (LLM_PROVIDER), and tests never call the network.

Every outcome carries what `llm_calls` records: tokens (a failed call is still billed for them),
latency, the raw answer and the model.
"""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from jobradar.models import LlmCallStatus


@dataclass(frozen=True)
class Usage:
    """Tokens a call consumed, split the way providers bill them.

    - input_tokens: input read at the full price (not from the prompt cache).
    - output_tokens: the answer, including any "thinking" tokens (billed as output).
    - cache_write_tokens / cache_read_tokens: input written to / read from the prompt cache. The
      instructions and schema are the same for every posting, so the provider stores them after
      the first call and later calls read them at a fraction of the price.
    """

    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True)
class LlmResponse[T: BaseModel]:
    """A successful call: the validated answer plus what it cost.

    `[T: BaseModel]` is Python 3.12's generic syntax: an LlmResponse[EnrichmentOut] has
    `parsed: EnrichmentOut`, so the type checker knows the fields of the answer.
    """

    parsed: T
    raw_text: str
    usage: Usage
    model: str
    latency_ms: int


class LlmError(Exception):
    """A call that produced no valid answer. Subclasses say why; `status` is the llm_calls status."""

    status: LlmCallStatus = LlmCallStatus.ERROR

    def __init__(
        self,
        message: str,
        *,
        model: str,
        usage: Usage | None = None,
        raw_text: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.usage = usage  # None when the call failed before the provider reported usage
        self.raw_text = raw_text
        self.latency_ms = latency_ms


class InvalidOutputError(LlmError):
    """The answer isn't valid JSON for the schema, or was cut off by the token limit. Worth a retry."""

    status = LlmCallStatus.INVALID_OUTPUT


class RefusedError(LlmError):
    """The provider declined to answer (a safety filter). Retrying the same input won't help."""

    status = LlmCallStatus.REFUSED


class ProviderError(LlmError):
    """Network or API failure after the SDK's own retries: no answer at all."""

    status = LlmCallStatus.ERROR


class LlmProvider(Protocol):
    """What every provider implements. A Protocol is structural typing ("duck typing" checked by
    pyright): a class qualifies by having these attributes and methods, without inheriting."""

    name: str  # "claude", "gemini", "fake": stored in llm_calls.provider
    model: str  # e.g. "claude-haiku-5-5": stored in llm_calls.model

    def extract[T: BaseModel](self, instructions: str, posting_text: str, schema: type[T]) -> LlmResponse[T]:
        """Ask for `schema`-shaped JSON about `posting_text`, following `instructions`.

        Returns the validated answer, or raises an LlmError subclass.
        """
        ...
