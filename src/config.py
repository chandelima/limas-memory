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
# Identidade dos commits e token (PAT) para push via HTTPS. O token não é gravado
# no .git/config nem no remote; é enviado só em memória, a cada push.
MEMORY_GIT_USER_NAME = os.getenv("MEMORY_GIT_USER_NAME", "")
MEMORY_GIT_USER_EMAIL = os.getenv("MEMORY_GIT_USER_EMAIL", "")
MEMORY_GIT_TOKEN = os.getenv("MEMORY_GIT_TOKEN", "")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Histórico da conversa (JSON, fora de memory/ e não versionado) e gatilho do Curator
CONVERSATION_FILE = Path(os.getenv("CONVERSATION_FILE", ROOT / "data" / "conversa.json")).resolve()
CURATOR_IDLE_SECONDS = float(os.getenv("CURATOR_IDLE_SECONDS", "600"))

# Bot do Telegram: token do BotFather e o ID numérico do único usuário autorizado
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_allowed = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
TELEGRAM_ALLOWED_USER_ID = int(_allowed) if _allowed.isdigit() else None

# Anexos (voucher, fotos): tabela local, modelo que lê documentos e motor de PDF do OpenRouter
ATTACHMENTS_FILE = Path(os.getenv("ATTACHMENTS_FILE", ROOT / "data" / "anexos.json")).resolve()
DOCUMENT_MODEL = os.getenv("DOCUMENT_MODEL", "")  # vazio = usa OPENROUTER_MODEL
PDF_ENGINE = os.getenv("PDF_ENGINE", "")  # vazio = o OpenRouter escolhe; ou cloudflare-ai, mistral-ocr, native
