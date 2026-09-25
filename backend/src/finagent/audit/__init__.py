"""本地审计记录。当前只包装 Chat Completions 文本调用。"""

from finagent.audit.audited_chat import (
    AuditPersistError,
    AuditedChatResult,
    EvidenceRef,
    audited_complete_chat,
)

__all__ = [
    "AuditPersistError",
    "AuditedChatResult",
    "EvidenceRef",
    "audited_complete_chat",
]
