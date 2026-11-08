"""Writing a row to llm_calls: one per call, successful or not.

The caller decides the status: usually the outcome's own (ok, or the LlmError subclass's), but the
enrich pipeline may downgrade a valid answer to "ungrounded" after its grounding check.
"""

import json
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, insert

from jobradar.llm.base import LlmError, LlmResponse
from jobradar.llm.pricing import cost_usd
from jobradar.models import LlmCall, LlmCallStatus, LlmPurpose


def _response_json(raw_text: str | None) -> dict[str, Any] | None:
    """The raw answer for the JSONB column: parsed when it's a JSON object, else kept as text."""
    if raw_text is None:
        return None
    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"text": raw_text}
    return value if isinstance(value, dict) else {"text": raw_text}


def record_call(
    conn: Connection,
    outcome: LlmResponse[BaseModel] | LlmError,
    *,
    run_id: uuid.UUID,
    posting_id: int | None,
    purpose: LlmPurpose,
    provider: str,
    prompt_version: str,
    status: LlmCallStatus | None = None,
) -> None:
    """Insert one llm_calls row for `outcome`. Runs in the caller's transaction (no commit here)."""
    if isinstance(outcome, LlmError):
        default_status, error = outcome.status, str(outcome)
    else:
        default_status, error = LlmCallStatus.OK, None
    usage = outcome.usage
    conn.execute(
        insert(LlmCall).values(
            run_id=run_id,
            posting_id=posting_id,
            purpose=purpose,
            provider=provider,
            model=outcome.model,
            prompt_version=prompt_version,
            status=status or default_status,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            cache_write_tokens=usage.cache_write_tokens if usage else None,
            cache_read_tokens=usage.cache_read_tokens if usage else None,
            cost_usd=cost_usd(outcome.model, usage),
            latency_ms=outcome.latency_ms,
            error=error,
            response=_response_json(outcome.raw_text),
        )
    )
