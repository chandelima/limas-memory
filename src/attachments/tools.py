"""Chat agent tool to send back a file the user sent earlier.

The tool does not send anything by itself: it records the request in an `outbox`, and whoever
talks to the user (the bot) sends the files after the reply. Only registry IDs are accepted.
"""

from langchain_core.tools import BaseTool, tool

from attachments.registry import Attachment, AttachmentRegistry


def build_attachment_tools(registry: AttachmentRegistry, outbox: list[Attachment]) -> list[BaseTool]:
    @tool
    def send_attachment(attachment_id: str) -> str:
        """Send the user, along with your reply, a file they sent earlier (voucher, receipt,
        photo). Use the ID that the memory documents cite as `attachment:ID` (accepted with
        or without the prefix)."""
        item = registry.get(attachment_id)
        if item is None:
            return f"ERROR: there is no attachment with ID '{attachment_id}'."
        if any(a.id == item.id for a in outbox):
            return f"OK: '{item.filename}' will already be sent."
        outbox.append(item)
        return f"OK: the file '{item.filename}' will be sent along with your reply."

    return [send_attachment]
