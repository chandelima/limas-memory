"""Document reader: turns a PDF or image (voucher, photo of a paper) into text.

Uses the OpenRouter model. A PDF goes as a `file` block: OpenRouter extracts the text before
calling the model (`file-parser` plugin), so it works with any model. An image goes as
`image_url` and needs a vision model (DOCUMENT_MODEL, or OPENROUTER_MODEL).
"""

import base64

from langchain_core.messages import HumanMessage

import config
from llm import get_llm

MAX_FILE_BYTES = 20 * 1024 * 1024  # download limit for files through the Telegram bot
MAX_TEXT_CHARS = 30_000  # cap on the extracted text that goes into the history and the memory
PDF_MIME = "application/pdf"
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
SUPPORTED_MIMES = {PDF_MIME} | IMAGE_MIMES

PROMPT = (
    "Faithfully transcribe ALL the content of this document, in its original language, keeping "
    "the original values: names, dates, times, places, addresses, numbers, codes, amounts and "
    "notes. Preserve the structure (fields and tables) as plain text, without Markdown. Do not "
    "summarize, do not interpret and do not make anything up. Mark unreadable passages as "
    "[unreadable]. Reply with the transcription only."
)


class ReaderError(Exception):
    """Failure to read the file; the message is safe to show to the user."""


_EXTENSIONS = {
    ".pdf": PDF_MIME, ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}


def is_supported(mime: str) -> bool:
    return mime.lower() in SUPPORTED_MIMES


def guess_mime(filename: str, mime: str = "") -> str:
    """File type: the given one, if supported; otherwise from the extension (some
    clients send PDFs as application/octet-stream)."""
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
    """Return the document's transcription (truncated at MAX_TEXT_CHARS)."""
    if not is_supported(mime):
        raise ReaderError("I only read PDFs and images (JPG, PNG, WebP or GIF).")
    if not data:
        raise ReaderError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ReaderError("The file exceeds the 20 MB limit.")
    try:
        llm = llm or _default_llm(mime)
    except RuntimeError as exc:  # e.g. missing OPENROUTER_API_KEY (its own message, safe)
        raise ReaderError(str(exc)) from exc
    try:
        reply = llm.invoke([build_message(data, filename, mime)])
    except Exception as exc:  # noqa: BLE001 - an API error becomes a message for the user
        hint = ""
        if mime.lower() != PDF_MIME:
            hint = " The model may not accept images: set DOCUMENT_MODEL to a vision model."
        raise ReaderError(f"I could not read the file ({type(exc).__name__}).{hint}") from exc
    text = reply.text.strip()
    if not text:
        raise ReaderError("The model returned no text for this file.")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rstrip() + "\n[text truncated: the document is larger than the limit]"
    return text


def _header(ref: str, filename: str, caption: str) -> list[str]:
    lines = [f"[File sent: {filename} ({ref})]"]
    if caption.strip():
        lines.append(f"Caption: {caption.strip()}")
    return lines


def format_attachment_message(ref: str, filename: str, caption: str, text: str) -> str:
    """Text that goes into the history (and then to the Curator) for a received file."""
    return "\n".join([*_header(ref, filename, caption), "Content extracted from the file:", text])


def format_placeholder(ref: str, filename: str, caption: str) -> str:
    """Provisional text, recorded as soon as the file arrives and while it is being read."""
    return "\n".join([*_header(ref, filename, caption), "(reading the file...)"])


def format_unreadable(ref: str, filename: str, caption: str) -> str:
    """Final text when the file could not be read."""
    return "\n".join([*_header(ref, filename, caption), "(could not read the file)"])
