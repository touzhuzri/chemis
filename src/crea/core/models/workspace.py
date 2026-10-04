"""The Chemistry Workspace domain (CREA 4.0 Stage F).

A workspace is a container for one conversation with the ChemistryAgent:
a sequence of user messages and assistant answers. Each assistant message
carries the `AgentResult`'s `tool_calls` / `evidence` / `provenance` /
`diagnostics`, so the Web layer can show *why* an answer says what it says
(§16) -- the Stage E result is not flattened to a bare string.

This is a conversation record, explicitly not a memory (§29): no user
identity, no preferences, no cross-workspace state. `WorkspaceMessage`
reuses the ONE `ToolResult` contract for `tool_calls`; there is no second
message or result vocabulary.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from crea.core.models.research import ToolResult
from crea.core.provenance import utc_now_iso


class MessageRole(str, Enum):
    """Who said it. Two roles, and no more (Stage F §3)."""

    USER = "user"
    ASSISTANT = "assistant"


class Workspace(BaseModel):
    """One named conversation container (§6)."""

    workspace_id: str
    title: str = "新建工作区"
    created_at: str = Field(default_factory=utc_now_iso)


class WorkspaceMessage(BaseModel):
    """One message in a workspace conversation (§7).

    `role` is `user` or `assistant`. A user message carries `content`; an
    assistant message also carries the `AgentResult` fields -- `tool_calls`
    (the ONE `ToolResult` contract), `evidence`, `provenance`,
    `diagnostics` -- so nothing the agent produced is lost between the tool
    layer and the Web layer.
    """

    message_id: str
    workspace_id: str
    role: MessageRole
    content: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    tool_calls: list[ToolResult] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    provenance: list[dict] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


__all__ = ["MessageRole", "Workspace", "WorkspaceMessage"]
