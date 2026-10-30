"""The legacy agent loop (superseded as the research decision centre by M2.0).

**M2.0 status.** This loop's reply shape (`AgentAction`), its stop contract
and its claim resolution are what `crea.agent.intelligence` replaces with
the single `ResearchDecision` protocol. `crea research --ai` and
`POST /api/research/intelligence` drive the new loop. What stays here is
compatibility: the offline demo stack, `build_offline_stack` /
`build_live_stack`, and the tests that pin this behaviour. Nothing new
should be built on this class -- a caller that wants a model-driven
research loop wants `ResearchIntelligence`.

The loop itself:

```
ResearchTask
  ↓
  LLM            -> AgentAction (a tool name and JSON arguments, never code)
  ↓
  ToolRegistry   -> validate, execute, ToolResult   <- the only path to a fact
  ↓
  tool results appended to the thread
  ↓
  LLM            -> ... or FINAL
  ↓
  ResearchReport
```

Two invariants the loop enforces rather than asks for:

* **Bounded.** `MAX_STEPS` and `MAX_TOOL_CALLS` are hard ceilings. Reaching one
  produces `PARTIAL` with whatever evidence exists, not a retry storm.
* **Nothing enters state that a tool did not produce.** The thread handed to
  the model is assembled from real `ToolResult`s. If a model claims a tool
  returned something, the next turn does not contain it -- the registry
  decides, not the narrative.
"""

from __future__ import annotations

import json
import time

from crea.core.errors import CreaError
from crea.core.ids import content_hash
from crea.core.models.research import (
    ActionKind,
    AgentAction,
    ResearchStep,
    ResearchTask,
    SourceRef,
    TaskStatus,
    ToolResult,
    ToolStatus,
)
from crea.core.models.session import ResearchSession, WorkflowStatus
from crea.llm.base import LLMProvider, redact
from crea.services.evidence import EvidenceRegistry

MAX_STEPS = 12
MAX_TOOL_CALLS = 20

#: The furthest a run has got. Progress only ever moves forward: a step that
#: validates after evidence was collected leaves the session in VALIDATING,
#: even if the model then asks for one more search.
_PHASE_RANK = {
    WorkflowStatus.RESEARCHING: 0,
    WorkflowStatus.COLLECTING_EVIDENCE: 1,
    WorkflowStatus.VALIDATING: 2,
    WorkflowStatus.REPORTING: 3,
}

#: Which tool call belongs to which phase. Everything not named here is
#: research -- asking a question, resolving an identity, analysing a
#: structure, proposing a candidate route.
_PHASE_FOR_TOOL = {
    "get_document_evidence": WorkflowStatus.COLLECTING_EVIDENCE,
    "get_reaction_details": WorkflowStatus.COLLECTING_EVIDENCE,
    "search_reaction_evidence": WorkflowStatus.COLLECTING_EVIDENCE,
    "create_claim": WorkflowStatus.COLLECTING_EVIDENCE,
    "validate_route_step": WorkflowStatus.VALIDATING,
    "route_evidence_report": WorkflowStatus.VALIDATING,
}

from crea.agent.observer import (
    Observation,
    ObservationKind,
    observe_from_registry,
)
from crea.agent.prompt import SYSTEM



