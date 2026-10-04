"""Research intelligence: what we know, what is missing, what to do next.

Phase 3.12. The knowledge base remembers; this service *reads* that memory
the way a researcher would. Three deterministic questions, asked in order:

1. **What is already established?** -- `assess_coverage` turns stored
   objects into `KnowledgeCoverage`: per-facet states (KNOWN /
   PARTIALLY_KNOWN / UNKNOWN) with the object ids each state rests on.
2. **What is missing?** -- `find_gaps` derives `KnowledgeGap`s from the
   coverage, each with a sentence a person can read ("已有反应结构信息，
   但尚未找到可验证的实验条件证据。"). A gap is a fact about knowledge,
   never a failure and never a fabricated value (§12/§16).
3. **What should this run do?** -- `build_research_plan` produces a
   `ResearchPlan` whose steps follow from the gaps (§17/§18). No model
   invents the plan. When the knowledge covers the question and the user
   did not ask for the latest literature, the plan says so and the search
   step is absent (§39/§40).

Plus `synthesize_fields`: the condition values different records state for
one body of reactions, side by side, each with its own evidence -- and
`FieldConflict`s where sources disagree. The synthesis never averages,
ranks or picks (§22-§24): `CONFLICT` means two valid reports disagree,
not that one is wrong.

Everything here reads the knowledge base and produces models. It writes
nothing (§108), imports no LLM, no provider and no RDKit.
"""

from __future__ import annotations

import re

from crea.core.models.knowledge import (
    EvidenceSynthesis,
    FacetKnowledge,
    FieldConflict,
    FieldObservation,
    KnowledgeCoverage,
    KnowledgeFacet,
    KnowledgeGap,
    KnowledgeState,
    ResearchPlan,
    ResearchPlanAction,
    ResearchPlanStep,
)

#: Deterministic markers that a question wants *current* literature (§40).
#: Words only -- the question text is never fed to a model to decide this.
_LATEST_MARKERS = re.compile(
    r"(最新|最近|近期|新文献|新的文献|20\d\d|recent|latest|new\s+papers?)",
    re.IGNORECASE)

#: Condition fields compared across records (§22). Quantities first; the
#: text fields the procedure also states are observations of the same kind.
_QUANTITY_FIELDS = ("temperature", "time", "pressure", "concentration",
                    "yield_value")
_TEXT_FIELDS = ("atmosphere", "workup", "purification")


def wants_latest_literature(question: str) -> bool:
    """True only when the question's own words ask for the latest (§40).

    "最新 / 最近 / 2026 / recent / latest" and nothing else. A question
    that merely mentions a year in a compound name stays False: the rule
    is spelling, not interpretation.
    """
    return bool(_LATEST_MARKERS.search(question or ""))


# ---------------------------------------------------------------------------
# coverage (§8/§12)
# ---------------------------------------------------------------------------


