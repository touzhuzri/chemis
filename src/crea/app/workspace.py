"""Workspace use cases: a conversation with the ChemistryAgent (Stage F).

The workbench's chat side. This module orchestrates -- it opens the
knowledge base, persists messages, builds the conversation context, and
runs the ChemistryAgent -- but it computes no chemistry and speaks no SQL
of its own. The agent is injected as a factory (§5/§30): this layer never
reaches RDKit, the resolver or a provider, and the agent never reaches the
workspace repository.

The one genuinely new capability (§8): a message is answered in the
context of its workspace's recent history, so "它有哪些已知反应？" can
resolve "它" against the previous turn. Context is just the last
`MAX_CONTEXT_MESSAGES` messages converted to neutral LLM messages -- no
summarization, no long-term memory, no cross-workspace state (§9/§10/§29).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from crea.core.ids import content_hash
from crea.core.models.workspace import MessageRole, Workspace, WorkspaceMessage
from crea.core.provenance import utc_now_iso
from crea.services.workspace import WorkspaceRepository

#: How much history a turn carries (§10). A hard cap, not a summarizer:
#: older messages simply drop out of context.
MAX_CONTEXT_MESSAGES = 30


class WorkspaceApplication:
    """Create, list and read workspaces; send messages through the agent.

    `agent_factory(kb)` is called once per message with the knowledge base
    that already opened the workspace repository, so the agent's chemistry
    registries and the workspace's persistence share one store for the
    lifetime of the turn. A failing factory or model is an honest assistant
    message with diagnostics -- never a fabricated answer (§17/§26).
    """

    def __init__(self, db_path, agent_factory: Callable, *,
                 max_context_messages: int = MAX_CONTEXT_MESSAGES) -> None:
        self._db_path = Path(db_path)
        self._agent_factory = agent_factory
        self._max_context_messages = max_context_messages

    # -- plumbing -----------------------------------------------------------

    def _kb(self):
        from crea.services.knowledge_base import KnowledgeBase

        kb = KnowledgeBase(self._db_path)
        kb.initialize()
        return kb

    @staticmethod
    def _require_workspace(repo: WorkspaceRepository, workspace_id: str) -> Workspace:
        workspace = repo.get_workspace(workspace_id)
        if workspace is None:
            raise KeyError(f"no workspace {workspace_id!r}")
        return workspace

    # -- workspaces (§6/§13) ------------------------------------------------

    def create_workspace(self, title: str = "") -> Workspace:
        workspace = Workspace(
            workspace_id="ws-" + content_hash(
                f"{utc_now_iso()}|{time.time_ns()}|{title}", 16),
            title=(title.strip() or "新建工作区"))
        kb = self._kb()
        try:
            return kb.workspace_repository().create_workspace(workspace)
        finally:
            kb.close()

    def list_workspaces(self) -> list[Workspace]:
        kb = self._kb()
        try:
            return kb.workspace_repository().list_workspaces()
        finally:
            kb.close()

    def get_workspace(self, workspace_id: str) -> Workspace:
        kb = self._kb()
        try:
            repo = kb.workspace_repository()
            return self._require_workspace(repo, workspace_id)
        finally:
            kb.close()

    def list_messages(self, workspace_id: str) -> list[WorkspaceMessage]:
        kb = self._kb()
        try:
            repo = kb.workspace_repository()
            self._require_workspace(repo, workspace_id)
            return repo.list_messages(workspace_id)
        finally:
            kb.close()

    # -- sending a message (§7/§8/§9) ---------------------------------------

    def send_message(self, workspace_id: str,
                     content: str) -> WorkspaceMessage:
        """Append the user's message, run the agent in context, append the answer."""
        kb = self._kb()
        try:
            repo = kb.workspace_repository()
            self._require_workspace(repo, workspace_id)

            repo.append_message(self._user_message(workspace_id, content))
            messages = repo.list_messages(workspace_id)
            history = self._history(messages[:-1])
            current = messages[-1].content

            try:
                agent = self._agent_factory(kb)
                result = agent.run(current, history=history)
            except Exception as exc:  # noqa: BLE001 - a dead agent is an honest answer
                answer = self._assistant_message(
                    workspace_id, "（未能生成回答：服务不可用）",
                    diagnostics=[f"agent error: {type(exc).__name__}: {exc}"])
            else:
                answer = self._assistant_message(
                    workspace_id,
                    result.answer or "（未生成回答，见诊断信息）",
                    tool_calls=result.tool_calls, evidence=result.evidence,
                    provenance=result.provenance, diagnostics=result.diagnostics)
            repo.append_message(answer)
            return answer
        finally:
            kb.close()

    # -- helpers ------------------------------------------------------------

    def _history(self, messages: list[WorkspaceMessage]) -> list[dict]:
        """Recent workspace messages as neutral LLM messages (§9).

        Only `role` + `content`: the conversational surface. Tool results
        and evidence are re-derived by the agent on its own run, never
        replayed from storage.
        """
        recent = messages[-self._max_context_messages:]
        return [{"role": m.role.value, "content": m.content} for m in recent]

    @staticmethod
    def _new_message_id(workspace_id: str, role: MessageRole,
                        content: str) -> str:
        return "msg-" + content_hash(
            f"{workspace_id}|{role.value}|{content}|{utc_now_iso()}|"
            f"{time.time_ns()}", 16)

    def _user_message(self, workspace_id: str,
                      content: str) -> WorkspaceMessage:
        return WorkspaceMessage(
            message_id=self._new_message_id(workspace_id, MessageRole.USER,
                                            content),
            workspace_id=workspace_id, role=MessageRole.USER, content=content)

    def _assistant_message(self, workspace_id: str, content: str, *,
                           tool_calls=(), evidence=(), provenance=(),
                           diagnostics=()) -> WorkspaceMessage:
        return WorkspaceMessage(
            message_id=self._new_message_id(workspace_id, MessageRole.ASSISTANT,
                                            content),
            workspace_id=workspace_id, role=MessageRole.ASSISTANT,
            content=content, tool_calls=list(tool_calls),
            evidence=list(evidence), provenance=list(provenance),
            diagnostics=list(diagnostics))


__all__ = ["MAX_CONTEXT_MESSAGES", "WorkspaceApplication"]
