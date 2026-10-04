"""Knowledge integrity audit and storage hygiene.

Phase 3.12 §61/§87/§88. The audit does not repair anything -- what is
broken is said, with the objects named. And secrets never enter the
database: provenance params are caller-controlled free-form, so
credential-looking keys are redacted on the way in.
"""

from __future__ import annotations

import pytest

from crea.core.models.entity import ChemicalMention
from crea.core.models.reaction_knowledge import ReactionStatus, reaction_key
from crea.core.models.research import ResearchTask
from crea.core.models.session import ResearchSession, WorkflowStatus
from crea.core.provenance import Provenance
from crea.services.knowledge_base import KnowledgeBase
from crea.services.knowledge_integrity import KnowledgeIntegrityService
from tests.unit.test_repositories_sqlite import make_evidence, make_entity
from tests.unit.test_research_intelligence import _linked_reaction, _record


@pytest.fixture()
def kb(tmp_path):
    base = KnowledgeBase(tmp_path / "crea.db")
    base.initialize()
    yield base
    base.close()


def _clean_knowledge(kb):
    entity = make_entity(1)
    entity.provenance = Provenance(tool="test")
    evidence = make_evidence().model_copy(
        update={"provenance": Provenance(tool="test")})
    kb.save_entity(entity)
    kb.save_evidence(evidence)
    reaction = _linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"]),
        evidence_refs=[evidence.evidence_id])
    reaction.provenance = Provenance(tool="test")
    reaction.record_id = "rec-1"  # the record chain, §25-§28
    kb.save_reaction(reaction)
    kb.save_reaction_record(_record("rec-1"), reaction_id="RXN-1")
    kb.save_session(ResearchSession(
        session_id="session-1",
        task=ResearchTask(task_id="t", question="q"),
        status=WorkflowStatus.COMPLETED,
        chemical_entity_ids=[entity.entity_id],
        reaction_ids=["RXN-1"],
        evidence_ids=[evidence.evidence_id]))
    return entity, evidence, reaction


def test_a_clean_knowledge_base_audits_clean(kb):
    _clean_knowledge(kb)
    report = KnowledgeIntegrityService().audit(kb)
    assert report.ok, [i.model_dump() for i in report.issues]
    assert report.checked["reactions"] == 1
    assert report.checked["linked_records"] == 1


def test_a_reaction_record_chain_that_broke_is_reported_not_repaired(kb):
    entity, evidence, reaction = _clean_knowledge(kb)
    # break the chain behind the audit's back: remove the stored record
    kb.store._execute(
        "DELETE FROM reaction_records WHERE record_id = ?", ("rec-1",))
    report = KnowledgeIntegrityService().audit(kb)
    assert not report.ok
    codes = {i.code for i in report.issues}
    assert "REACTION_RECORD_MISSING" in codes
    # nothing was "fixed": the reaction still claims its record
    assert kb.get_reaction("RXN-1").record_id == "rec-1"


def test_supported_status_without_evidence_is_reported(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    reaction = _linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"]))
    # a status the evidence never earned -- exactly what §77 forbids
    reaction.status = ReactionStatus.EVIDENCE_BACKED
    kb.save_reaction(reaction)
    report = KnowledgeIntegrityService().audit(kb)
    assert "SUPPORTED_STATUS_WITHOUT_EVIDENCE" in {i.code for i in report.issues}


def test_a_mention_without_scope_is_reported(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_mention(ChemicalMention(
        mention_id="m-1", text="compound 7", source_id="",
        resolution_status="resolved", entity_id=entity.entity_id))
    report = KnowledgeIntegrityService().audit(kb)
    assert "MENTION_WITHOUT_SCOPE" in {i.code for i in report.issues}


# -- which image did this structure come from (§36) --------------------------


def test_an_entity_says_which_image_it_came_from(kb):
    from crea.core.models.identity import (
        ChemicalIdentityCandidate,
        ChemicalIdentityResolution,
        IdentityBasis,
        ImageProvenance,
        RecognitionStage,
        StructureInput,
        StructureInputKind,
        StructureResolutionStatus,
    )
    from crea.services.entity_registry import ChemicalEntityRegistry

    image = ImageProvenance(
        recognizer="decimer", recognizer_version="0.1",
        file_reference="figures/fig-1.png", raw_smiles="C#Cc1ccccc1",
        normalized_smiles="C#Cc1ccccc1", source="user_provided")
    inchi = "InChI=1S/C8H8/c1-2-8-6-4-3-5-7-8/h1,3-7H,2H2"
    candidate = ChemicalIdentityCandidate(
        candidate_id="c1", source="DECIMER", input_name=None,
        canonical_smiles="C#Cc1ccccc1", inchi=inchi,
        identity_basis=IdentityBasis.EXACT_STRUCTURE,
        recognition_stage=RecognitionStage.IMAGE_RECOGNIZED_CANDIDATE)
    resolution = ChemicalIdentityResolution(
        status=StructureResolutionStatus.RESOLVED,
        input=StructureInput(kind=StructureInputKind.IMAGE, value="",
                             image=image),
        candidates=[candidate], selected=candidate)

    registry = ChemicalEntityRegistry(kb.entity_registry()._repository)
    entity = registry.register_resolution(resolution)
    stored = kb.get_entity(entity.entity_id)
    assert stored.provenance is not None
    assert stored.provenance.params["image"]["file_reference"] == (
        "figures/fig-1.png")
    assert stored.provenance.params["image"]["recognizer"] == "decimer"


# -- secrets never persist (§87/§88) ----------------------------------------


def test_the_knowledge_base_redacts_credential_looking_provenance(kb):
    entity = make_entity(1)
    entity.provenance = Provenance(
        tool="test", source="pubchem",
        params={"Authorization": "Bearer sk-SECRET-TOKEN",
                "api_key": "abc123", "normal": "kept"})
    kb.save_entity(entity)
    stored = kb.get_entity(entity.entity_id)
    assert stored.provenance.params["Authorization"] == "[REDACTED]"
    assert stored.provenance.params["api_key"] == "[REDACTED]"
    assert stored.provenance.params["normal"] == "kept"
    assert "sk-SECRET-TOKEN" not in str(stored.provenance.params)

    exported = kb.export_dict()
    assert "sk-SECRET-TOKEN" not in repr(exported)
    assert "abc123" not in repr(exported)