def assess_coverage(kb, *, entity_id: str | None = None,
                    target: str = "") -> KnowledgeCoverage:
    """What the knowledge base already holds for one target.

    States follow from named objects only -- a facet is KNOWN because
    stored reactions/evidence/routes stand behind it. KNOWN never means
    "current" (§15): it means CREA has recorded, cited sources about this.
    """
    facets: list[FacetKnowledge] = []

    entity = None
    if entity_id:
        from crea.repositories.errors import NotFound

        try:
            entity = kb.get_entity(entity_id)
        except NotFound:
            entity = None

    if entity is not None:
        facets.append(FacetKnowledge(
            facet=KnowledgeFacet.ENTITY, state=KnowledgeState.KNOWN,
            detail=f"已建立实体 {entity.entity_id}（identity key 有据）",
            object_ids=[entity.entity_id]))
    else:
        facets.append(FacetKnowledge(
            facet=KnowledgeFacet.ENTITY, state=KnowledgeState.UNKNOWN,
            detail="知识库中没有该目标的 ChemicalEntity"))

    reactions = kb.search_reactions(entity_id=entity_id) if entity_id else []
    reaction_ids = [r.reaction_id for r in reactions]

    papers = list(kb.get_papers_for_entity(entity_id)) if entity_id else []
    paper_ids = [p.paper_id for p in papers]
    if reactions:
        for reaction in reactions:
            scope = reaction.paper_id or reaction.doi
            if scope and scope not in paper_ids:
                paper_ids.append(scope)
    facets.append(FacetKnowledge(
        facet=KnowledgeFacet.PAPER,
        state=KnowledgeState.KNOWN if paper_ids else KnowledgeState.UNKNOWN,
        detail=(f"{len(paper_ids)} 篇来源指向该化合物" if paper_ids
                else "没有论文/来源指向该化合物"),
        object_ids=paper_ids))

    if not reactions:
        reaction_state = KnowledgeState.UNKNOWN
        reaction_detail = "没有已建立的反应"
    elif all(r.status.value == "partial" or not r.reaction_key
             or "name:" in r.reaction_key for r in reactions):
        reaction_state = KnowledgeState.PARTIALLY_KNOWN
        reaction_detail = (f"{len(reactions)} 个反应，但均未完全解析"
                           f"（PARTIAL 或 name-based key）")
    else:
        reaction_state = KnowledgeState.KNOWN
        reaction_detail = f"{len(reactions)} 个已建立的反应"
    facets.append(FacetKnowledge(
        facet=KnowledgeFacet.REACTION, state=reaction_state,
        detail=reaction_detail, object_ids=reaction_ids))

    experimental_ids = [r.reaction_id for r in reactions
                        if r.status.value == "experimentally_supported"]
    evidence_ids: list[str] = []
    for reaction in reactions:
        for item in kb.get_evidence_for_reaction(reaction.reaction_id):
            if item.evidence_id not in evidence_ids:
                evidence_ids.append(item.evidence_id)
    if experimental_ids:
        evidence_state = KnowledgeState.KNOWN
        evidence_detail = (f"{len(experimental_ids)} 个反应有实验级支持，"
                           f"{len(evidence_ids)} 条证据")
    elif evidence_ids:
        evidence_state = KnowledgeState.PARTIALLY_KNOWN
        evidence_detail = (f"{len(evidence_ids)} 条引用证据，"
                           f"但没有实验级（EXPERIMENTALLY_SUPPORTED）支持")
    else:
        evidence_state = KnowledgeState.UNKNOWN
        evidence_detail = "没有任何证据站在反应后面"
    facets.append(FacetKnowledge(
        facet=KnowledgeFacet.EXPERIMENTAL_EVIDENCE, state=evidence_state,
        detail=evidence_detail,
        object_ids=experimental_ids or evidence_ids))

    route_hits = kb.get_routes_for_entity(entity_id) if entity_id else []
    route_ids = list(dict.fromkeys(h["route_id"] for h in route_hits))
    facets.append(FacetKnowledge(
        facet=KnowledgeFacet.ROUTE,
        state=KnowledgeState.KNOWN if route_ids else KnowledgeState.UNKNOWN,
        detail=(f"{len(route_ids)} 条候选路线涉及该化合物" if route_ids
                else "没有候选路线记录"),
        object_ids=route_ids))

    exact_links: list[str] = []
    any_links: list[str] = []
    backed = {r.reaction_id for r in reactions
              if r.status.value in ("evidence_backed",
                                    "experimentally_supported")}
    for route_id in route_ids:
        for link in kb.store.links_for_route(route_id):
            any_links.append(link.link_id)
            if not link.screening_only and link.reaction_id in backed:
                exact_links.append(link.link_id)
    if exact_links:
        route_evidence_state = KnowledgeState.KNOWN
        route_evidence_detail = (f"{len(exact_links)} 个步骤有精确的文献反应链接")
    elif any_links:
        route_evidence_state = KnowledgeState.PARTIALLY_KNOWN
        route_evidence_detail = "只有筛选级（screening only）链接，没有精确支持"
    else:
        route_evidence_state = KnowledgeState.UNKNOWN
        route_evidence_detail = "路线与文献反应之间没有已测量的链接"
    facets.append(FacetKnowledge(
        facet=KnowledgeFacet.ROUTE_EVIDENCE, state=route_evidence_state,
        detail=route_evidence_detail,
        object_ids=exact_links or any_links))

    return KnowledgeCoverage(
        entity_id=entity.entity_id if entity is not None else entity_id,
        target=target or (entity.entity_id if entity is not None else ""),
        facets=facets)


# ---------------------------------------------------------------------------
# gaps (§12/§16)
# ---------------------------------------------------------------------------


