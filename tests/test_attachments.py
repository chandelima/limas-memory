import json

import pytest
from langchain_core.messages import AIMessage

import config
from attachments import reader
from attachments.reader import ReaderError, format_attachment_message, read_document
from attachments.registry import AttachmentRegistry, normalize_id
from conversation.log import ConversationLog


def make(registry, **kw):
    base = dict(file_id="AgACAg-xyz", file_unique_id="u1", kind="document", chat_id=42,
                message_id=7, filename="voucher.pdf", mime="application/pdf", size=1234,
                caption="viagem a Lisboa")
    base.update(kw)
    return registry.add(**base)


class FakeLLM:
    def __init__(self, text="Hotel Sol, check-in 12/03", fail=None):
        self.text, self.fail, self.messages = text, fail, None

    def invoke(self, messages):
        self.messages = messages
        if self.fail:
            raise self.fail
        return AIMessage(content=self.text)


# ---- registro -----------------------------------------------------------------------


def test_registry_add_get_and_persist(tmp_path):
    path = tmp_path / "data" / "anexos.json"
    item = make(AttachmentRegistry(path))
    assert len(item.id) == 8 and item.ref == f"anexo:{item.id}" and item.caption == "viagem a Lisboa"
    again = AttachmentRegistry(path)  # nova instância lê do arquivo
    assert again.get(item.id).file_id == "AgACAg-xyz" and again.all()[0].message_id == 7


def test_registry_get_accepts_prefix_and_case(tmp_path):
    reg = AttachmentRegistry(tmp_path / "a.json")
    item = make(reg)
    assert reg.get(f"anexo:{item.id}") is item or reg.get(f"anexo:{item.id}").id == item.id
    assert reg.get(f" ANEXO:{item.id.upper()} ").id == item.id
    assert reg.get("naoexiste") is None
    assert normalize_id("Anexo: AB12") == "ab12"


def test_registry_ids_are_unique_and_opaque(tmp_path):
    reg = AttachmentRegistry(tmp_path / "a.json")
    ids = {make(reg, file_id=f"f{i}").id for i in range(50)}
    assert len(ids) == 50


