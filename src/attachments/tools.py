"""Tool do agente de resposta para reenviar um arquivo que o usuário mandou antes.

A tool não envia nada sozinha: ela registra o pedido numa `outbox`, e quem fala com o
usuário (bot) envia os arquivos depois da resposta. Só IDs do registro são aceitos.
"""

from langchain_core.tools import BaseTool, tool

from attachments.registry import Attachment, AttachmentRegistry


def build_attachment_tools(registry: AttachmentRegistry, outbox: list[Attachment]) -> list[BaseTool]:
    @tool
    def send_attachment(attachment_id: str) -> str:
        """Envia ao usuário, junto com a sua resposta, um arquivo que ele enviou antes
        (voucher, comprovante, foto). Use o ID que os documentos da memória citam como
        `anexo:ID` (aceita com ou sem o prefixo)."""
        item = registry.get(attachment_id)
        if item is None:
            return f"ERRO: não existe anexo com o ID '{attachment_id}'."
        if any(a.id == item.id for a in outbox):
            return f"OK: '{item.filename}' já será enviado."
        outbox.append(item)
        return f"OK: o arquivo '{item.filename}' será enviado junto com a sua resposta."

    return [send_attachment]