def find_gaps(coverage: KnowledgeCoverage) -> list[KnowledgeGap]:
    """Every facet that is not fully established, in facet order.

    Each gap is one readable sentence plus the kind of work that would
    close it. Nothing here is a failure report: "没有实验压力记录" is a
    research outcome, said plainly (§12).
    """
    gaps: list[KnowledgeGap] = []

    def state(facet: KnowledgeFacet) -> KnowledgeState:
        return coverage.state_of(facet)

    if state(KnowledgeFacet.ENTITY) is KnowledgeState.UNKNOWN:
        gaps.append(KnowledgeGap(
            facet=KnowledgeFacet.ENTITY,
            reason="目标结构尚未建立 ChemicalEntity。",
            suggested_action="resolve identity"))

    if state(KnowledgeFacet.PAPER) is KnowledgeState.UNKNOWN:
        gaps.append(KnowledgeGap(
            facet=KnowledgeFacet.PAPER,
            reason="尚无论文或来源记录指向该化合物。",
            suggested_action="search literature"))
    elif state(KnowledgeFacet.PAPER) is KnowledgeState.PARTIALLY_KNOWN:
        gaps.append(KnowledgeGap(
            facet=KnowledgeFacet.PAPER,
            reason="只有部分来源与该化合物建立了关联。",
            suggested_action="search literature"))

    reaction_state = state(KnowledgeFacet.REACTION)
    evidence_state = state(KnowledgeFacet.EXPERIMENTAL_EVIDENCE)
    if reaction_state is KnowledgeState.UNKNOWN:
        gaps.append(KnowledgeGap(
            facet=KnowledgeFacet.REACTION,
            reason="尚未建立该化合物的反应知识。",
            suggested_action="search literature; extract reaction evidence"))
    elif reaction_state is KnowledgeState.PARTIALLY_KNOWN:
        gaps.append(KnowledgeGap(
            facet=KnowledgeFacet.REACTION,
            reason="已有反应记录，但都未完全解析（组分未解析或 key 基于名称）。",
            suggested_action="extract reaction evidence"))

    if evidence_state is KnowledgeState.UNKNOWN and (
            reaction_state is not KnowledgeState.UNKNOWN):
        # the sentence §12 names: structures known, experimental conditions not
        gaps.append(KnowledgeGap(
            facet=KnowledgeFacet.EXPERIMENTAL_EVIDENCE,
            reason="已有反应结构信息，但尚未找到可验证的实验条件证据。",
            suggested_action="inspect local document; extract reaction evidence",
            object_ids=_ids_of(coverage, KnowledgeFacet.REACTION)))
    elif evidence_state is KnowledgeState.UNKNOWN:
        gaps.append(KnowledgeGap(
            facet=KnowledgeFacet.EXPERIMENTAL_EVIDENCE,
            reason="没有任何实验或引用证据。",
            suggested_action="inspect local document; extract reaction evidence"))
    elif evidence_state is KnowledgeState.PARTIALLY_KNOWN:
        gaps.append(KnowledgeGap(
            facet=KnowledgeFacet.EXPERIMENTAL_EVIDENCE,
            reason="反应有引用证据，但没有实验级支持。",
            suggested_action="inspect local document; extract reaction evidence"))

    if state(KnowledgeFacet.ROUTE) is KnowledgeState.UNKNOWN:
        gaps.append(KnowledgeGap(
            facet=KnowledgeFacet.ROUTE,
            reason="尚无候选合成路线记录。",
            suggested_action="retrosynthesize"))

    route_evidence_state = state(KnowledgeFacet.ROUTE_EVIDENCE)
    if route_evidence_state is KnowledgeState.UNKNOWN:
        if state(KnowledgeFacet.ROUTE) is not KnowledgeState.UNKNOWN:
            gaps.append(KnowledgeGap(
                facet=KnowledgeFacet.ROUTE_EVIDENCE,
                reason="路线步骤与文献反应之间还没有已测量的链接。",
                suggested_action="validate routes against literature reactions"))
    elif route_evidence_state is KnowledgeState.PARTIALLY_KNOWN:
        gaps.append(KnowledgeGap(
            facet=KnowledgeFacet.ROUTE_EVIDENCE,
            reason="路线链接只有筛选级（screening only），没有精确的文献支持。",
            suggested_action="validate routes against literature reactions"))

    return gaps


# ---------------------------------------------------------------------------
# plan (§17/§18)
# ---------------------------------------------------------------------------


