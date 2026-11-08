"""A fake LlmProvider for tests: answers from a function, never touches the network."""

import json
from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from jobradar.llm.base import InvalidOutputError, LlmResponse, Usage

# The function receives the posting text and returns the answer as a dict (validated against the
# schema like a real answer) or raises an LlmError to simulate a failure. It may be called from
# several threads at once (the enrich pipeline's workers).
Responder = Callable[[str], dict[str, object]]


class FakeProvider:
    name = "fake"
    model = "fake-model"
    requests_per_minute = 60_000.0  # no real limit; tests shouldn't wait

    def __init__(self, respond: Responder) -> None:
        self._respond = respond
        self.calls: list[tuple[str, str]] = []  # (instructions, posting_text) of every call, for asserts

    def extract[T: BaseModel](self, instructions: str, posting_text: str, schema: type[T]) -> LlmResponse[T]:
        self.calls.append((instructions, posting_text))
        raw_text = json.dumps(self._respond(posting_text))
        # Rough token counts (~4 characters per token), enough to exercise cost accounting.
        usage = Usage(
            input_tokens=(len(instructions) + len(posting_text)) // 4, output_tokens=len(raw_text) // 4
        )
        try:
            parsed = schema.model_validate_json(raw_text)
        except ValidationError as exc:
            raise InvalidOutputError(
                str(exc), model=self.model, usage=usage, raw_text=raw_text, latency_ms=0
            ) from exc
        return LlmResponse(parsed=parsed, raw_text=raw_text, usage=usage, model=self.model, latency_ms=0)
