"""AI provider factory."""
from .base import AIProvider, AIResponse, ToolCall
import config


def create_provider() -> AIProvider:
    """Instantiate and return the configured AI provider."""
    provider = config.AI_PROVIDER
    if provider == "groq":
        from .groq_provider import GroqProvider
        return GroqProvider()
    elif provider == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider()
    elif provider == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    else:
        raise ValueError(
            f"Unknown AI_PROVIDER: {provider!r}. "
            "Set AI_PROVIDER to one of: groq, openai, anthropic"
        )


__all__ = ["AIProvider", "AIResponse", "ToolCall", "create_provider"]
