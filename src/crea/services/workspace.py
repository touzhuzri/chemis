"""The workspace persistence seam (CREA 4.0 Stage F).

A workspace is a conversation container; this module defines the storage
protocol it needs and an in-memory implementation for tests and offline
runs. It is pure persistence -- no agent, no LLM, no chemistry. The
SQLite implementation lives in `crea.repositories.sqlite` (mirroring the
entity/reaction repository split); the orchestration that *uses* this
seam to run the ChemistryAgent lives in `crea.app.workspace`.

Five operations and no more (§21): create a workspace, list them, read
one, append a message, list a workspace's messages in order.
"""

from __future__ import annotations

from typing import Protocol

from crea.core.models.workspace import Workspace, WorkspaceMessage


class WorkspaceRepository(Protocol):
    """What the workspace use case needs from storage."""

    def create_workspace(self, workspace: Workspace) -> Workspace: ...
    def get_workspace(self, workspace_id: str) -> Workspace | None: ...
    def list_workspaces(self) -> list[Workspace]: ...
    def append_message(self, message: WorkspaceMessage) -> WorkspaceMessage: ...
    def list_messages(self, workspace_id: str) -> list[WorkspaceMessage]: ...


class InMemoryWorkspaceRepository:
    """Everything in dicts. For tests and offline runs; gone with the process."""

    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}
        self._messages: dict[str, list[WorkspaceMessage]] = {}

    def create_workspace(self, workspace: Workspace) -> Workspace:
        self._workspaces[workspace.workspace_id] = workspace
        return workspace

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        return self._workspaces.get(workspace_id)

    def list_workspaces(self) -> list[Workspace]:
        return sorted(self._workspaces.values(),
                      key=lambda w: (w.created_at, w.workspace_id))

    def append_message(self, message: WorkspaceMessage) -> WorkspaceMessage:
        self._messages.setdefault(message.workspace_id, []).append(message)
        return message

    def list_messages(self, workspace_id: str) -> list[WorkspaceMessage]:
        return list(self._messages.get(workspace_id, []))


__all__ = ["InMemoryWorkspaceRepository", "WorkspaceRepository"]