def test_registry_corrupted_file_is_set_aside(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{ nao e json", encoding="utf-8")
    reg = AttachmentRegistry(path)
    assert reg.all() == [] and len(list(tmp_path.glob("a.json.corrupt-*"))) == 1


def test_registry_file_id_never_in_memory_dir_default():
    assert "memory" not in config.ATTACHMENTS_FILE.parts[-2:]  # data/anexos.json, fora de memory/


# ---- histórico com anexos ------------------------------------------------------------


def test_log_message_keeps_attachment_ids_and_old_files_still_load(tmp_path):
    path = tmp_path / "c.json"
    log = ConversationLog(path)
    log.append("usuario", "[Arquivo enviado: v.pdf (anexo:ab12cd34)]\nHotel", attachments=["ab12cd34"])
    log.append("usuario", "texto normal")
    assert ConversationLog(path).messages()[0].attachments == ["ab12cd34"]
    assert ConversationLog(path).messages()[1].attachments == []

    old = tmp_path / "old.json"  # formato anterior, sem o campo `attachments`
    old.write_text(json.dumps({
        "messages": [{"id": 1, "role": "usuario", "text": "oi", "at": "2026-11-17T10:00:00-03:00"}],
        "next_id": 2, "curated_upto": 0,
    }), encoding="utf-8")
    assert ConversationLog(old).messages()[0].attachments == []


# ---- leitor --------------------------------------------------------------------------


def test_pdf_is_sent_as_file_part_and_image_as_image_url():
    llm = FakeLLM()
    read_document(b"%PDF-1.4 x", "v.pdf", "application/pdf", llm=llm)
    parts = llm.messages[0].content
    assert parts[0]["type"] == "text" and "TODO o conteúdo" in parts[0]["text"]
    assert parts[1]["type"] == "file" and parts[1]["file"]["filename"] == "v.pdf"
    assert parts[1]["file"]["file_data"].startswith("data:application/pdf;base64,")

    llm = FakeLLM()
    read_document(b"\x89PNG", "foto.png", "image/png", llm=llm)
    assert llm.messages[0].content[1]["type"] == "image_url"
    assert llm.messages[0].content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_read_returns_stripped_text_and_truncates_long_documents():
    assert read_document(b"x", "a.pdf", "application/pdf", llm=FakeLLM("  texto  \n")) == "texto"
    long = read_document(b"x", "a.pdf", "application/pdf", llm=FakeLLM("a" * (reader.MAX_TEXT_CHARS + 500)))
    assert len(long) < reader.MAX_TEXT_CHARS + 100 and long.endswith("o documento é maior que o limite]")


@pytest.mark.parametrize(
    "data,mime,expected",
    [(b"x", "text/plain", "Só leio PDF"), (b"", "application/pdf", "vazio"),
     (b"x" * (reader.MAX_FILE_BYTES + 1), "application/pdf", "20 MB")],
)
def test_read_rejects_unsupported_empty_and_huge_files(data, mime, expected):
    llm = FakeLLM()
    with pytest.raises(ReaderError, match=expected):
        read_document(data, "a", mime, llm=llm)
    assert llm.messages is None  # nem chegou a chamar o modelo


def test_api_failure_becomes_friendly_error_with_vision_hint_only_for_images():
    boom = RuntimeError("400: No endpoints support image input")
    with pytest.raises(ReaderError, match="DOCUMENT_MODEL") as img:
        read_document(b"x", "f.png", "image/png", llm=FakeLLM(fail=boom))
    assert "No endpoints" not in str(img.value)  # detalhes da API não vazam para o usuário
    with pytest.raises(ReaderError) as pdf:
        read_document(b"x", "v.pdf", "application/pdf", llm=FakeLLM(fail=boom))
    assert "DOCUMENT_MODEL" not in str(pdf.value)


def test_empty_model_answer_is_an_error():
    with pytest.raises(ReaderError, match="não devolveu texto"):
        read_document(b"x", "a.pdf", "application/pdf", llm=FakeLLM("   "))


def test_missing_api_key_is_reported(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    with pytest.raises(ReaderError, match="OPENROUTER_API_KEY"):
        read_document(b"x", "a.pdf", "application/pdf")


def test_default_llm_uses_document_model_and_pdf_engine(monkeypatch):
    seen = {}
    monkeypatch.setattr(reader, "get_llm", lambda **kw: seen.update(kw) or FakeLLM())
    monkeypatch.setattr(config, "DOCUMENT_MODEL", "google/visao")
    monkeypatch.setattr(config, "PDF_ENGINE", "mistral-ocr")
    reader.read_document(b"x", "a.pdf", "application/pdf")
    assert seen["model"] == "google/visao" and seen["temperature"] == 0
    assert seen["extra_body"] == {"plugins": [{"id": "file-parser", "pdf": {"engine": "mistral-ocr"}}]}
    seen.clear()
    reader.read_document(b"x", "f.png", "image/png")  # imagem não usa o motor de PDF
    assert "extra_body" not in seen
    monkeypatch.setattr(config, "DOCUMENT_MODEL", "")
    seen.clear()
    reader.read_document(b"x", "a.pdf", "application/pdf")
    assert seen["model"] is None  # None = padrão OPENROUTER_MODEL


def test_format_attachment_message_with_and_without_caption():
    text = format_attachment_message("anexo:ab12cd34", "voucher.pdf", "viagem", "Hotel Sol")
    assert text.splitlines()[0] == "[Arquivo enviado: voucher.pdf (anexo:ab12cd34)]"
    assert "Legenda: viagem" in text and text.endswith("Hotel Sol")
    assert "Legenda" not in format_attachment_message("anexo:x", "a.pdf", "  ", "t")
