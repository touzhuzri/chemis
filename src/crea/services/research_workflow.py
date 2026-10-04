"""Research workflow -- one ResearchTask through the tools that already exist.

Phase 3.7, section 2. This layer orchestrates; it does not compute, search,
parse or validate anything itself. Every fact in the resulting report comes
back through a `ToolResult`, exactly as it does for the agent loop, so the
workflow cannot invent a structure, a paper or a route that no tool produced.

Two failure rules shape this class:

* An unavailable capability (a retrosynthesis engine with no model, a
  document that is not there, a tool that raises) is recorded -- the step
  outcome, an error note, `PARTIAL` status -- and the run continues with
  the steps that may still legitimately run. Nothing is faked to make the
  flow look complete.
* An unresolved identity ends the structure-dependent steps (analysis,
  retrosynthesis, validation). The workflow never guesses a structure to
  keep the pipeline moving; the report says "identity unresolved" instead.

The caller supplies the user's input explicitly (`structure_input`,
`document_id`); extracting a chemical name from a free-text question is
not something this module attempts, because a wrong extraction would be a
guessed structure wearing the user's question as a disguise.
"""

from __future__ import annotations

from crea.core.models.identity import StructureInput, StructureInputKind
from crea.core.models.literature import Paper
from crea.core.models.reaction import ReactionRecord
from crea.core.models.research import (
    ActionKind,
    ResearchReport,
    ResearchStep,
    ResearchTask,
    SourceRef,
    TaskStatus,
    ToolStatus,
)

#: Tool outcomes that mean the step did not happen. `UNAVAILABLE` and
#: `REJECTED` are listed with `ERROR` on purpose: a refusal and an outage
#: are both "no result", and the workflow must not read either as a fact.
_FAILED_STATUSES = (ToolStatus.ERROR, ToolStatus.UNAVAILABLE, ToolStatus.REJECTED)


