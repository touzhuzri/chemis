"""The offline demo: the whole research chain, no key, no network.

Phase 4 (§78/§79). `python -m crea.cli demo` runs one complete research
task over a synthetic procedure PDF with a scripted extraction model, and
shows what the workbench shows: identity, knowledge, reactions,
evidence, routes, and the report.

Everything it produces is **SYNTHETIC**: fixture literature, a synthetic
compound lookup, a scripted extraction. The demo labels it as such at
every level -- the report's sources say "(synthetic record)", the
summary says `origin = SYNTHETIC` -- so nobody mistakes demo output for
real research. The value is that the *chain* is real: the same
ResearchWorkflow, the same registries, the same knowledge base, the same
Report v3.
"""

from __future__ import annotations

import json

from crea.core.ids import content_hash

#: The synthetic target: 1,4-diphenyl-1,3-diyne.
DEMO_SMILES = "c1ccccc1C#CC#Cc2ccccc2"
DEMO_NAME = "1,4-diphenyl-1,3-diyne"

#: The synthetic procedure. Every number here is invented for the demo
#: and every reader is told so (§79).
DEMO_PROCEDURE = [
    "Experimental (SYNTHETIC DEMO DATA)",
    "Phenylacetylene (1.0 mmol),",
    "CuI (10 mol%),",
    "were dissolved in DMF (5 mL).",
    "The mixture was stirred at 80 C",
    "for 12 h.",
    "The product was isolated in 82% yield.",
    "Compound 8 (InChI=1S/C16H10/c1-3-9-15(10-4-1)13-7-8-14-16-11-5-2-6-12-16"
    "/h1-6,9-12H) was purified.",
]


class SyntheticCompoundProvider:
    """A one-entry compound lookup for the demo. SYNTHETIC, by construction.

    Implements the `CompoundProvider` protocol with hand-written candidate
    records -- nothing here is presented as an upstream response.
    """

    name = "synthetic-demo"
    requires_key: bool = False

    def __init__(self) -> None:
        from crea.core.models.identity import (
            ChemicalIdentityCandidate,
            IdentityBasis,
        )
        from crea.providers.base import Capability

        self.capabilities = frozenset({Capability.COMPOUND_LOOKUP})
        self._candidates = [ChemicalIdentityCandidate(
            candidate_id="synthetic-phenylacetylene",
            source="SYNTHETIC", source_id="SYNTHETIC",
            candidate_name="phenylacetylene",
            synonyms=["ethynylbenzene"],
            canonical_smiles="C#Cc1ccccc1",
            inchi="InChI=1S/C8H8/c1-2-8-6-4-3-5-7-8/h1,3-7H,2H2",
            inchikey="UQSBGCPXNLHSSA-UHFFFAOYSA-N",
            molecular_formula="C8H8", molecular_weight=102.13,
            identity_basis=IdentityBasis.UNKNOWN)]

    def status(self):
        from crea.providers.base import Availability, ProviderStatus

        return ProviderStatus(self.name, Availability.AVAILABLE)

    def lookup(self, query):
        name = (query.name or "").strip().lower()
        if name in ("phenylacetylene", "ethynylbenzene"):
            return list(self._candidates)
        return []


class ScriptedExtractionModel:
    """An extraction model that always answers with the demo proposal.

    A model may propose (§6): the proposal below is checked by the real
    policy against the real document text, exactly like any other.
    """

    name = "scripted-demo-extractor"
    model = "demo-1"

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def complete(self, request):
        from crea.llm.base import LLMResponse

        return LLMResponse(text=self._payload, provider=self.name,
                           model=self.model)


def demo_payload(evidence_id: str) -> str:
    """A valid extraction proposal for the demo procedure's own numbers."""
    return json.dumps({
        "description": "coupling to the product (SYNTHETIC DEMO DATA)",
        "description_evidence_refs": [evidence_id],
        "components": [
            {"name": "phenylacetylene", "value": "phenylacetylene",
             "component_role": "reactant", "unit": "", "status": "EXPLICIT",
             "evidence_refs": [evidence_id],
             "raw_text": "Phenylacetylene (1.0 mmol),"},
            {"name": "compound 8", "value": "compound 8",
             "component_role": "product", "unit": "", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "Compound 8"},
        ],
        "conditions": [
            {"name": "temperature", "value": 80, "raw_value": "80",
             "unit": "C", "raw_unit": "C", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "stirred at 80 C"},
            {"name": "time", "value": 12, "raw_value": "12", "unit": "h",
             "raw_unit": "h", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "for 12 h."},
            {"name": "yield_value", "value": 82, "raw_value": "82",
             "unit": "%", "raw_unit": "%", "status": "EXPLICIT",
             "evidence_refs": [evidence_id], "raw_text": "82% yield"},
        ],
        "yield_type": "unknown",
        "yield_type_evidence_refs": [],
        "raw_experimental_text": "",
    })