def build_research_plan(coverage: KnowledgeCoverage,
                        gaps: list[KnowledgeGap], *,
                        question: str = "", has_document: bool = False,
                        wants_latest: bool | None = None) -> ResearchPlan:
    """What this run should do -- derived from the gaps, never invented.

    The rules, in order of how often they matter:

    * A gap in papers/reactions/experimental evidence means literature and
      document work; when the knowledge covers those facets and the user
      did not ask for the latest literature, **no search step is planned**
      (§39) -- existing knowledge is reused instead of re-searched.
    * A user-supplied document is always new material: it is always
      inspected and extracted, whatever the coverage says.
    * A route gap means retrosynthesis; a route-evidence gap means
      validating against literature reactions.
    * Synthesis runs whenever reactions exist or are planned; reporting
      always runs.

    With no knowledge base the coverage is all-UNKNOWN, every gap is
    open, and the plan is the full pipeline -- exactly the behavior that
    existed before this phase.
    """
    if wants_latest is None:
        wants_latest = wants_latest_literature(question)
    facets = {g.facet for g in gaps}
    steps: list[ResearchPlanStep] = [
        ResearchPlanStep(action=ResearchPlanAction.ASSESS_KNOWLEDGE,
                         reason="先读知识库：已有知识决定本次研究补什么（§7）",
                         tool="get_existing_knowledge"),
    ]

    need_search = (wants_latest
                   or KnowledgeFacet.PAPER in facets
                   or KnowledgeFacet.REACTION in facets
                   or KnowledgeFacet.EXPERIMENTAL_EVIDENCE in facets)
    if need_search:
        if wants_latest:
            reason = "用户要求最新文献（§40）；知识库不假装自己是当前事实"
        else:
            reason = _reason_for(gaps, (KnowledgeFacet.PAPER,
                                        KnowledgeFacet.REACTION,
                                        KnowledgeFacet.EXPERIMENTAL_EVIDENCE))
        steps.append(ResearchPlanStep(
            action=ResearchPlanAction.SEARCH_LITERATURE, reason=reason,
            gap_facet=_facet_for(gaps, (KnowledgeFacet.PAPER,
                                        KnowledgeFacet.REACTION,
                                        KnowledgeFacet.EXPERIMENTAL_EVIDENCE)),
            tool="search_literature"))

    if has_document or KnowledgeFacet.EXPERIMENTAL_EVIDENCE in facets:
        reason = ("用户提供了本地文档：新材料总是被检查（§15）"
                  if has_document else
                  _reason_for(gaps, (KnowledgeFacet.EXPERIMENTAL_EVIDENCE,)))
        steps.append(ResearchPlanStep(
            action=ResearchPlanAction.INSPECT_DOCUMENT, reason=reason,
            gap_facet=KnowledgeFacet.EXPERIMENTAL_EVIDENCE,
            tool="get_document_evidence"))
        steps.append(ResearchPlanStep(
            action=ResearchPlanAction.EXTRACT_REACTION_EVIDENCE,
            reason=reason,
            gap_facet=KnowledgeFacet.EXPERIMENTAL_EVIDENCE,
            tool="extract_reactions"))

    if KnowledgeFacet.ROUTE in facets:
        steps.append(ResearchPlanStep(
            action=ResearchPlanAction.RETROSYNTHESIZE,
            reason=_reason_for(gaps, (KnowledgeFacet.ROUTE,)),
            gap_facet=KnowledgeFacet.ROUTE, tool="retrosynthesize"))
    if KnowledgeFacet.ROUTE_EVIDENCE in facets:
        steps.append(ResearchPlanStep(
            action=ResearchPlanAction.VALIDATE_ROUTES,
            reason=_reason_for(gaps, (KnowledgeFacet.ROUTE_EVIDENCE,)),
            gap_facet=KnowledgeFacet.ROUTE_EVIDENCE,
            tool="validate_route_step"))

    steps.append(ResearchPlanStep(
        action=ResearchPlanAction.SYNTHESIZE_EVIDENCE,
        reason="把各来源的实验条件并排列出，冲突保留出处（§22-§24）",
        tool="synthesize_evidence"))
    steps.append(ResearchPlanStep(
        action=ResearchPlanAction.REPORT,
        reason="报告区分已有知识、本次新增、冲突与缺口（§100）"))

    return ResearchPlan(goal=question, coverage=coverage, gaps=list(gaps),
                        steps=steps, wants_latest=wants_latest)


