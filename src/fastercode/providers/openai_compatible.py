"""Backends that speak the OpenAI Chat Completions protocol.

Each of these is a configuration of OpenAIProvider rather than a new
integration: point the openai client at a different base URL, read a
different API key env var, and pick a default model. No extra dependencies -
`openai` is already required.

Adding another OpenAI-protocol backend means adding a class here, not a new
file. The four things to get right:

  base_url        the vendor's OpenAI-compatible endpoint
  api_key_env     the env var their own SDK/docs use, so a key already
                  exported for their CLI just works
  model           see the note on defaults below
  json_mode       "json_object" if the backend supports
                  response_format, None if it doesn't or if support depends
                  on which model gets routed to

json_mode=None is safe because the prompt templates in
adapters/python_adapter.py already end with `Return ONLY JSON matching
{"lines": [list of strings]}` - response_format is belt-and-braces. Those
backends lean on _parse_lines() in openai_provider.py, which tolerates the
markdown fences models tend to add when they aren't held to a strict JSON
mode.

use_responses_api is False everywhere here: responses.create() is
OpenAI-proprietary, so attempting it against these would waste a round-trip
on every call before falling back to Chat Completions.

On model defaults: prefer a vendor's self-updating alias where one exists
(mistral-large-latest, openrouter/auto-beta) since those never go stale.
Pinned IDs do go stale - DeepSeek retired deepseek-chat and
deepseek-reasoner on 2026-07-24 - so a 404 from one of these usually means
the ID moved, not that the code is broken. Any of them can be overridden
per-run via llm.set_credentials(..., model=...) or job YAML's api.model.
"""

from .openai_provider import OpenAIProvider


class KimiProvider(OpenAIProvider):
    """Moonshot AI."""

    name = "kimi"

    def __init__(self, model="kimi-k3"):
        super().__init__(
            model=model,
            base_url="https://api.moonshot.ai/v1",
            api_key_env="MOONSHOT_API_KEY",
            use_responses_api=False,
        )


class XAIProvider(OpenAIProvider):
    """xAI (Grok). grok-build is their coding-agent model if the default
    underperforms on refactoring."""

    name = "xai"

    def __init__(self, model="grok-4.5"):
        super().__init__(
            model=model,
            base_url="https://api.x.ai/v1",
            api_key_env="XAI_API_KEY",
            use_responses_api=False,
        )


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek. deepseek-chat/deepseek-reasoner were retired 2026-07-24 -
    use the v4 line."""

    name = "deepseek"

    def __init__(self, model="deepseek-v4-pro"):
        super().__init__(
            model=model,
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
            use_responses_api=False,
        )


class MistralProvider(OpenAIProvider):
    """Mistral. The -latest alias is self-updating, so this default doesn't
    need maintaining."""

    name = "mistral"

    def __init__(self, model="mistral-large-latest"):
        super().__init__(
            model=model,
            base_url="https://api.mistral.ai/v1",
            api_key_env="MISTRAL_API_KEY",
            use_responses_api=False,
        )


class GeminiProvider(OpenAIProvider):
    """Google Gemini, via its OpenAI-compatibility shim rather than the
    native google-genai SDK - keeps it dependency-free and on the same code
    path as everything else here.

    json_mode is None: Google's compat docs steer to the SDK's .parse()
    helper and don't document response_format, so requesting JSON mode is not
    safe to assume. The prompt asks for the JSON shape regardless.
    """

    name = "gemini"

    def __init__(self, model="gemini-3.5-flash"):
        super().__init__(
            model=model,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
            use_responses_api=False,
            json_mode=None,
        )


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter - a router in front of many vendors' models.

    Two things follow from that. json_mode is None because response_format
    support depends on whichever model gets routed to. And the default
    `openrouter/auto-beta` lets OpenRouter choose per request, which means
    report.pdf will record the literal string "openrouter/auto-beta" rather
    than the model that actually served - pass an explicit `vendor/model`
    slug (e.g. "anthropic/claude-opus-5") if the report needs to name a real
    model.
    """

    name = "openrouter"

    def __init__(self, model="openrouter/auto-beta"):
        super().__init__(
            model=model,
            base_url="https://openrouter.ai/api/v1",
            api_key_env="OPENROUTER_API_KEY",
            use_responses_api=False,
            json_mode=None,
        )
