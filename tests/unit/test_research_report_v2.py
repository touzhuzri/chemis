"""ResearchReport v2 -- the report layer organising what a run produced.

§5 of Phase 3.7, and only the report layer: these tests build model
instances and assert on rendered Markdown. No RDKit, no EvidenceRegistry,
no RouteValidation, no Identity service, no agent loop -- those are tested
wherever they live, and re-testing them here would only couple this file to
their internals.

What is under test is the organisation and the honesty of the output:
identity resolved or explicitly unresolved, computed values labelled
computed, discovery kept apart from evidence-backed literature, experimental
parameters tabled with their evidence, candidate steps saying "Candidate
only", provenance kept on every important finding, limitations generated
from what is actually missing, and a status that is never upgraded by the
renderer.
"""

from __future__ import annotations

from crea.core.models.evidence import Evidence, EvidenceStrength, EvidenceType, Locator
from crea.core.models.identity import (
    ChemicalIdentityCandidate,
    ChemicalIdentityResolution,
    IdentityBasis,
    StructureInput,
    StructureResolutionStatus,
)
from crea.core.models.reaction import (
    ComponentRole,
    FieldStatus,
    Quantity,
    ReactionConditions,
    ReactionRecord,
    YieldType,
)
from crea.core.models.reaction_knowledge import (
    Reaction,
    ReactionComponent,
    ReactionStatus,
    RouteStepReactionLink,
)
from crea.core.models.reaction_linking import (
    LinkBasis,
    CandidateRouteStepEvidence,
    ReactionEvidenceLevel,
    ReactionMatchType,
    RouteEvidenceReport,
)
from crea.core.models.research import (
    Finding,
    ResearchReport,
    ResearchStep,
    ResearchTask,
    SourceRef,
    TaskStatus,
    ToolStatus,
)
from crea.core.models.route import (
    CandidateRoute,
    ReactionStep,
    RouteMolecule,
    StepStatus,
    SupportLevel,
    TransformationCandidate,
)
from crea.core.models.session import ResearchSession, WorkflowStatus
from crea.report.research import render_research_report

DIYNE = "c1ccccc1C#CC#Cc2ccccc2"
QUESTION = "分析 1,4-diphenyl-1,3-diyne 的性质、文献、合成方法和实验依据"


# ---------------------------------------------------------------------------
# builders (plain models; nothing computed here)
# ---------------------------------------------------------------------------


def _evidence(eid="ev-1", *, etype=EvidenceType.EXPERIMENTAL,
              strength=EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE,
              source="user_provided", source_id="doc-1",
              quote="The reaction was stirred at 80 C for 12 hours.",
              locator=None, paper_id=None, doi=None) -> Evidence:
    return Evidence(
        evidence_id=eid,
        evidence_type=etype,
        strength=strength,
        source=source,
        source_id=source_id,
        retrieved_at="2026-10-02T00:00:00+00:00",
        raw_quote=quote,
        locator=locator or Locator(locator_type="document_region",
                                   document_id="doc-1", section="Experimental",
                                   page_start=1),
        paper_id=paper_id,
        doi=doi,
    )


def _quantity(value=80, unit="C", *, status=FieldStatus.EXPLICIT,
              refs=None, raw_value=None) -> Quantity:
    return Quantity(value=value, raw_value=raw_value if raw_value is not None
                    else str(value),
                    unit=unit, raw_unit=unit, status=status,
                    evidence_refs=list(refs or []))


def _record(*, temperature=None, pressure=None, yield_value=None,
            reaction_id="rxn-1", refs=None) -> ReactionRecord:
    conditions = ReactionConditions(
        temperature=temperature or Quantity(),
        pressure=pressure or Quantity(),
        yield_value=yield_value or Quantity(),
    )
    return ReactionRecord(reaction_id=reaction_id, document_id="doc-1",
                          conditions=conditions,
                          evidence_refs=list(refs or []))