class ResearchWorkflow:
    """Run one research task through the registered tools, in a fixed order:

    identity -> chemical analysis -> literature discovery -> local document
    evidence -> retrosynthesis -> route validation -> research report.

    The order is deliberate. Analysis, retrosynthesis and validation all
    consume the structure identity establishes, so identity runs first and
    its failure shortens the run instead of poisoning it.
    """

    def __init__(self, registry, *, evidence_sink: list | None = None,
                 documents=None, entities=None, reactions=None,
                 knowledge_base=None) -> None:
        """`registry` is the `ToolRegistry` built by `crea.agent.toolsets.build_registry`.

        `evidence_sink` must be the SAME list handed to `build_registry`:
        the document tools append `Evidence` there and the report reads it
        back from here. A second list would render a report whose evidence
        section silently disagrees with what the tools produced.
        """
        self._registry = registry
        self._evidence = evidence_sink if evidence_sink is not None else []
        #: Phase 3.10: the reaction intelligence layer, wired in rather than
        #: rebuilt. `documents` lets the workflow read a supplied PDF's text
        #: for mention resolution; `entities`/`reactions` are the registries
        #: this run writes to (created when absent, so the workflow runs the
        #: same either way).
        self._documents = documents
        self._entities = entities
        self._reactions = reactions
        #: The persistent knowledge base (Phase 3.11), when one is wired. The
        #: run's result is committed to it at the end of `run`; without one the
        #: workflow is byte-for-byte what it was before (§36/§41).
        self._knowledge_base = knowledge_base
        #: The session this run produced, filled at the end of `run`.
        self.session: "ResearchSession | None" = None
        #: A caller's phase callback (Phase 4 §38): called at each boundary so
        #: it can report progress -- and raise to stop the run cooperatively
        #: at a safe point. None means no one is watching.
        self.on_phase = None

    def run(
        self,
        task: ResearchTask,
        *,
        structure_input: StructureInput | None = None,
        document_id: str | None = None,
        document_query: str = "Experimental",
        literature_limit: int = 10,
        paper_id: str = "",
        doi: str = "",
    ) -> ResearchReport:
        """Execute the workflow for `task` and return its research report.

        `structure_input` is the user's chemical input as given -- a name, a
        SMILES, an InChI. When it is absent or does not resolve, the
        structure-dependent steps are skipped and recorded, never guessed.
        `document_id` is a document already ingested into the library the
        registry's document tools read from; when it is absent the report
        carries "No user document provided" instead of a fabricated one.
        """
        # Deferred like `ResearchAgent.run` does: presentation is built from
        # core models only, but the report module is not this layer's concern
        # until there is a report to render.
        from crea.core.provenance import utc_now_iso
        from crea.report.research import render_report

        task.status = TaskStatus.RUNNING
        # What the knowledge base held before this run looked (Phase 3.12).
        # Taken first, so `new` is genuinely what THIS run added (§100).
        existing_ids = (self._snapshot()
                        if self._knowledge_base is not None else {})
        from crea.core.ids import content_hash
        from crea.core.models.session import ResearchSession, WorkflowStatus

        session = ResearchSession(
            session_id=content_hash(
                f"{task.task_id}|{utc_now_iso()}|{__import__('time').time_ns()}", 16),
            task=task, status=WorkflowStatus.RESEARCHING)
        self.session = session

        def phase(name: str, status) -> None:
            """Record the internal phase; the external state machine stays
            the six WorkflowStatus values (§4)."""
            session.phases.append(name)
            session.status = status
            if self.on_phase is not None:
                # A caller watching for cancellation raises here, at a step
                # boundary -- never mid-write, so knowledge stays consistent.
                self.on_phase(name)
        unresolved: list[str] = []
        computed_notes: list[str] = []
        reaction_details: list[ReactionRecord] = []
        route_notes: list[str] = []
        route_evidence: list = []
        sources: list[SourceRef] = []
        #: Works the literature step discovered, kept as records when a
        #: knowledge base is wired so the next run finds them existing (§41).
        discovered: list = []
        #: Steps that produced what they were meant to produce, for the
        #: report's interpretation line. Steps that ran and produced nothing
        #: are in `unresolved` instead -- executing is not achieving.
        executed: list[str] = []
        index = 0
        tool_calls = 0
        partial = False

        def call(tool: str, arguments: dict):
            """Execute one tool, record the step, and flag anything that failed.

            A failed call becomes a `ResearchStep`, an entry in
            `task.errors` and `unresolved`, and marks the run partial --
            all three, because the trace, the machine-readable task and the
            rendered report are read by different people.
            """
            nonlocal index, tool_calls, partial
            index += 1
            tool_calls += 1
            step_id = f"{task.task_id}-w{index:02d}"
            result = self._registry.execute(tool, arguments, step_id=step_id)
            task.steps.append(ResearchStep(
                step_id=step_id, index=index - 1,
                action=ActionKind.TOOL_CALL, tool=tool,
                arguments=dict(arguments),
                result_status=result.status, error=result.error,
            ))
            if result.status in _FAILED_STATUSES:
                partial = True
                message = f"{tool} 未成功：{result.error or result.status.value}"
                task.errors.append(message)
                unresolved.append(message)
            return result

        def skip(tool: str, reason: str) -> None:
            """Record a step that was deliberately not run.

            A skip is visible in the trace with `result_status=None` and a
            note saying why. Silence would make an absent step look like a
            step nobody thought of, which is a different claim.
            """
            nonlocal index
            index += 1
            step_id = f"{task.task_id}-w{index:02d}"
            task.steps.append(ResearchStep(
                step_id=step_id, index=index - 1,
                action=ActionKind.TOOL_CALL, tool=tool,
                result_status=None, note=f"skipped：{reason}",
            ))

        phase("researching.identity", WorkflowStatus.RESEARCHING)
        # -- Step 1: identity ------------------------------------------------
        # No input, or a lookup that does not resolve, means no structure.
        # The structure-dependent steps below branch on `smiles`; they never
        # fall back to a plausible one.
        smiles: str | None = None
        entity_id: str | None = None
        if structure_input is None:
            skip("resolve_chemical_identity", "未提供 structure_input")
            unresolved.append(
                "identity unresolved：未提供 chemical name 或结构输入；未猜测结构。")
            partial = True
        else:
            result = call("resolve_chemical_identity", {
                "kind": structure_input.kind.value,
                "value": structure_input.value,
            })
            data = (result.data
                    if result.status is ToolStatus.OK
                    and isinstance(result.data, dict) else {})
            selected = data.get("selected") or {}
            if data.get("chemistry_allowed") and selected.get("canonical_smiles"):
                smiles = selected["canonical_smiles"]
                entity_id = data.get("entity_id")
                executed.append("身份解析")
            else:
                reason = (data.get("agent_message") or data.get("reason")
                          or result.error or "未能建立结构")
                unresolved.append(f"identity unresolved：{reason}")
                partial = True

        # -- Step 1.5: assess existing knowledge (Phase 3.12) ----------------
        # With a knowledge base wired, the run reads what is already
        # established BEFORE searching: existing knowledge decides what this
        # run needs to add (§7/§13-§16), and a facet already covered is not
        # mechanically re-searched (§39). Without a knowledge base the plan is
        # absent and every step runs exactly as before (§36).
        plan = None
        if self._knowledge_base is not None:
            phase("assessing_knowledge", WorkflowStatus.RESEARCHING)
            from crea.services.research_intelligence import (
                assess_coverage,
                build_research_plan,
                find_gaps,
            )

            _cov = assess_coverage(self._knowledge_base, entity_id=entity_id,
                                   target=task.question)
            _gaps = find_gaps(_cov)
            plan = build_research_plan(
                _cov, _gaps, question=task.question,
                has_document=bool(document_id))
            task.steps.append(ResearchStep(
                step_id=f"{task.task_id}-w{index + 1:02d}", index=index,
                action=ActionKind.TOOL_CALL, tool="get_existing_knowledge",
                result_status=ToolStatus.OK,
                note=f"已有知识评估（§13）：{len(_gaps)} 处缺口",
            ))
            index += 1

        # -- Step 2: chemical analysis (computed, never evidence) ------------
        if smiles:
            result = call("analyze_molecule", {"smiles": smiles})
            if result.status is ToolStatus.OK:
                data = result.data
                computed_notes.append(
                    f"规范 SMILES {data.get('canonical_smiles')}；"
                    f"分子式 {data.get('molecular_formula')}；"
                    f"分子量 {data.get('molecular_weight')}；"
                    f"环数 {data.get('ring_count')}")
                executed.append("分子分析")
        else:
            skip("analyze_molecule", "identity unresolved，跳过")

        phase("researching.literature", WorkflowStatus.RESEARCHING)
        # -- Step 3: literature discovery ------------------------------------
        # Papers found here enter the report as sources, never as evidence:
        # discovery is not proof, and metadata cannot be upgraded into
        # experimental support by being listed.
        literature_query = (
            structure_input.value
            if structure_input is not None
            and structure_input.kind is StructureInputKind.NAME
            else task.question
        )
        from crea.core.models.knowledge import ResearchPlanAction

        search_planned = (plan is None or any(
            step.action is ResearchPlanAction.SEARCH_LITERATURE
            for step in plan.steps))
        if not search_planned:
            # §39: the base already covers the facets a search would fill, and
            # the user did not ask for the latest -- so the run reuses what it
            # holds instead of re-searching. Recorded, never silent.
            skip("search_literature", "知识已覆盖且未要求最新文献（§39）")
        else:
            lit_args = {"query": literature_query, "limit": literature_limit}
            if self._knowledge_base is not None:
                # With a base wired, the run keeps the works it discovered --
                # a `Paper` is a literature record, and a second run must find
                # it as EXISTING knowledge rather than re-discover it (§41).
                lit_args["include_records"] = True
            result = call("search_literature", lit_args)
            if result.status is ToolStatus.OK:
                papers = result.data.get("papers") or []
                for record in result.data.get("paper_records") or []:
                    try:
                        discovered.append(Paper.model_validate(record))
                    except Exception:  # noqa: BLE001 - unreadable record is data
                        continue
                for paper in papers:
                    identifier = paper.get("doi") or paper.get("paper_id")
                    if not identifier:
                        continue
                    ref = SourceRef(kind="paper", identifier=identifier,
                                    label=paper.get("title") or "",
                                    url=paper.get("url"), doi=paper.get("doi"))
                    if ref.identifier not in {s.identifier for s in sources}:
                        sources.append(ref)
                if papers:
                    executed.append("文献检索")
                else:
                    unresolved.append("文献检索未返回任何论文")

        # -- Step 4: local document evidence, only when the user supplied one
        if not document_id:
            unresolved.append("No user document provided")
        else:
            evidence_result = call("get_document_evidence", {
                "document_id": document_id, "query": document_query,
                "max_evidence": 5,
            })
            # The Document -> Evidence -> ReactionRecord chain stops at the
            # first failure; the failure itself is already recorded above.
            if evidence_result.status is ToolStatus.OK:
                created = evidence_result.data.get("created") or 0
                if created:
                    executed.append("本地文档")
                else:
                    partial = True
                    unresolved.append(
                        f"本地文档 {document_id} 未产生 Evidence："
                        f"query {document_query!r} 没有匹配的段落")

                record_result = call("get_reaction_details", {
                    "document_id": document_id, "query": document_query,
                })
                if record_result.status is ToolStatus.OK:
                    data = record_result.data
                    for payload in data.get("records") or []:
                        try:
                            reaction_details.append(
                                ReactionRecord.model_validate(payload))
                        except Exception:  # noqa: BLE001 - an unreadable record is not a record
                            unresolved.append(
                                "提取结果包含无法解析的反应记录，已忽略")
                    for conflict in data.get("conflicts") or []:
                        unresolved.append(
                            f"实验片段中 {conflict.get('field', '字段')} 存在冲突，"
                            f"未替你选择其中之一")
                    for note in data.get("notes") or []:
                        if note:
                            unresolved.append(str(note))
                    if not reaction_details:
                        # The document was supplied but no record survived
                        # extraction. That is a gap in this environment (no
                        # extraction model, or nothing extractable), and the
                        # run is partial rather than quietly complete.
                        partial = True

        # -- Step 4.5: mentions, entities, reactions (Phase 3.10) ----------
        # The knowledge layer joins here. Every name in the document becomes
        # a mention; every mention is resolved by the identity layer -- never
        # by a model; the record plus those resolutions becomes a Reaction.
        # A paper whose metadata was all we had produces no reaction, and the
        # report says so rather than inventing one (§9).
        mentions: list = []
        resolutions: list = []
        reactions_built: list = []
        entities_registry, mention_resolver, reaction_builder, reaction_registry = (
            self._knowledge_stack())
        if self._knowledge_base is not None:
            # Held evidence is knowledge already (§18): a persistent registry
            # refuses a reaction whose citations do not resolve, so what this
            # run is about to cite is stored first, append-only.
            for item in self._evidence:
                self._knowledge_base.save_evidence(item)
        if reaction_details:
            phase("researching.mentions", WorkflowStatus.RESEARCHING)
            document_text = self._document_text(document_id)
            if document_text:
                from crea.services.mention_resolution import extract_chemical_mentions

                # names the record itself uses, so the document's occurrences
                # of them are found without a name guess
                names = []
                for record in reaction_details:
                    for item in (record.conditions.reactants
                                 + record.conditions.reagents
                                 + record.conditions.catalysts
                                 + record.conditions.ligands
                                 + record.conditions.solvents
                                 + record.conditions.unassigned):
                        if item.name:
                            names.append(item.name)
                raw_mentions = extract_chemical_mentions(
                    document_text, known_names=names,
                    source="user_provided", source_id=document_id or "")
                resolutions = [mention_resolver.resolve(m) for m in raw_mentions]
                mentions = [r.mention for r in resolutions]
            phase("researching.reactions", WorkflowStatus.COLLECTING_EVIDENCE)
            from crea.services.reaction_builder import (
                product_mentions_from_document,
            )

            for record in reaction_details:
                product_mentions = product_mentions_from_document(
                    record, document_text or "", mention_resolver,
                    source="user_provided", source_id=document_id or "")
                reaction = reaction_builder.build(
                    record, paper_id=paper_id or "", doi=doi or "",
                    source_id=document_id or "",
                    product_mentions=product_mentions)
                reaction_registry.register(reaction)
                reactions_built.append(reaction)
            phase("collecting_evidence.reactions", WorkflowStatus.COLLECTING_EVIDENCE)

        # -- Step 5: retrosynthesis (candidates only) ------------------------
        if not smiles:
            skip("retrosynthesize", "identity unresolved，跳过")
        else:
            result = call("retrosynthesize",
                          {"target_smiles": smiles, "max_routes": 3})
            if result.status is ToolStatus.OK:
                data = result.data
                for warning in data.get("warnings") or []:
                    if warning and warning not in route_notes:
                        route_notes.append(warning)
                if data.get("route_count"):
                    executed.append("逆合成")
                else:
                    # No provider, no model/stock, or an empty search: the
                    # capability did not deliver, so the run is partial and
                    # the report says why. No placeholder route is written.
                    partial = True
                    unresolved.append(
                        "逆合成未返回候选路线（unavailable）："
                        + str(data.get("reason") or data.get("status") or "无结果"))

        # -- Step 5.5: reaction <-> route links (Phase 3.10) ----------------
        # A measured link per (step, reaction). The linker records what the
        # comparison found and nothing else: support levels move only inside
        # RouteValidationService / RouteEvidenceService (§57), and a similar
        # reaction is screening-only, never proof (§35).
        links: list = []
        record_links: list = []
        if smiles and reactions_built:
            from crea.services.route_reaction_linking import RouteReactionLinker

            linker = RouteReactionLinker()
            validator = getattr(self._registry, "validator", None)
            workbench = getattr(self._registry, "routes", None)
            held_records = {r.reaction_id: r
                            for r in (getattr(workbench, "records", []) or [])}
            for route in (getattr(workbench, "routes", None) or []):
                for step in route.steps:
                    for reaction in reactions_built:
                        link = linker.link(step, reaction)
                        links.append(link)
                        if link.screening_only:
                            continue
                        record = held_records.get(reaction.record_id)
                        if validator is not None:
                            linker.stage(validator, reaction, record,
                                         evidence=self._evidence)
                        record_links.append({
                            "step_id": step.step_id,
                            "reaction_id": reaction.record_id,
                            "reactant_smiles": [c.canonical_smiles
                                                 for c in reaction.reactants
                                                 if c.canonical_smiles],
                            "product_smiles": [c.canonical_smiles
                                                for c in reaction.products
                                                if c.canonical_smiles],
                        })
            if links:
                executed.append("反应-路线比对")

        # -- Step 6: route validation, then the evidence chain ---------------
        # Every route stays at whatever the validator earns it. Nothing here
        # sets a status; only `validate_route_step` -> RouteValidationService
        # may move a step, and only upward, and only from held evidence.
        workbench = getattr(self._registry, "routes", None)
        routes = list(getattr(workbench, "routes", None) or []) if workbench else []

        if not smiles:
            skip("validate_route_step", "identity unresolved，跳过")
            skip("route_evidence_report", "identity unresolved，跳过")
        elif not routes:
            skip("validate_route_step", "没有候选路线")
            skip("route_evidence_report", "没有候选路线")
        else:
            validated = 0
            for route in routes:
                for step in route.steps:
                    result = call("validate_route_step",
                                  {"step_id": step.step_id})
                    if result.status is ToolStatus.OK:
                        validated += 1
                        for warning in result.data.get("warnings") or []:
                            if warning and warning not in route_notes:
                                route_notes.append(warning)
            if validated:
                executed.append("路线验证")
            phase("validating.routes", WorkflowStatus.VALIDATING)
            result = call("route_evidence_report",
                          {"record_links": record_links})
            if result.status is ToolStatus.OK:
                route_evidence = list(
                    getattr(workbench, "evidence_reports", None) or [])
                for item in result.data.get("unresolved") or []:
                    if item and item not in unresolved:
                        unresolved.append(str(item))
                executed.append("证据链报告")

        # -- Step 7: the report ---------------------------------------------
        # The status says how far the run got; a partial run renders as
        # partial, with every gap in `unresolved_items`. The report is built
        # from what the tools returned -- computed notes, held evidence,
        # discovered sources, candidate routes -- and from nothing else.
        phase("reporting", WorkflowStatus.REPORTING)
        task.status = TaskStatus.PARTIAL if partial else TaskStatus.COMPLETED
        unresolved = list(dict.fromkeys(unresolved))
        if executed:
            interpretation = ("ResearchWorkflow 顺序执行研究步骤，成功完成："
                              + "、".join(executed) + "。")
        else:
            interpretation = "ResearchWorkflow 顺序执行研究步骤，未有步骤成功完成。"

        report = render_report(
            question=task.question,
            interpretation=interpretation,
            claims=[],
            evidence=list(self._evidence),
            sources=sources,
            unresolved=unresolved,
            steps=task.steps,
            tool_calls=tool_calls,
            status=task.status,
            computed_notes=computed_notes,
            reaction_details=reaction_details,
            routes=routes,
            route_notes=route_notes,
            route_evidence=route_evidence,
            reactions=reactions_built,
            reaction_links=links,
        )
        task.report = report
        task.evidence = list(self._evidence)
        task.sources = sources
        task.unresolved_items = unresolved
        task.finished_at = utc_now_iso()

        # -- the session: one task's transcript, nothing remembered (§5/§16)
        session.steps = list(task.steps)
        session.findings = list(report.findings)
        session.sources = list(sources)
        session.evidence_ids = [e.evidence_id for e in self._evidence]
        session.route_ids = [r.route_id for r in report.routes]
        session.reaction_ids = list(dict.fromkeys(
            r.reaction_id for r in reactions_built))
        session.chemical_entity_ids = list(dict.fromkeys(
            ([entity_id] if entity_id else [])
            + [c.entity_id for r in reactions_built
               for c in r.components() if c.entity_id]))
        session.chemical_mention_ids = list(dict.fromkeys(
            [m.mention_id for m in mentions]
            + [c.mention.mention_id for r in reactions_built
               for c in r.components() if c.mention is not None]))
        session.status = (WorkflowStatus.COMPLETED
                          if task.status is TaskStatus.COMPLETED
                          else WorkflowStatus.PARTIAL)

        # -- persist the run (Phase 3.11, §52) ----------------------------
        # One unit of work: the entities, mentions, evidence, reactions and
        # the records behind them, the routes, the measured links and the
        # session all land together, or none of them does. A run with no
        # knowledge base wired persists nothing -- and is otherwise unchanged.
        if self._knowledge_base is not None:
            self._knowledge_base.persist_research_result(
                session=session,
                entities=[entities_registry.get(e)
                          for e in entities_registry.entity_ids],
                mentions=list(mentions),
                papers=list(discovered),
                evidence=list(self._evidence),
                reactions=list(reactions_built),
                records=list(reaction_details),
                routes=list(report.routes),
                links=list(links),
            )
            report.knowledge = self._knowledge_report(existing_ids, task, entity_id)
        return report

    # -- knowledge report (Phase 3.12) --------------------------------------

    def _snapshot(self) -> dict:
        """The ids the knowledge base holds, by kind (§100)."""
        kb = self._knowledge_base
        return {
            "entities": [e.entity_id for e in kb.list_entities()],
            "mentions": [m.mention_id for m in kb.list_mentions()],
            "papers": [p.paper_id for p in kb.list_papers()],
            "evidence": [e.evidence_id for e in kb.list_evidence()],
            "reactions": [r.reaction_id for r in kb.list_reactions()],
            "reaction_records": [r.reaction_id for r in kb.list_reaction_records()],
            "routes": [r.route_id for r in kb.list_routes()],
        }

    def _knowledge_report(self, existing_ids: dict, task,
                          entity_id: str | None) -> object:
        """What the base knew and what this run changed (§100)."""
        from crea.core.models.knowledge import KnowledgeReport
        from crea.services.research_intelligence import (
            assess_coverage,
            find_gaps,
            synthesize_fields,
        )

        after = self._snapshot()
        new = {kind: [i for i in ids if i not in set(existing_ids.get(kind, []))]
               for kind, ids in after.items()}
        existing = {kind: list(existing_ids.get(kind, [])) for kind in after}
        try:
            coverage = assess_coverage(self._knowledge_base, entity_id=entity_id,
                                       target=task.question)
            gaps = find_gaps(coverage)
            # conflicts live on the recorded conditions of every reaction this
            # entity takes part in -- across runs, so a second run's different
            # temperature is a CONFLICT, kept with both citations (§22-§24)
            conflicts = synthesize_fields(
                self._knowledge_base, entity_id=entity_id).conflicts
        except Exception:  # noqa: BLE001 - knowledge report is best-effort
            coverage, gaps, conflicts = None, [], []
        return KnowledgeReport(
            coverage=coverage, gaps=gaps, conflicts=conflicts,
            existing=existing, new=new)


    # -- knowledge-layer wiring (Phase 3.10) -------------------------------

    def _knowledge_stack(self):
        """The entity/mention/reaction services this run writes to.

        Assembled from what the registry already holds -- the identity
        service the identity step used, the evidence the tools produced --
        so the knowledge layer sees exactly what the workflow saw. No LLM
        appears here: extraction goes through the extraction tool (§20).
        """
        from crea.services.entity_registry import ChemicalEntityRegistry
        from crea.services.mention_resolution import (
            ChemicalMentionResolver,
            ParentheticalContext,
        )
        from crea.services.reaction_builder import (
            ReactionBuilder,
            ReactionRegistry,
        )

        entities = self._entities if self._entities is not None             else ChemicalEntityRegistry()
        reactions = self._reactions if self._reactions is not None             else ReactionRegistry()
        identity_state = getattr(self._registry, "identity", None)
        service = getattr(identity_state, "service", None)
        resolver = ChemicalMentionResolver(
            entities, identity=service,
            context_sources=(ParentheticalContext(),),
            held_evidence=self._evidence)
        builder = ReactionBuilder(resolver, entity_registry=entities,
                                  held_evidence=self._evidence)
        return entities, resolver, builder, reactions

    def _document_text(self, document_id: str | None) -> str:
        """The supplied document's text, or an empty string when there is none."""
        if not document_id or self._documents is None:
            return ""
        parsed = self._documents.get(document_id)
        if parsed is None:
            return ""
        return "\n".join(page.text for page in parsed.pages if page.text)


__all__ = ["ResearchWorkflow"]
