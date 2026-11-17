import json

import pytest
from langchain_core.messages import AIMessage

import config
from attachments import reader
from attachments.reader import ReaderError, format_attachment_message, read_document
from conversation.log import ConversationLog


class FakeLLM:
    def __init__(self, text="Hotel Sol, check-in 12/03", fail=None):
        self.text, self.fail, self.messages = text, fail, None

    def invoke(self, messages):
        self.messages = messages
        if self.fail:
            raise self.fail
        return AIMessage(content=self.text)


# ---- history with attachments -------------------------------------------------------------


def test_log_message_keeps_attachment_ids_and_old_files_still_load(tmp_path):
    path = tmp_path / "c.json"
    log = ConversationLog(path)
    log.append("user", "[File sent: v.pdf (attachment:ab12cd34)]\nHotel", attachments=["ab12cd34"])
    log.append("user", "plain text")
    assert ConversationLog(path).messages()[0].attachments == ["ab12cd34"]
    assert ConversationLog(path).messages()[1].attachments == []

    old = tmp_path / "old.json"  # earlier format, without the `attachments` field
    old.write_text(json.dumps({
        "messages": [{"id": 1, "role": "user", "text": "hi", "at": "2026-11-17T10:00:00-03:00"}],
        "next_id": 2,
    }), encoding="utf-8")
    assert ConversationLog(old).messages()[0].attachments == []


# ---- reader ----------------------------------------------------------------------------------


def test_pdf_is_sent_as_file_part_and_image_as_image_url():
    llm = FakeLLM()
    read_document(b"%PDF-1.4 x", "v.pdf", "application/pdf", llm=llm)
    parts = llm.messages[0].content
    assert parts[0]["type"] == "text" and "ALL the content" in parts[0]["text"]
    assert parts[1]["type"] == "file" and parts[1]["file"]["filename"] == "v.pdf"
    assert parts[1]["file"]["file_data"].startswith("data:application/pdf;base64,")

    llm = FakeLLM()
    read_document(b"\x89PNG", "photo.png", "image/png", llm=llm)
    assert llm.messages[0].content[1]["type"] == "image_url"
    assert llm.messages[0].content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_read_returns_stripped_text_and_truncates_long_documents():
    assert read_document(b"x", "a.pdf", "application/pdf", llm=FakeLLM("  text  \n")) == "text"
    long = read_document(b"x", "a.pdf", "application/pdf", llm=FakeLLM("a" * (reader.MAX_TEXT_CHARS + 500)))
    assert len(long) < reader.MAX_TEXT_CHARS + 100 and long.endswith("the document is larger than the limit]")


@pytest.mark.parametrize(
    "data,mime,expected",
    [(b"x", "text/plain", "I only read PDFs"), (b"", "application/pdf", "empty"),
     (b"x" * (reader.MAX_FILE_BYTES + 1), "application/pdf", "20 MB")],
)
def test_read_rejects_unsupported_empty_and_huge_files(data, mime, expected):
    llm = FakeLLM()
    with pytest.raises(ReaderError, match=expected):
        read_document(data, "a", mime, llm=llm)
    assert llm.messages is None  # the model was not even called


def test_api_failure_becomes_friendly_error_with_vision_hint_only_for_images():
    boom = RuntimeError("400: No endpoints support image input")
    with pytest.raises(ReaderError, match="DOCUMENT_MODEL") as img:
        read_document(b"x", "f.png", "image/png", llm=FakeLLM(fail=boom))
    assert "No endpoints" not in str(img.value)  # API details do not leak to the user
    with pytest.raises(ReaderError) as pdf:
        read_document(b"x", "v.pdf", "application/pdf", llm=FakeLLM(fail=boom))
    assert "DOCUMENT_MODEL" not in str(pdf.value)


def test_empty_model_answer_is_an_error():
    with pytest.raises(ReaderError, match="returned no text"):
        read_document(b"x", "a.pdf", "application/pdf", llm=FakeLLM("   "))


def test_missing_api_key_is_reported(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    with pytest.raises(ReaderError, match="OPENROUTER_API_KEY"):
        read_document(b"x", "a.pdf", "application/pdf")


def test_default_llm_uses_document_model_and_pdf_engine(monkeypatch):
    seen = {}
    monkeypatch.setattr(reader, "get_llm", lambda **kw: seen.update(kw) or FakeLLM())
    monkeypatch.setattr(config, "DOCUMENT_MODEL", "google/vision")
    monkeypatch.setattr(config, "PDF_ENGINE", "mistral-ocr")
    reader.read_document(b"x", "a.pdf", "application/pdf")
    assert seen["model"] == "google/vision" and seen["temperature"] == 0
    assert seen["extra_body"] == {"plugins": [{"id": "file-parser", "pdf": {"engine": "mistral-ocr"}}]}
    seen.clear()
    reader.read_document(b"x", "f.png", "image/png")  # an image does not use the PDF engine
    assert "extra_body" not in seen
    monkeypatch.setattr(config, "DOCUMENT_MODEL", "")
    seen.clear()
    reader.read_document(b"x", "a.pdf", "application/pdf")
    assert seen["model"] is None  # None = default OPENROUTER_MODEL


def test_format_attachment_message_with_and_without_caption():
    text = format_attachment_message("attachment:ab12cd34", "voucher.pdf", "trip", "Hotel Sol")
    assert text.splitlines()[0] == "[File sent: voucher.pdf (attachment:ab12cd34)]"
    assert "Caption: trip" in text and text.endswith("Hotel Sol")
    assert "Caption" not in format_attachment_message("attachment:x", "a.pdf", "  ", "t")