def _step(step_id="s-1", index=1, *, status=StepStatus.CANDIDATE,
          support=SupportLevel.NO_SUPPORT, refs=None, records=None,
          papers=None) -> ReactionStep:
    return ReactionStep(
        step_id=step_id, step_index=index,
        reactants=[RouteMolecule(molecule_id="r-1", canonical_smiles="A")],
        products=[RouteMolecule(molecule_id="p-1", canonical_smiles="B")],
        transformation=TransformationCandidate(
            transformation_class="Suzuki coupling",
            transformation_class_source="provider"),
        status=status, support_level=support,
        evidence_refs=list(refs or []),
        reaction_record_ids=list(records or []),
        paper_ids=list(papers or []),
    )


def _route(steps, route_id="rt-1") -> CandidateRoute:
    return CandidateRoute(route_id=route_id,
                          target=RouteMolecule(molecule_id="t-1",
                                               canonical_smiles=DIYNE),
                          steps=steps, source="fake-retrosynthesis")


def _identity(resolved=True):
    candidate = ChemicalIdentityCandidate(
        candidate_id="c-1", source="PUBCHEM", source_id="12345",
        candidate_name="1,4-diphenylbuta-1,3-diyne",
        canonical_smiles=DIYNE,
        inchi="InChI=1S/C16H10/c1-3-5-13-7-9-15(10-8-13)12-11-16-6-4-2-14-16/h1-10H",
        inchikey="DQCUYHFFLPKQKM-UHFFFAOYSA-N",
        identity_basis=IdentityBasis.PROVIDER_IDENTITY,
    )
    if not resolved:
        return ChemicalIdentityResolution(
            status=StructureResolutionStatus.AMBIGUOUS,
            input=StructureInput.of("NAME", "vitamin C"),
            candidates=[candidate],
            reason="found 3 distinct structures for this input",
        )
    return ChemicalIdentityResolution(
        status=StructureResolutionStatus.RESOLVED,
        input=StructureInput.of("NAME", "1,4-diphenyl-1,3-diyne"),
        candidates=[candidate], selected=candidate,
        selected_basis=IdentityBasis.PROVIDER_IDENTITY,
        reason="exactly one structure matched",
    )


def _report(**kwargs) -> ResearchReport:
    defaults = dict(question=QUESTION, status=TaskStatus.COMPLETED)
    defaults.update(kwargs)
    return ResearchReport(**defaults)


def _session(report=None, *, status=WorkflowStatus.COMPLETED, steps=None):
    task = ResearchTask(task_id="t-1", question=QUESTION,
                        status=report.status if report else TaskStatus.COMPLETED)
    return ResearchSession(session_id="ses-1", task=task,
                           steps=list(steps or []), status=status)


# ---------------------------------------------------------------------------
# 1-2. identity
# ---------------------------------------------------------------------------


def test_identity_resolved_shows_input_name_structure_and_source():
    text = render_research_report(_report(), identity=_identity())

    assert "## 2. Chemical Identity" in text
    assert "name `1,4-diphenyl-1,3-diyne`" in text
    assert "resolved name: 1,4-diphenylbuta-1,3-diyne" in text
    assert f"SMILES: `{DIYNE}`" in text
    assert "InChI: `InChI=1S/C16H10" in text
    assert "InChIKey: `DQCUYHFFLPKQKM-UHFFFAOYSA-N`" in text
    assert "identity source: PUBCHEM 12345" in text
    assert "resolution status: resolved" in text
    assert "Identity unresolved" not in text


def test_identity_unresolved_is_stated_and_never_guessed():
    text = render_research_report(_report(), identity=_identity(resolved=False))

    assert "Identity unresolved" in text
    assert "resolution status: ambiguous" in text
    assert "candidates: 1（无一被选中）" in text
    # no structure is asserted anywhere in the identity section
    identity_section = text.split("## 2. Chemical Identity")[1].split("## 3.")[0]
    assert "SMILES:" not in identity_section


def test_without_any_identity_input_the_report_says_unresolved():
    text = render_research_report(_report())
    assert "Identity unresolved" in text
    assert "不猜测结构" in text


# ---------------------------------------------------------------------------
# 3. computed findings
# ---------------------------------------------------------------------------


