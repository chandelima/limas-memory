"""Configuração central: lê variáveis de ambiente (e o .env da raiz do projeto)."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Slug do modelo no OpenRouter (sobrescreva com OPENROUTER_MODEL se necessário)
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-5.5")

# Pasta da memória permanente (somente Markdown) e remote git dela
MEMORY_DIR = Path(os.getenv("MEMORY_DIR", ROOT / "memory")).resolve()
MEMORY_GIT_REMOTE = os.getenv("MEMORY_GIT_REMOTE", "")
# Identidade dos commits e token (PAT) para push via HTTPS. O token não é gravado
# no .git/config nem no remote; é enviado só em memória, a cada push.
MEMORY_GIT_USER_NAME = os.getenv("MEMORY_GIT_USER_NAME", "")
MEMORY_GIT_USER_EMAIL = os.getenv("MEMORY_GIT_USER_EMAIL", "")
MEMORY_GIT_TOKEN = os.getenv("MEMORY_GIT_TOKEN", "")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