def run_demo(*, db_path=None, echo=print) -> dict:
    """One complete research run over synthetic fixtures. Returns facts.

    With `db_path` the knowledge persists there; without it the demo runs
    against a throwaway database and says so.
    """
    import tempfile
    from pathlib import Path

    from crea.agent.offline import build_offline_workflow_stack
    from crea.agent.workspace import DocumentLibrary
    from crea.core.models.identity import StructureInput
    from crea.core.models.research import ResearchTask
    from crea.report.research import render_research_report_v3
    from crea.services.documents import LocalDocumentService
    from crea.services.document_evidence import search_document
    from crea.services.knowledge_base import KnowledgeBase
    from crea.services.reaction_extraction import ReactionExtractor
    from crea.sources.pdf import PypdfDocumentParser

    workdir = Path(tempfile.mkdtemp(prefix="crea-demo-"))
    pdf_path = workdir / "synthetic-procedure.pdf"
    from crea.agent.demo_pdf import make_demo_pdf

    pdf_path.write_bytes(make_demo_pdf([DEMO_PROCEDURE]))

    # the evidence id is minted exactly as the tools mint it
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    parsed = library.ingest(pdf_path)
    evidence_id = search_document(parsed, "CuI")[0].evidence.evidence_id

    knowledge_base = KnowledgeBase(db_path if db_path is not None
                                   else workdir / "demo.crea.db")
    knowledge_base.initialize()
    workflow, library, sink, registry = build_offline_workflow_stack(
        extractor=ReactionExtractor(
            ScriptedExtractionModel(demo_payload(evidence_id))),
        compound_provider=SyntheticCompoundProvider(),
        knowledge_base=knowledge_base)
    parsed = library.ingest(pdf_path)

    task = ResearchTask(
        task_id=content_hash("demo|" + DEMO_SMILES, 16),
        question=f"（SYNTHETIC DEMO）研究 {DEMO_NAME} 的文献合成方法")
    report = workflow.run(
        task,
        structure_input=StructureInput.of("SMILES", DEMO_SMILES),
        document_id=parsed.document.document_id,
        document_query="CuI",
        paper_id="SYNTHETIC-PAPER-A",
    )
    markdown = render_research_report_v3(
        report,
        session=workflow.session,
        identity=getattr(getattr(registry, "identity", None),
                         "resolution", None))

    echo("=" * 72)
    echo("CREA DEMO — 全部演示数据 origin = SYNTHETIC（合成数据，非真实文献）")
    echo("=" * 72)
    echo("")
    echo(f"[SYNTHETIC] 目标结构: {DEMO_SMILES}")
    for entity_id in workflow.session.chemical_entity_ids:
        entity = knowledge_base.get_entity(entity_id)
        echo(f"[SYNTHETIC] Entity {entity.entity_id}: "
             f"{', '.join(entity.names) or entity.canonical_smiles}")
    for reaction_id in workflow.session.reaction_ids:
        reaction = knowledge_base.get_reaction(reaction_id)
        echo(f"[SYNTHETIC] Reaction {reaction.reaction_id}: "
             f"{reaction.status.value}")
        for record in knowledge_base.get_reaction_records(reaction_id):
            temperature = record.conditions.temperature
            yield_value = record.conditions.yield_value
            echo(f"[SYNTHETIC]   record {record.reaction_id}: "
                 f"{temperature.raw_value} {temperature.raw_unit}, "
                 f"{yield_value.raw_value} {yield_value.raw_unit}")
    echo(f"[SYNTHETIC] Evidence 条目: {len(workflow.session.evidence_ids)}")
    echo("")
    echo(markdown)
    summary = {
        "origin": "SYNTHETIC",
        "entity_ids": list(workflow.session.chemical_entity_ids),
        "reaction_ids": list(workflow.session.reaction_ids),
        "evidence_ids": list(workflow.session.evidence_ids),
        "session_id": workflow.session.session_id,
        "report_markdown": markdown,
    }
    knowledge_base.close()
    return summary


__all__ = ["DEMO_NAME", "DEMO_PROCEDURE", "DEMO_SMILES",
           "SyntheticCompoundProvider", "run_demo"]