def test_computed_findings_are_labelled_computed_not_evidence():
    report = _report(findings=[
        Finding(text="分子式 C16H10；分子量 202.26", kind="computed"),
        Finding(text="文献报道收率 82%", kind="evidence_backed",
                evidence_refs=["ev-1"]),
    ])
    text = render_research_report(report)

    analysis = text.split("## 3. Molecular Analysis")[1].split("## 4.")[0]
    assert "[Computed] 分子式 C16H10" in analysis
    # the computed finding does not leak into literature or provenance-as-fact
    assert analysis.count("[Computed]") == 1
    assert "以下为计算结果，不是文献证据" in analysis
    assert "文献报道收率 82%" not in analysis


# ---------------------------------------------------------------------------
# 4-5. literature discovery vs evidence-backed
# ---------------------------------------------------------------------------


def test_literature_discovery_lists_papers_as_discovery_only():
    report = _report(sources=[
        SourceRef(kind="paper", identifier="W1",
                  label="Synthesis of diarylbutadiynes",
                  doi="10.0000/synthetic.2026.0001",
                  url="https://doi.org/10.0000/synthetic.2026.0001"),
    ])
    text = render_research_report(report)

    lit = text.split("## 4. Literature")[1].split("## 5.")[0]
    assert "### Literature Discovery" in lit
    assert "[Discovery] Synthesis of diarylbutadiynes" in lit
    assert "DOI: 10.0000/synthetic.2026.0001" in lit
    assert "source: W1" in lit
    assert "仅为 Discovery" in lit
    assert "### Evidence-backed Literature" in lit
    assert "(none)" in lit  # a found paper with no evidence is not evidence


def test_evidence_backed_literature_requires_real_evidence():
    report = _report(
        sources=[SourceRef(kind="paper", identifier="W1", label="A paper")],
        evidence=[_evidence("ev-lit", etype=EvidenceType.ABSTRACT,
                            strength=EvidenceStrength.DISCOVERY_EVIDENCE,
                            source="openalex", source_id="W1",
                            quote="a model system for a symmetric diyne",
                            locator=Locator(locator_type="abstract",
                                            field="abstract"),
                            paper_id="W1", doi="10.0000/synthetic.2026.0001")],
    )
    text = render_research_report(report)

    lit = text.split("## 4. Literature")[1].split("## 5.")[0]
    backed = lit.split("### Evidence-backed Literature")[1]
    assert "[Evidence-backed] 10.0000/synthetic.2026.0001" in backed
    assert "Evidence ID: `ev-lit`" in backed
    assert "strength: discovery_evidence" in backed
    assert "Source: openalex / Source ID: W1" in backed
    assert "Quote:" in backed
    # metadata stays discovery: it may not be sold as experimental support
    assert "不能升级为实验支持" in backed


# ---------------------------------------------------------------------------
# 6-7. experimental evidence table
# ---------------------------------------------------------------------------


def test_experimental_evidence_tables_parameters_with_their_evidence():
    ev = _evidence("ev-1", quote="The reaction was stirred at 80 C for 12 hours.")
    report = _report(
        evidence=[ev],
        reaction_details=[_record(
            temperature=_quantity(80, "C", refs=["ev-1"]),
            yield_value=_quantity(82, "%", refs=["ev-1"],
                                  status=FieldStatus.EXPLICIT),
            refs=["ev-1"],
        )],
    )
    text = render_research_report(report)

    exp = text.split("## 5. Experimental Evidence")[1].split("## 6.")[0]
    assert "| Parameter | Value | Evidence |" in exp
    assert "| Temperature | 80 C | page 1 Experimental |" in exp
    assert "| Yield | 82 %" in exp


def test_a_not_found_parameter_is_reported_as_not_stated():
    report = _report(reaction_details=[_record(
        temperature=_quantity(80, "C", refs=["ev-1"]),
        pressure=Quantity(status=FieldStatus.NOT_FOUND),
    )])
    text = render_research_report(report)

    exp = text.split("## 5. Experimental Evidence")[1].split("## 6.")[0]
    assert "| Temperature | 80 C |" in exp
    assert "| Pressure | not stated |" in exp
    # the absent parameter is never filled with a plausible default
    assert "1 atm" not in text


