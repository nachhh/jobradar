# ADR-0003: LLM provider and structured outputs

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

Phase 2 extracts structured fields (seniority, skills, "hires from LATAM?", salary…) from about 2,700 postings with an LLM, then only from new or edited ones. The answers feed filters and search, so they must have a fixed shape and fixed allowed values, and the cost must stay small (project budget: under US$20 in total). Several providers fit, with different prices and limits, and their offers change often.

## Decision

- **A provider interface** (`llm/base.py`: `extract(instructions, posting_text, schema)`), with one adapter per provider: **Claude** (default), **Gemini** (alternative) and a **fake** for tests. `LLM_PROVIDER` picks one; nothing outside `llm/` imports a provider SDK.
- **Claude Haiku 5.5** by default: $0.10 / $0.50 per million input / output tokens, paid from the owner's existing credits. Effort `low`: extraction is reading, not reasoning; at that level the model thinks only when it needs to, which live tests showed helps on judgement calls (e.g. "Technical Lead Senior" → `lead`) for about 120 extra output tokens on average.
- **Prompt caching:** about two thirds of each request (the instructions plus the schema, ~3,300 tokens) is identical for every posting. It is marked for caching, so it is written once (1.25x the input price) and read by later calls at 0.1x. Measured: ~$0.00025–0.0004 per posting, about **$1 for a full pass**, versus ~$1.7 without caching. Cache tokens are recorded apart in `llm_calls`, since they are billed apart.
- **Structured outputs:** each call sends a JSON schema generated from one Pydantic model (`EnrichmentOut`), so the provider constrains the answer to those fields and values. The same model validates the answer, since the schema can't express every rule (`salary_min <= salary_max`) and refusals or truncated answers don't match it.
- **Gemini gets the schema as standard JSON Schema** (`response_json_schema`): its older OpenAPI-style `response_schema` rejects the `additionalProperties: false` that strict schemas need (found in the first live call).
- **Our own validation instead of the SDK's `parse()` helper:** the adapter calls the plain API and validates the text itself, so an answer that fails validation still reaches `llm_calls` as raw text, with its tokens and cost.
- **Allowed values defined once:** the schema reuses the enums of the database models, so the values the LLM may answer with and the values the `CHECK` constraints accept can't drift apart.
- **Grounding check** for the most important field: the model quotes the sentence behind `latam_hiring`, and a "yes" or "no" whose quote isn't in the posting becomes "unknown".
- **Every call is logged** in `llm_calls` (tokens, cost from a price table in code, latency, outcome, raw answer); run totals are sums over it.

## Consequences

- Switching provider or model is a setting plus, for a new provider, one adapter. Tests run without network or API keys.
- Small recurring cost (about $2–4 a month if ingestion runs daily), bounded by `ENRICH_MAX_CALLS` and visible in `llm_calls`.
- Changing the prompt or the schema means bumping `PROMPT_VERSION`, which re-enriches everything (about $1): prompt changes are deliberate.
- The price table must be updated by hand when prices change; costs already recorded keep the price of their day.

## Alternatives considered

- **Gemini or Groq free tiers only:** no cost, but daily request caps turn the first backfill into several days of resumed runs, and the caps change without notice. Kept as the alternative adapter.
- **A local model (Ollama):** free and private, but far too slow on this Intel Mac without a GPU for thousands of postings. Rejected for the main flow.
- **A larger model (Sonnet, Opus):** better at subtle judgement, 20–40 times the price; not needed for extraction. Revisit only if the owner's spot check finds systematic errors.
- **A framework (LangChain, LiteLLM) for the provider abstraction:** more features than this needs and one more dependency to learn; the interface here is a single method. LangGraph arrives in Phase 3, for the agent.
- **Free-form answers parsed with regular expressions:** brittle; structured outputs make the shape a guarantee instead of a hope.
