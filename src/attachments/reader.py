"""Leitor de documentos: transforma PDF e imagem (voucher, foto de papel) em texto.

Usa o modelo do OpenRouter. PDF vai como bloco `file`: o OpenRouter extrai o texto antes
de chamar o modelo (plugin `file-parser`), então funciona com qualquer modelo. Imagem vai
como `image_url` e exige um modelo com visão (DOCUMENT_MODEL, ou o OPENROUTER_MODEL).
"""

import base64

from langchain_core.messages import HumanMessage

import config
from llm import get_llm

MAX_FILE_BYTES = 20 * 1024 * 1024  # limite de download de arquivos pelo bot do Telegram
MAX_TEXT_CHARS = 30_000  # teto do texto extraído que entra no histórico e na memória
PDF_MIME = "application/pdf"
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
SUPPORTED_MIMES = {PDF_MIME} | IMAGE_MIMES

PROMPT = (
    "Transcreva fielmente TODO o conteúdo deste documento, em português quando houver "
    "tradução óbvia dos rótulos, mantendo os valores originais: nomes, datas, horários, "
    "locais, endereços, números, códigos, valores e observações. Preserve a estrutura "
    "(campos e tabelas) em texto simples, sem Markdown. Não resuma, não interprete e não "
    "invente nada. Marque trechos ilegíveis como [ilegível]. Responda só com a transcrição."
)


class ReaderError(Exception):
    """Falha ao ler o arquivo; a mensagem é segura para mostrar ao usuário."""


_EXTENSIONS = {
    ".pdf": PDF_MIME, ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}


def is_supported(mime: str) -> bool:
    return mime.lower() in SUPPORTED_MIMES


def guess_mime(filename: str, mime: str = "") -> str:
    """Tipo do arquivo: o informado, se for suportado; senão pela extensão (alguns
    clientes mandam PDF como application/octet-stream)."""
    if mime and is_supported(mime):
        return mime.lower()
    for ext, known in _EXTENSIONS.items():
        if filename.lower().endswith(ext):
            return known
    return mime or ""


def build_message(data: bytes, filename: str, mime: str) -> HumanMessage:
    mime = mime.lower()
    b64 = base64.b64encode(data).decode("ascii")
    if mime == PDF_MIME:
        part = {"type": "file", "file": {"filename": filename, "file_data": f"data:{mime};base64,{b64}"}}
    else:
        part = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
    return HumanMessage(content=[{"type": "text", "text": PROMPT}, part])


def _default_llm(mime: str):
    extra = {}
    if mime.lower() == PDF_MIME and config.PDF_ENGINE:
        extra["extra_body"] = {"plugins": [{"id": "file-parser", "pdf": {"engine": config.PDF_ENGINE}}]}
    return get_llm(model=config.DOCUMENT_MODEL or None, temperature=0, **extra)


def read_document(data: bytes, filename: str, mime: str, llm=None) -> str:
    """Devolve a transcrição do documento (truncada em MAX_TEXT_CHARS)."""
    if not is_supported(mime):
        raise ReaderError("Só leio PDF e imagens (JPG, PNG, WebP ou GIF).")
    if not data:
        raise ReaderError("O arquivo está vazio.")
    if len(data) > MAX_FILE_BYTES:
        raise ReaderError("O arquivo passa do limite de 20 MB.")
    try:
        llm = llm or _default_llm(mime)
    except RuntimeError as exc:  # ex.: OPENROUTER_API_KEY ausente (mensagem própria, segura)
        raise ReaderError(str(exc)) from exc
    try:
        reply = llm.invoke([build_message(data, filename, mime)])
    except Exception as exc:  # noqa: BLE001 - erro da API vira mensagem para o usuário
        hint = ""
        if mime.lower() != PDF_MIME:
            hint = " O modelo pode não aceitar imagem: defina DOCUMENT_MODEL com um modelo de visão."
        raise ReaderError(f"Não consegui ler o arquivo ({type(exc).__name__}).{hint}") from exc
    text = reply.text.strip()
    if not text:
        raise ReaderError("O modelo não devolveu texto para esse arquivo.")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rstrip() + "\n[texto truncado: o documento é maior que o limite]"
    return text


def _header(ref: str, filename: str, caption: str) -> list[str]:
    lines = [f"[Arquivo enviado: {filename} ({ref})]"]
    if caption.strip():
        lines.append(f"Legenda: {caption.strip()}")
    return lines


def format_attachment_message(ref: str, filename: str, caption: str, text: str) -> str:
    """Texto que entra no histórico (e, depois, vai para o Curator) para um arquivo recebido."""
    return "\n".join([*_header(ref, filename, caption), "Conteúdo extraído do arquivo:", text])


def format_placeholder(ref: str, filename: str, caption: str) -> str:
    """Texto provisório, gravado assim que o arquivo chega e enquanto ele é lido."""
    return "\n".join([*_header(ref, filename, caption), "(lendo o arquivo...)"])


def format_unreadable(ref: str, filename: str, caption: str) -> str:
    """Texto final quando não foi possível ler o arquivo."""
    return "\n".join([*_header(ref, filename, caption), "(não foi possível ler o arquivo)"])