def test_metadata_evidence_never_appears_in_the_experimental_table():
    meta = _evidence("ev-m", etype=EvidenceType.METADATA,
                     strength=EvidenceStrength.SECONDARY_METADATA_EVIDENCE,
                     source="crossref", source_id="W1", quote="a title")
    report = _report(
        evidence=[meta],
        reaction_details=[_record(refs=["ev-m"])],
    )
    text = render_research_report(report)

    exp = text.split("## 5. Experimental Evidence")[1].split("## 6.")[0]
    # the table shows where each value came from; a metadata ref resolves to
    # its locator, and the value rows carry no invented numbers
    assert "| Temperature | not stated |" in exp


# ---------------------------------------------------------------------------
# 8-10. routes
# ---------------------------------------------------------------------------


def test_a_candidate_route_step_says_candidate_only():
    report = _report(routes=[_route([_step("s-1", 1)])])
    text = render_research_report(report)

    retro = text.split("## 6. Retrosynthesis")[1].split("## 7.")[0]
    assert "### Route 1 (`rt-1`)" in retro
    assert "#### Step 1" in retro
    assert "Reaction: A → B（Suzuki coupling）" in retro
    assert "Evidence: Candidate only" in retro
    # nothing in a candidate step claims a support level above candidate
    assert "Status: literature_supported" not in retro
    assert "Status: experimentally_supported" not in retro


def test_a_literature_supported_route_is_not_called_candidate_only():
    report = _report(
        routes=[_route([_step("s-1", 1, status=StepStatus.LITERATURE_SUPPORTED,
                              support=SupportLevel.LITERATURE_SUPPORT,
                              refs=["ev-1"], papers=["W1"])])],
        evidence=[_evidence("ev-1", etype=EvidenceType.FULLTEXT)],
    )
    text = render_research_report(report)

    retro = text.split("## 6. Retrosynthesis")[1].split("## 7.")[0]
    assert "Evidence: Candidate only" not in retro
    assert "Status: literature_supported" in retro
    assert "Support: literature_support" in retro
    assert "Evidence: `ev-1`" in retro
    assert "Papers: W1" in retro


def test_an_experimentally_supported_route_shows_its_record():
    chain_step = CandidateRouteStepEvidence(
        step_id="s-1", step_index=1,
        candidate_status=StepStatus.EXPERIMENTALLY_SUPPORTED,
        evidence_level=ReactionEvidenceLevel.USER_DOCUMENT_EXPERIMENTAL_SUPPORT,
        match_type=ReactionMatchType.EXACT,
        experimental_support=["rxn-1"], evidence_refs=["ev-1"],
    )
    report = _report(
        routes=[_route([_step("s-1", 1,
                              status=StepStatus.EXPERIMENTALLY_SUPPORTED,
                              support=SupportLevel.USER_DOCUMENT_SUPPORT,
                              refs=["ev-1"], records=["rxn-1"])])],
        route_evidence=[RouteEvidenceReport(route_id="rt-1", steps=[chain_step])],
        evidence=[_evidence("ev-1")],
    )
    text = render_research_report(report)

    retro = text.split("## 6. Retrosynthesis")[1].split("## 7.")[0]
    assert "Status: experimentally_supported" in retro
    assert "Reaction records: `rxn-1`" in retro
    assert "Evidence level: user_document_experimental_support" in retro
    assert "Evidence: Candidate only" not in retro


# ---------------------------------------------------------------------------
# 11. provenance
# ---------------------------------------------------------------------------


def test_important_findings_keep_their_full_provenance():
    report = _report(findings=[
        Finding(text="文献报道收率 82%", kind="evidence_backed",
                evidence_refs=["ev-1"]),
    ], evidence=[_evidence("ev-1")])
    text = render_research_report(report)

    prov = text.split("## 7. Evidence and Provenance")[1].split("## 8.")[0]
    assert "Finding [evidence_backed]: 文献报道收率 82%" in prov
    assert "Evidence ID: `ev-1`" in prov
    assert "Source: user_provided / Source ID: doc-1" in prov
    assert "Locator: page 1 Experimental" in prov
    assert "Quote:" in prov


def test_a_finding_with_no_evidence_says_so_in_provenance():
    report = _report(findings=[
        Finding(text="该化合物可能容易聚合", kind="insufficient"),
    ])
    text = render_research_report(report)

    prov = text.split("## 7. Evidence and Provenance")[1].split("## 8.")[0]
    assert "Evidence: none" in prov
    assert "不应作为事实使用" in prov


