"""Chat model factory (OpenRouter through the OpenAI-compatible API)."""

from langchain_openai import ChatOpenAI

import config


def get_llm(model: str | None = None, **kwargs) -> ChatOpenAI:
    """OpenRouter model. Without `model`, uses OPENROUTER_MODEL."""
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("Set OPENROUTER_API_KEY in the environment or in .env")
    return ChatOpenAI(
        model=model or config.OPENROUTER_MODEL,
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
        **kwargs,
    )
