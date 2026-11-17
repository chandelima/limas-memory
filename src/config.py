"""Configuração central: lê variáveis de ambiente (e o .env da raiz do projeto)."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Slug do modelo no OpenRouter (sobrescreva com OPENROUTER_MODEL se necessário)
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-6-luna")

# Pasta da memória permanente (somente Markdown) e remote git dela
MEMORY_DIR = Path(os.getenv("MEMORY_DIR", ROOT / "memory")).resolve()
MEMORY_GIT_REMOTE = os.getenv("MEMORY_GIT_REMOTE", "")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
