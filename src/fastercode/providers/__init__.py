from .agent_provider import AgentProvider
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider
from .openai_compatible import (
    DeepSeekProvider,
    GeminiProvider,
    KimiProvider,
    MistralProvider,
    OpenRouterProvider,
    XAIProvider,
)
from .openai_provider import OpenAIProvider

PROVIDERS = {
    "openai": OpenAIProvider(),
    "anthropic": AnthropicProvider(),
    "kimi": KimiProvider(),
    "xai": XAIProvider(),
    "deepseek": DeepSeekProvider(),
    "mistral": MistralProvider(),
    "gemini": GeminiProvider(),
    "openrouter": OpenRouterProvider(),
}

# AgentProvider is intentionally absent from PROVIDERS above: it produces no
# code, so `api.provider: agent` in a job.yaml must stay an error. agent.py
# activates it directly via llm.set_active_provider().


def get_provider(name):
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ValueError(f"unknown LLM provider '{name}'")
    return provider