def test_an_unresolved_evidence_id_is_reported_not_dropped():
    report = _report(findings=[
        Finding(text="x", kind="evidence_backed", evidence_refs=["ev-gone"]),
    ])
    text = render_research_report(report)
    assert "Evidence ID: `ev-gone`（unresolved）" in text


# ---------------------------------------------------------------------------
# 12-13. status
# ---------------------------------------------------------------------------


def test_partial_report_says_partial_and_where_it_stopped_keeps_results():
    report = _report(status=TaskStatus.PARTIAL,
                     findings=[Finding(text="分子式 C16H10", kind="computed")])
    session = _session(report, status=WorkflowStatus.PARTIAL,
                       steps=[ResearchStep(step_id="t-1-s0", index=0,
                                           tool="analyze_molecule",
                                           result_status=ToolStatus.OK)])
    text = render_research_report(report, session=session)

    assert text.startswith("# Research Report")
    assert "Status: PARTIAL" in text
    assert "Stopped at: step 0 (analyze_molecule)" in text
    # results that were produced are kept
    assert "[Computed] 分子式 C16H10" in text
    assert "workflow PARTIAL" in text


def test_completed_report_only_when_the_workflow_said_completed():
    report = _report(status=TaskStatus.COMPLETED)
    session = _session(report, status=WorkflowStatus.COMPLETED)
    text = render_research_report(report, session=session)
    assert "Status: COMPLETED" in text
    assert "Stopped at:" not in text

    # the renderer never upgrades: report COMPLETED + session PARTIAL -> PARTIAL
    partial_session = _session(report, status=WorkflowStatus.PARTIAL)
    text = render_research_report(report, session=partial_session)
    assert "Status: PARTIAL" in text
    assert "Status: COMPLETED" not in text

    # and it never upgrades a partial task to COMPLETED either
    partial_report = _report(status=TaskStatus.PARTIAL)
    done_session = _session(partial_report, status=WorkflowStatus.COMPLETED)
    text = render_research_report(partial_report, session=done_session)
    assert "Status: PARTIAL" in text
    assert "Status: COMPLETED" not in text


# ---------------------------------------------------------------------------
# 14. limitations
# ---------------------------------------------------------------------------


def test_limitations_are_generated_from_what_is_actually_missing():
    report = _report(status=TaskStatus.PARTIAL,
                     sources=[SourceRef(kind="paper", identifier="W1",
                                        label="A paper found by search")],
                     unresolved_items=["LLM 未完成：TimeoutError，研究在中途终止。"])
    session = _session(report, status=WorkflowStatus.PARTIAL)
    text = render_research_report(report, session=session)

    lim = text.split("## 8. Limitations")[1]
    for phrase in ("identity unresolved", "metadata only", "no full text",
                   "no experimental evidence", "retrosynthesis unavailable",
                   "LLM stopped early", "workflow PARTIAL"):
        assert phrase in lim, f"missing limitation: {phrase}"
    # a paper was found, so the gap is "metadata only", not "no literature"
    assert "no literature result" not in lim


def test_no_literature_result_is_reported_when_the_search_found_nothing():
    text = render_research_report(_report(status=TaskStatus.PARTIAL))
    lim = text.split("## 8. Limitations")[1]
    assert "no literature result" in lim


def test_limitations_name_candidate_routes_and_unvalidated_routes():
    report = _report(routes=[_route([_step("s-1", 1)])])
    text = render_research_report(report)

    lim = text.split("## 8. Limitations")[1]
    assert "retrosynthesis candidate only" in lim
    assert "route not experimentally validated" in lim