def _ids_of(coverage: KnowledgeCoverage, facet: KnowledgeFacet) -> list[str]:
    for item in coverage.facets:
        if item.facet is facet:
            return list(item.object_ids)
    return []


def _reason_for(gaps: list[KnowledgeGap], facets: tuple) -> str:
    for gap in gaps:
        if gap.facet in facets:
            return gap.reason
    return "知识已覆盖该方面；继续既有流程"


def _facet_for(gaps: list[KnowledgeGap], facets: tuple):
    for gap in gaps:
        if gap.facet in facets:
            return gap.facet
    return None


# ---------------------------------------------------------------------------
# evidence synthesis (§19-§24)
# ---------------------------------------------------------------------------


def synthesize_fields(kb=None, *, entity_id: str | None = None,
                      reaction_ids: list[str] | None = None,
                      reactions=None,
                      records_by_reaction: dict | None = None
                      ) -> EvidenceSynthesis:
    """The condition values recorded for these reactions, side by side.

    Every observation points at its own evidence, paper and record (§24).
    Where sources state different values for one field, the difference is
    a `FieldConflict` -- both values kept, nothing averaged, nothing
    chosen (§22/§23). This aggregation invents no value: every entry here
    was stated in a record and is re-stated with its citation (§6).

    `reactions` / `records_by_reaction` let a caller synthesize over
    objects the knowledge base has not stored yet -- the run being
    reported right now -- without pretending they are established.
    """
    resolved: list = []
    if reactions is not None:
        resolved = list(reactions)
    elif reaction_ids and kb is not None:
        for reaction_id in reaction_ids:
            reaction = kb.store.get_reaction(reaction_id)
            if reaction is not None:
                resolved.append(reaction)
    elif entity_id and kb is not None:
        resolved = kb.search_reactions(entity_id=entity_id)
    else:
        from crea.repositories.errors import RepositoryError

        raise RepositoryError(
            "synthesize_fields needs reactions, reaction_ids or entity_id")

    observations: list[FieldObservation] = []
    for reaction in resolved:
        if records_by_reaction is not None and (
                reaction.reaction_id in records_by_reaction):
            records = [records_by_reaction[reaction.reaction_id]]
        elif kb is not None:
            records = kb.get_reaction_records(reaction.reaction_id)
        else:
            records = []
        for record in records:
            observations.extend(_observations_of(reaction, record))
        # a reaction with no stored record contributes nothing: no record,
        # no stated conditions (§27 -- never guessed)

    by_field: dict[str, list[FieldObservation]] = {}
    for observation in observations:
        by_field.setdefault(observation.field, []).append(observation)

    conflicts: list[FieldConflict] = []
    for field, items in by_field.items():
        stated = {(str(o.value), o.unit) for o in items}
        if len(stated) > 1:
            conflicts.append(FieldConflict(field=field, observations=items))

    return EvidenceSynthesis(
        entity_id=entity_id,
        reaction_ids=[r.reaction_id for r in resolved],
        observations=observations,
        conflicts=conflicts)


def _observations_of(reaction, record) -> list[FieldObservation]:
    out: list[FieldObservation] = []
    conditions = record.conditions

    def observation(field, *, value, raw_value, unit, raw_unit, refs):
        out.append(FieldObservation(
            field=field, value=value, raw_value=raw_value, unit=unit,
            raw_unit=raw_unit,
            evidence_id=_first_resolvable(refs),
            paper_id=reaction.paper_id or "",
            doi=reaction.doi or "",
            source_id=reaction.source_id or "",
            reaction_id=reaction.reaction_id,
            record_id=record.reaction_id))

    for field in _QUANTITY_FIELDS:
        quantity = getattr(conditions, field)
        if quantity.stated:
            observation(field, value=quantity.value,
                        raw_value=quantity.raw_value,
                        unit=quantity.unit, raw_unit=quantity.raw_unit,
                        refs=quantity.evidence_refs)
    for field in _TEXT_FIELDS:
        text = getattr(conditions, field)
        refs = getattr(conditions, f"{field}_evidence_refs")
        if text:
            observation(field, value=text, raw_value=text, unit="",
                        raw_unit="", refs=list(refs or []))
    return out


def _first_resolvable(refs) -> str:
    return refs[0] if refs else ""


__all__ = [
    "assess_coverage",
    "build_research_plan",
    "find_gaps",
    "synthesize_fields",
    "wants_latest_literature",
]
