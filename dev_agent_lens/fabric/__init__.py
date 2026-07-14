"""Fabric convenience layer over DAL span data (ENG2-1313).

One-call "readable conversation per session, filtered by signal":

    from dev_agent_lens.fabric import (
        reconstruct_session,   # session_id → markdown export, in time order
        list_sessions,         # filters → session dicts (newest first)
        export_conversations,  # bulk write one .md per session, atomically
    )
"""

from dev_agent_lens.fabric.queries import (
    SessionContext,
    export_conversations,
    get_meeting_sessions,
    get_session_context,
    get_ticket_sessions,
    list_sessions,
    reconstruct_session,
)

__all__ = [
    "SessionContext",
    "export_conversations",
    "get_meeting_sessions",
    "get_session_context",
    "get_ticket_sessions",
    "list_sessions",
    "reconstruct_session",
]
