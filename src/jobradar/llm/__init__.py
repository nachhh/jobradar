"""LLM access: one interface (base.LlmProvider), one adapter per provider.

- base.py: the interface, the response type and the error types.
- claude.py, gemini.py: real providers; fake.py: the one tests use.
- pricing.py: cost of a call. log.py: the llm_calls row. ratelimit.py: requests per minute.

Callers get a provider from make_provider() and never import an SDK themselves.
"""

from jobradar.config import Settings
from jobradar.llm.base import LlmProvider


class MissingApiKeyError(RuntimeError):
    """The chosen provider has no API key configured."""


def make_provider(settings: Settings) -> LlmProvider:
    """The provider named by LLM_PROVIDER, with its key from the environment.

    Imports happen inside the branches, so using one provider doesn't load the other's SDK.
    """
    if settings.llm_provider == "claude":
        from jobradar.llm.claude import ClaudeProvider

        if settings.anthropic_api_key is None:
            raise MissingApiKeyError("LLM_PROVIDER=claude needs ANTHROPIC_API_KEY (see .env.example)")
        return ClaudeProvider(settings.anthropic_api_key.get_secret_value())

    from jobradar.llm.gemini import GeminiProvider

    if settings.gemini_api_key is None:
        raise MissingApiKeyError("LLM_PROVIDER=gemini needs GEMINI_API_KEY (see .env.example)")
    return GeminiProvider(settings.gemini_api_key.get_secret_value())