class ResearchAgent:
    def __init__(self, llm: LLMProvider, registry, documents=None,
                 max_steps: int = MAX_STEPS,
                 max_tool_calls: int = MAX_TOOL_CALLS,
                 evidence: list | None = None) -> None:
        self._llm = llm
        self._registry = registry
        self._documents = documents
        self._max_steps = max_steps
        self._max_tool_calls = max_tool_calls
        self._max_calls = max_tool_calls
        # Shared with the tools that mint evidence. The agent does not keep a
        # private copy: a Claim can only cite what a tool actually produced, and
        # the tool's sink and the agent's view have to be the same list.
        self._evidence: list = evidence if evidence is not None else []
        # One task, one session. An identical call that already succeeded is
        # replayed rather than re-run: a model that searches the same phrase
        # three times should cost one call, not three.
        self._cache: dict = {}
        #: The process record of the current task, rebuilt by every `run`.
        #: It is a transcript of one research task -- never a memory of the
        #: person asking, and never carried into the next task.
        self.session: ResearchSession | None = None
        self._sources: list[SourceRef] = []
        self._unresolved: list[str] = []
        self._observations: list[str] = []
        self._reactions: list = []
        self._routes: list = []
        self._route_notes: list[str] = []
        self._route_evidence: list = []

    # -- entry point --------------------------------------------------------

    def run(self, question: str, task_id: str | None = None, *,
            workflow: bool = False) -> ResearchTask:
        """Answer one question, or run one complete research task.

        `workflow=False` is the ordinary mode: a question goes in, an answer
        comes out, and a model failure is a failed task.

        `workflow=True` is the complete-research mode. The run walks a state
        machine -- RESEARCHING -> COLLECTING_EVIDENCE -> VALIDATING ->
        REPORTING -> COMPLETED | PARTIAL -- recorded on `self.session`, and
        its failure contract is stricter: **nothing may end as COMPLETED
        that is not complete**. A model that dies mid-run, a tool that never
        succeeded, an unanswered clarification -- all of them end PARTIAL
        with the gap named in the report. There is no way in this mode to
        produce a complete-looking report over incomplete work, which is
        the one outcome the phase exists to prevent.
        """
        from crea.core.provenance import utc_now_iso
        from crea.report.research import render_report

        task = ResearchTask(
            task_id=task_id or content_hash(f"{question}|{self._llm.name}", 16),
            question=question,
            status=TaskStatus.RUNNING,
        )
        self.session = ResearchSession(
            session_id=content_hash(
                f"{task.task_id}|{question}|{utc_now_iso()}|{time.time_ns()}", 16),
            task=task,
            status=WorkflowStatus.RESEARCHING,
        )

        messages: list[dict] = [
            {"role": "user", "content": f"Research question: {question}"}
        ]
        tool_calls = 0
        final_claims: list = []
        interpretation = ""
        evidence_by_tool: dict[str, list[str]] = {}

        for index in range(self._max_steps):
            step_id = f"{task.task_id}-s{index:02d}"
            try:
                from crea.llm.base import LLMRequest

                response = self._llm.complete(LLMRequest(
                    system=SYSTEM, messages=self._thread(messages),
                    tools=self._tool_schemas()))
            except Exception as exc:  # noqa: BLE001 - the model failing is a task outcome
                task.errors.append(f"llm provider failed: {type(exc).__name__}: {exc}")
                if workflow:
                    # A dead model mid-research is a PARTIAL run: whatever the
                    # tools already produced stays in the report, and the
                    # report says the run was cut short. Never FAILED here --
                    # workflow mode has no failed state -- and never a report
                    # that reads as if it finished.
                    self._unresolved.append(
                        f"LLM 未完成：{type(exc).__name__}，研究在中途终止，"
                        f"以下为已完成部分。")
                    task.status = TaskStatus.PARTIAL
                else:
                    task.status = TaskStatus.FAILED
                break

            action = self._parse(response.text, response.action)
            if action is None:
                task.errors.append(
                    f"step {index}: the model did not return a usable action")
                if workflow:
                    self._unresolved.append(
                        "LLM 未返回可用动作，研究在中途终止，以下为已完成部分。")
                    task.status = TaskStatus.PARTIAL
                else:
                    task.status = TaskStatus.FAILED
                break

            step = ResearchStep(
                step_id=step_id, index=index, action=action.action,
                tool=action.tool, arguments=redact(action.arguments),
                llm_provider=response.provider, llm_model=response.model,
            )

            if action.action is ActionKind.FINAL:
                step.result_status = ToolStatus.OK
                final_claims = self._verify_claims(action.claims) +                     self._resolve_claims(action.claim_specs, evidence_by_tool)
                interpretation = action.summary
                task.steps.append(step)
                break

            if action.action is ActionKind.CLARIFY:
                step.result_status = ToolStatus.OK
                step.note = "model asked for clarification"
                task.steps.append(step)
                self._unresolved.append(
                    action.question or "the model requested clarification")
                task.status = TaskStatus.PARTIAL
                break

            if tool_calls >= self._max_tool_calls:
                step.result_status = ToolStatus.REJECTED
                step.note = "tool-call budget reached"
                task.steps.append(step)
                self._unresolved.append("工具调用次数已达上限")
                task.status = TaskStatus.PARTIAL
                break

            tool_calls += 1
            cached = self._cache.get(_call_key(action.tool, action.arguments))
            if cached is not None:
                result = cached
                result = result.model_copy(update={
                    "step_id": step_id, "data": {
                        **(result.data if isinstance(result.data, dict) else {}),
                        "replayed_from_cache": True,
                    }})
            else:
                result = self._registry.execute(action.tool or "", action.arguments,
                                               step_id=step_id)
                if result.status is ToolStatus.OK:
                    self._cache[_call_key(action.tool, action.arguments)] = result
            observation = observe_from_registry(
                self._registry, result.tool, result)
            step.result_status = result.status
            step.error = result.error
            task.steps.append(step)
            self._absorb(result, observation)
            self._note_phase(result.tool)
            evidence_by_tool.setdefault(result.tool, []).extend(result.evidence_ids)
            # The model is told what actually happened, not just the payload:
            # a rejected or unavailable call must not look like an empty answer.
            messages.append({
                "role": "user",
                "content": f"tool_result({result.tool}) = "
                           + json.dumps(redact(observation.to_context()),
                                        ensure_ascii=False),
            })
        else:
            task.status = TaskStatus.PARTIAL
            self._unresolved.append("步数已达上限")

        task.evidence = list(self._evidence)
        task.sources = list(self._sources)
        task.unresolved_items = list(self._unresolved)
        self._note_phase("__report__")
        task.report = render_report(
            question=question,
            interpretation=interpretation,
            claims=final_claims,
            evidence=self._evidence,
            sources=self._sources,
            unresolved=self._unresolved,
            computed_notes=self._observations,
            reaction_details=self._reactions,
            routes=self._routes,
            route_notes=self._route_notes,
            route_evidence=self._route_evidence,
            steps=task.steps,
            tool_calls=tool_calls,
            status=task.status,
        )
        if task.status is TaskStatus.RUNNING:
            # A run that produced something but reached no evidenced
            # conclusion is partial, not failed. FAILED is reserved for a run
            # that produced nothing. Routes count as production: two candidate
            # routes and three validated steps is work, and reporting it as a
            # failure would teach the reader to distrust a report that isn't.
            produced = (self._evidence or self._observations or self._sources
                        or self._routes or self._reactions)
            if final_claims and (self._evidence or self._routes):
                task.status = TaskStatus.COMPLETED
            elif produced:
                task.status = TaskStatus.PARTIAL
                if self._routes and not self._evidence:
                    self._unresolved.append(
                        "路线步骤尚无文献或实验条件支持；"
                        "以上候选步骤仅为逆合成工具提出的方案。")
            else:
                task.status = TaskStatus.FAILED
        if workflow and task.status is TaskStatus.FAILED:
            # Workflow mode has no FAILED outcome: a run that produced
            # nothing is PARTIAL with the report saying so, because "FAILED"
            # invites a caller to discard the report that explains why.
            task.status = TaskStatus.PARTIAL
            self._unresolved.append("研究未能产出任何结论；以下为执行记录。")
        task.report.status = task.status
        from crea.core.provenance import utc_now_iso
        task.finished_at = utc_now_iso()
        self._finish_session(task)
        return task

    # -- workflow session ---------------------------------------------------

    def _note_phase(self, tool: str | None) -> None:
        """Move the session forward on the state machine, never backward.

        A run that validates after collecting evidence stays in VALIDATING
        even if the model then asks for one more search -- progress is the
        furthest stage reached, not the last thing that happened.
        """
        if self.session is None:
            return
        if tool == "__report__":
            target = WorkflowStatus.REPORTING
        else:
            target = _PHASE_FOR_TOOL.get(tool or "", WorkflowStatus.RESEARCHING)
        if _PHASE_RANK[target] > _PHASE_RANK[self.session.status]:
            self.session.status = target

    def _finish_session(self, task: ResearchTask) -> None:
        """Snapshot what the task produced onto its one session.

        One task, one session, and nothing carried forward: the session is a
        transcript of this run and dies with it. There is deliberately no
        history list and no user field here.
        """
        if self.session is None:
            return
        self.session.task = task
        self.session.steps = list(task.steps)
        self.session.findings = list(task.report.findings)
        self.session.sources = list(task.sources)
        self.session.evidence_ids = [e.evidence_id for e in task.evidence]
        self.session.route_ids = [r.route_id for r in task.report.routes]
        # Phase 3.10: the reaction knowledge this run produced, deduped and
        # in deterministic order -- a transcript of one task, never memory.
        reactions = getattr(self._registry, "reactions", None)
        if reactions is not None:
            held = list(reactions.all())
            self.session.reaction_ids = list(dict.fromkeys(
                r.reaction_id for r in held))
            self.session.chemical_entity_ids = list(dict.fromkeys(
                c.entity_id for r in held for c in r.components()
                if c.entity_id))
            self.session.chemical_mention_ids = list(dict.fromkeys(
                c.mention.mention_id for r in held for c in r.components()
                if c.mention is not None))
        self.session.status = (
            WorkflowStatus.COMPLETED
            if task.status is TaskStatus.COMPLETED
            else WorkflowStatus.PARTIAL
        )

    # -- helpers ------------------------------------------------------------

    def _resolve_claims(self, specs, evidence_by_tool: dict) -> list:
        """Build claims from evidence a tool actually produced.

        Every reference is looked up in what ran. A spec naming a tool that
        produced nothing yields a claim with no evidence, which the report
        renders as "insufficient" rather than dropping.
        """
        from crea.core.ids import content_hash
        from crea.core.models.evidence import Claim

        out = []
        for spec in specs:
            refs = list(evidence_by_tool.get(spec.from_tool, []))
            if not refs:
                continue          # nothing to stand on: not a claim
            out.append(Claim.build(
                claim_id=content_hash(f"{spec.claim_type}|{spec.text}", 16),
                text=spec.text, claim_type=spec.claim_type, evidence_refs=refs,
            ))
        return out

    def _tool_schemas(self) -> list[dict]:
        return self._registry.describe()

    def _verify_claims(self, claims) -> list:
        """Drop any claim citing evidence this task never produced.

        A model may propose a claim and name its evidence. It may not *create*
        the evidence. A reference that does not resolve is removed here rather
        than passed on, so no unverified claim ever reaches the report as if it
        were backed.
        """
        held = {e.evidence_id for e in self._evidence}
        verified = []
        for claim in claims:
            if not claim.evidence_refs:
                continue
            if all(r in held for r in claim.evidence_refs):
                verified.append(claim)
            else:
                self._unresolved.append(
                    f"模型提出的结论引用了不存在的证据，已剔除：{claim.text[:40]}")
        return verified

    def _thread(self, messages: list[dict]) -> list[dict]:
        """The model sees tool results and the tool catalogue. Nothing else."""
        catalogue = json.dumps(self._registry.describe(), ensure_ascii=False)
        head = [
            {"role": "system", "content": catalogue},
            {"role": "user",
             "content": "Registered tools (JSON Schema each): " + catalogue},
        ]
        return head + messages

    @staticmethod
    def _parse(text: str, action: dict | None = None) -> AgentAction | None:
        """Prefer a structured action; fall back to parsing the text.

        A provider that supports tool calling hands us `action` and the text is
        just the model's aside. A provider that does not -- the scripted one --
        puts JSON in the text. Neither path is privileged.
        """
        payload = action
        if payload is None:
            try:
                payload = json.loads(text or "")
            except (TypeError, ValueError):
                return None
        if not isinstance(payload, dict):
            return None
        try:
            return AgentAction.model_validate(payload)
        except Exception:  # noqa: BLE001
            return None

    def _absorb(self, result: ToolResult,
                observation: Observation | None = None) -> None:
        """Only a real result contributes. A model's account contributes nothing.

        M1.2: the observation decides what the *call* was, and every kind other
        than SUCCESS is recorded. Previously a `REJECTED` result was silently
        dropped -- the model got `data = null` and no reason, which reads as an
        empty answer rather than "this never ran".
        """
        if observation is None:
            observation = observe_from_registry(
                self._registry, result.tool, result)
        if result.status is ToolStatus.OK and isinstance(result.data, dict):
            for item in result.data.get("evidence_ids", []) or []:
                for evidence in self._evidence:
                    if evidence.evidence_id == item and item not in [
                            e.evidence_id for e in self._evidence]:
                        self._evidence.append(evidence)
            self._collect_sources(result)
            self._collect_observations(result)
            self._collect_reactions(result)
            self._collect_routes(result)
            self._collect_route_evidence(result)
        self._record_observation(observation)

    def _record_observation(self, observation: Observation) -> None:
        """Carry a non-success reading into the run's unresolved record.

        NO_RESULT is recorded too, but as an honourable miss: "queried, nothing
        matched" is not a failure and must not be described as one.
        """
        note = observation.summary or observation.error or ""
        tool = observation.tool
        if observation.kind is ObservationKind.NEEDS_INPUT:
            missing = (f"（缺少：{', '.join(observation.missing_requirements)}）"
                       if observation.missing_requirements else "")
            self._unresolved.append(
                f"工具 {tool} 需要补充信息后才能继续{missing}：{note}")
        elif observation.kind is ObservationKind.UNAVAILABLE:
            self._unresolved.append(f"工具 {tool} 当前不可用：{note}")
        elif observation.kind is ObservationKind.PARTIAL:
            self._unresolved.append(f"工具 {tool} 只返回了部分结果：{note}")
        elif observation.kind is ObservationKind.ERROR:
            self._unresolved.append(f"工具 {tool} 未成功：{note}")
        elif observation.kind is ObservationKind.NO_RESULT:
            item = f"工具 {tool} 查询完成，但未找到符合条件的结果"
            if item not in self._unresolved:
                self._unresolved.append(item)

    def _collect_observations(self, result: ToolResult) -> None:
        """Computed facts come from the tool that computed them.

        Recorded as observations of what a tool returned, never as a model's
        account of it. A tool that did not run contributes nothing.
        """
        if result.status is not ToolStatus.OK or not isinstance(result.data, dict):
            return
        data = result.data
        if result.tool == "analyze_molecule" and data.get("canonical_smiles"):
            self._observations.append(
                f"RDKit 计算：规范 SMILES = {data['canonical_smiles']}；"
                f"分子式 {data.get('molecular_formula')}；"
                f"分子量 {data.get('molecular_weight')}；"
                f"环数 {data.get('ring_count')}；"
                f"Murcko 骨架 {data.get('scaffold') or '(无环骨架)'}"
            )
        elif result.tool == "compare_structures":
            self._observations.append(
                f"RDKit 计算：两结构匹配判定 {data.get('verdict')}；"
                f"最大公共子结构 {data.get('common_atoms')} 原子 / "
                f"{data.get('common_bonds')} 键；"
                f"差异区域 {len(data.get('differing_regions') or [])} 处"
            )
        elif result.tool == "search_literature":
            self._observations.append(
                f"检索「{data.get('query')}」返回 {data.get('paper_count')} 篇"
                f"候选文献（未排序）"
            )
        elif result.tool == "search_local_document" and data.get("found"):
            self._observations.append(
                f"本地文档命中 {data.get('hit_count')} 处（未解读）"
            )

    def _collect_reactions(self, result: ToolResult) -> None:
        """Structured conditions the extractor validated.

        Only records the tool already returned. A model cannot put a
        ReactionRecord into the report by describing one; the tool runs first
        and whatever survives its checks is what arrives here.
        """
        if result.tool != "get_reaction_details" or result.status is not ToolStatus.OK:
            return
        from crea.core.models.reaction import ReactionRecord

        data = result.data if isinstance(result.data, dict) else {}
        for payload in data.get("records") or []:
            try:
                self._reactions.append(ReactionRecord.model_validate(payload))
            except Exception:  # noqa: BLE001 - a malformed record is not a record
                self._unresolved.append(
                    "提取工具返回了一条无法解析的反应记录，已忽略")
        for conflict in data.get("conflicts") or []:
            field = conflict.get("field", "条件")
            self._unresolved.append(
                f"提供的实验片段在 {field} 上存在冲突，未替你选择")

    def _collect_routes(self, result: ToolResult) -> None:
        """Routes the tools produced, never routes the model described.

        The route is read back out of the workbench the tools wrote to, not
        out of the model's own message, so a model that narrates a plausible
        synthesis contributes nothing here.
        """
        if result.status is not ToolStatus.OK or not isinstance(result.data, dict):
            return
        if result.tool == "validate_route_step":
            # the sentences the report must use when a step has nothing behind it
            for note in result.data.get("warnings", []):
                if note and note not in self._route_notes:
                    self._route_notes.append(note)
            return
        if result.tool != "retrosynthesize":
            return
        from crea.core.models.route import CandidateRoute

        # Rebuilt from what the tool returned, exactly as a reaction record is.
        # A model describing a route in prose contributes nothing here.
        rebuilt = []
        for payload in result.data.get("routes") or []:
            try:
                rebuilt.append(CandidateRoute.model_validate(payload))
            except Exception:  # noqa: BLE001 - a malformed route is not a route
                self._unresolved.append(
                    f"{result.tool} 返回了无法解析的路线，已忽略")
        self._routes = rebuilt
        for warning in result.data.get("warnings") or []:
            if warning not in self._route_notes:
                self._route_notes.append(warning)

    def _collect_route_evidence(self, result: ToolResult) -> None:
        """The reaction-level chain, rebuilt from what the tool returned.

        Same rule as every other collector: the report is built from a tool's
        output, never from the model's account of a tool. A model that
        describes a route as experimentally supported without ever calling
        `route_evidence_report` produces no chain, and therefore no promotion.
        """
        if result.status is not ToolStatus.OK or not isinstance(result.data, dict):
            return
        if result.tool != "route_evidence_report":
            return
        from crea.core.models.reaction_linking import RouteEvidenceReport

        for payload in result.data.get("reports") or []:
            try:
                self._route_evidence.append(RouteEvidenceReport.model_validate(payload))
            except Exception:  # noqa: BLE001 - an unreadable chain is not a chain
                self._unresolved.append(
                    "证据链工具返回了无法解析的结果，已忽略")
        for item in result.data.get("unresolved") or []:
            if item not in self._unresolved:
                self._unresolved.append(item)

    def _collect_sources(self, result: ToolResult) -> None:
        data = result.data if isinstance(result.data, dict) else {}
        for paper in data.get("papers", []) or []:
            if not isinstance(paper, dict):
                continue
            identifier = paper.get("doi") or paper.get("paper_id")
            if not identifier:
                continue
            ref = SourceRef(
                kind="paper", identifier=identifier,
                label=paper.get("title") or "", url=paper.get("url"),
                doi=paper.get("doi"),
            )
            if ref.identifier not in {s.identifier for s in self._sources}:
                self._sources.append(ref)


def _call_key(tool: str | None, arguments: dict) -> str:
    return f"{tool}|{json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)}"


__all__ = ["MAX_STEPS", "MAX_TOOL_CALLS", "ResearchAgent", "CreaError"]
