"""ReactionRecord persistence: the experimental basis, kept as written.

Phase 3.12 §25-§28. `ReactionRecord` is the extraction result; `Reaction`
is the knowledge object. This is the chain between them: raw values stay
raw (no unit conversion), NOT_FOUND stays NOT_FOUND (no completion from
common sense), and `get_reaction_record(reaction_id)` answers "这个
Reaction 的实验依据来自哪个 ReactionRecord？"
"""

from __future__ import annotations

import pytest

from crea.core.models.reaction import (
    FieldStatus,
    Quantity,
    ReactionConditions,
    ReactionRecord,
)
from crea.core.models.reaction_knowledge import reaction_key
from crea.repositories.errors import ConstraintViolation
from crea.services.knowledge_base import KnowledgeBase
from tests.unit.test_repositories_sqlite import make_entity
from tests.unit.test_research_intelligence import _linked_reaction


@pytest.fixture()
def kb(tmp_path):
    base = KnowledgeBase(tmp_path / "crea.db")
    base.initialize()
    yield base
    base.close()


def _full_record() -> ReactionRecord:
    return ReactionRecord(
        reaction_id="rec-1",
        source="document",
        document_id="doc-1",
        reaction_description="coupling to the product",
        description_evidence_refs=["ev-1"],
        conditions=ReactionConditions(
            temperature=Quantity(value=80, raw_value="80", unit="C",
                                 raw_unit="C", status=FieldStatus.EXPLICIT,
                                 evidence_refs=["ev-1"],
                                 raw_text="stirred at 80 C"),
            time=Quantity(value=12, raw_value="12", unit="h", raw_unit="h",
                          status=FieldStatus.EXPLICIT),
            pressure=Quantity(),  # NOT_FOUND: never completed from sense
            yield_value=Quantity(value=82, raw_value="82", unit="%",
                                 raw_unit="%", status=FieldStatus.EXPLICIT),
            atmosphere="N2",
            atmosphere_evidence_refs=["ev-1"]),
        evidence_refs=["ev-1"],
        raw_experimental_text="The mixture was stirred at 80 C for 12 h.",
        extractor="scripted-1",
        policy_version="reaction-v1")


def test_a_record_round_trips_with_raw_values_and_absences(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_reaction(_linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"])))
    kb.save_reaction_record(_full_record(), reaction_id="RXN-1")
    kb.close()

    reopened = KnowledgeBase(kb.path)
    reopened.initialize()
    records = reopened.get_reaction_records("RXN-1")
    assert len(records) == 1
    record = records[0]
    assert record.reaction_id == "rec-1"
    temperature = record.conditions.temperature
    assert temperature.value == 80
    assert temperature.raw_value == "80", "raw stays raw (§27)"
    assert temperature.raw_unit == "C"
    assert temperature.evidence_refs == ["ev-1"]
    assert record.conditions.pressure.status is FieldStatus.NOT_FOUND, (
        "没有 pressure 就保持 NOT_FOUND，禁止常识补全（§27）")
    assert record.conditions.atmosphere == "N2"
    assert record.extractor == "scripted-1"
    reopened.close()


def test_the_reaction_record_chain_answers_the_provenance_question(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    reaction = _linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"]))
    reaction.record_id = "rec-1"
    kb.save_reaction(reaction)
    kb.save_reaction_record(_full_record(), reaction_id="RXN-1")

    # 这个 Reaction 的实验依据来自哪个 ReactionRecord？
    records = kb.get_reaction_records("RXN-1")
    assert [r.reaction_id for r in records] == ["rec-1"]
    assert kb.get_reaction("RXN-1").record_id == "rec-1"


def test_a_record_never_lands_without_its_reaction(kb):
    with pytest.raises(ConstraintViolation):
        kb.save_reaction_record(_full_record(), reaction_id="RXN-nope")


def test_no_unit_conversion_happens_anywhere(kb):
    entity = make_entity(1)
    kb.save_entity(entity)
    kb.save_reaction(_linked_reaction(
        "RXN-1", entity, key=reaction_key(["e:r"], ["e:p"])))
    record = _full_record()
    record.conditions.temperature = Quantity(
        value=None, raw_value="reflux", unit="UNKNOWN", raw_unit="reflux",
        status=FieldStatus.EXPLICIT)
    kb.save_reaction_record(record, reaction_id="RXN-1")
    loaded = kb.get_reaction_records("RXN-1")[0]
    assert loaded.conditions.temperature.raw_value == "reflux"
    assert loaded.conditions.temperature.value is None