def test_limitations_do_not_invent_gaps_that_are_not_there():
    """A run with identity, full text and an experimentally validated route
    must not carry the corresponding limitation lines."""
    chain_step = CandidateRouteStepEvidence(
        step_id="s-1", step_index=1,
        candidate_status=StepStatus.EXPERIMENTALLY_SUPPORTED,
        evidence_level=ReactionEvidenceLevel.EXPERIMENTAL_REACTION_SUPPORT,
        match_type=ReactionMatchType.EXACT, experimental_support=["rxn-1"],
    )
    report = _report(
        status=TaskStatus.COMPLETED,
        evidence=[_evidence("ev-1", etype=EvidenceType.FULLTEXT)],
        reaction_details=[_record(refs=["ev-1"])],
        routes=[_route([_step("s-1", 1,
                              status=StepStatus.EXPERIMENTALLY_SUPPORTED,
                              support=SupportLevel.EXPERIMENTAL_SUPPORT,
                              refs=["ev-1"], records=["rxn-1"])])],
        route_evidence=[RouteEvidenceReport(route_id="rt-1", steps=[chain_step])],
        sources=[SourceRef(kind="paper", identifier="W1")],
        findings=[Finding(text="x", kind="evidence_backed",
                          evidence_refs=["ev-1"])],
    )
    # give the literature an evidence-backed entry too, so "metadata only" is false
    report.evidence.append(_evidence("ev-lit", etype=EvidenceType.FULLTEXT,
                                     strength=EvidenceStrength.PRIMARY_DOCUMENT_EVIDENCE,
                                     source="openalex", source_id="W1"))
    # and the reaction knowledge layer is complete too (Phase 3.10): an
    # established reaction, linked to the step by an exact match
    report.reactions.append(Reaction(
        reaction_id="RXN-1", status=ReactionStatus.EXPERIMENTALLY_SUPPORTED,
        reactants=[ReactionComponent(
            role=ComponentRole.REACTANT, name="phenylacetylene",
            entity_id="CHEM-00000001", identity_key="crea-identity-key-v1:x",
            canonical_smiles="C#Cc1ccccc1")],
        products=[ReactionComponent(
            role=ComponentRole.PRODUCT, name="the product",
            entity_id="CHEM-00000002", identity_key="crea-identity-key-v1:y",
            canonical_smiles=DIYNE)],
        paper_id="W1", evidence_refs=["ev-1"]))
    report.reaction_links.append(RouteStepReactionLink(
        link_id="l-1", step_id="s-1", reaction_id="RXN-1",
        match_type=ReactionMatchType.EXACT,
        link_basis=LinkBasis.EXACT_STRUCTURE, screening_only=False))
    session = _session(report, status=WorkflowStatus.COMPLETED)
    text = render_research_report(report, identity=_identity(),
                                  session=session)

    lim = text.split("## 8. Limitations")[1]
    for phrase in ("identity unresolved", "no literature result",
                   "metadata only", "no full text", "no experimental evidence",
                   "retrosynthesis unavailable", "retrosynthesis candidate only",
                   "route not experimentally validated", "LLM stopped early",
                   "workflow PARTIAL", "no reactions established",
                   "reaction partially resolved",
                   "no reaction is experimentally supported",
                   "no route step is linked"):
        assert phrase not in lim, f"spurious limitation: {phrase}"
    assert "(none identified)" in lim


# ---------------------------------------------------------------------------
# format guardrails
# ---------------------------------------------------------------------------


def test_the_report_carries_no_ranking_or_confidence_language():
    chain_step = CandidateRouteStepEvidence(
        step_id="s-1", step_index=1,
        candidate_status=StepStatus.LITERATURE_SUPPORTED,
        evidence_level=ReactionEvidenceLevel.LITERATURE_REACTION_SUPPORT,
        match_type=ReactionMatchType.EXACT,
    )
    report = _report(
        routes=[_route([_step("s-1", 1, status=StepStatus.LITERATURE_SUPPORTED,
                              support=SupportLevel.LITERATURE_SUPPORT,
                              refs=["ev-1"]), _step("s-2", 2)])],
        route_evidence=[RouteEvidenceReport(route_id="rt-1", steps=[chain_step])],
        evidence=[_evidence("ev-1")],
    )
    text = render_research_report(report, identity=_identity())

    lowered = text.lower()
    for forbidden in ("confidence", "probability", "success rate",
                      "recommended", "best route", "ranking"):
        assert forbidden not in lowered, f"forbidden language: {forbidden}"
    # describing two routes is fine; choosing between them is not
    assert "不排名、不推荐" in text
