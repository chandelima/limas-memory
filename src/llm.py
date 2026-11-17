"""Fábrica do modelo de chat (OpenRouter via API compatível com OpenAI)."""

from langchain_openai import ChatOpenAI

import config


def get_llm(model: str | None = None, **kwargs) -> ChatOpenAI:
    """Modelo do OpenRouter. Sem `model`, usa OPENROUTER_MODEL."""
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("Defina OPENROUTER_API_KEY no ambiente ou no .env")
    return ChatOpenAI(
        model=model or config.OPENROUTER_MODEL,
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
        **kwargs,
    )
