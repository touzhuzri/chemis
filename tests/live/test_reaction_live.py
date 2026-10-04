"""Live reaction extraction. Opt-in, one synthetic page, no real paper.

    CREA_LIVE_TESTS=1 CREA_LLM_API_KEY=... pytest -m live

The document is one of ours: a synthetic PDF with a complete procedure. What
this verifies is the part a scripted model cannot -- that a real model, given
the extraction prompt and the passages, produces a proposal our validation
accepts. Whether it extracts *well* is a different question, and a loose one;
what must hold is that nothing is invented.
"""

import os
import pathlib
import tempfile

import pytest

from crea.agent.reaction_prompt import EXTRACTION_SYSTEM
from crea.agent.workspace import DocumentLibrary
from crea.llm import LLMConfig, LLMRequest, OpenAICompatibleLLMProvider
from crea.services.document_evidence import search_document
from crea.services.documents import LocalDocumentService
from crea.services.reaction_extraction import ReactionExtractor
from crea.sources.pdf import PypdfDocumentParser

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CREA_LIVE_TESTS") != "1",
        reason="set CREA_LIVE_TESTS=1 to run live tests",
    ),
    pytest.mark.skipif(
        not os.environ.get("CREA_LLM_API_KEY"),
        reason="CREA_LLM_API_KEY is not set; skipping rather than failing",
    ),
]

PROCEDURE = [[
    "Experimental",
    "Phenylacetylene (1.0 mmol),",
    "CuI (10 mol%),",
    "DBU (2.0 equiv)",
    "were dissolved in DMF (5 mL).",
    "The mixture was stirred at 80 C",
    "for 12 h.",
    "The product was isolated in 82% yield.",
]]


@pytest.fixture
def evidence():
    directory = pathlib.Path(tempfile.mkdtemp(prefix="crea-live-rxn-"))
    path = directory / "procedure.pdf"
    from tests.fakes import pdfs

    path.write_bytes(pdfs.make_pdf(PROCEDURE))
    library = DocumentLibrary(LocalDocumentService(PypdfDocumentParser()))
    library.ingest(path)
    return search_document(library.all()[0], "CuI")[0].evidence


def test_live_extraction_produces_a_validated_record(evidence):
    """A real model must at least be able to get through the validator."""
    llm = OpenAICompatibleLLMProvider(LLMConfig.from_env())
    result = ReactionExtractor(llm).extract([evidence], document_id="live")

    assert result.outcome.value in ("ok", "partial"), (
        f"nothing survived: {[r.reason for r in result.rejected]}")
    assert result.records
    record = result.records[0]
    assert record.evidence_refs, "a record with no evidence is not a record"

    # whatever it extracted, the values are the paper's
    quote = evidence.raw_quote
    for name in ("temperature", "time", "yield_value"):
        value = getattr(record.conditions, name)
        if value.stated and value.value is not None:
            assert value.raw_value in quote, (
                f"{name}={value.raw_value!r} is not in the quoted text")


def test_live_extraction_invents_nothing(evidence):
    """The paper mentions no pressure. Whatever the model does, none appears."""
    llm = OpenAICompatibleLLMProvider(LLMConfig.from_env())
    result = ReactionExtractor(llm).extract([evidence], document_id="live")
    if not result.records:
        pytest.skip("the model returned nothing usable this time")
    for record in result.records:
        assert record.conditions.pressure.stated is False
        assert record.conditions.atmosphere == ""


def test_live_extraction_cites_real_evidence(evidence):
    """Every field must point at the evidence it was given."""
    llm = OpenAICompatibleLLMProvider(LLMConfig.from_env())
    result = ReactionExtractor(llm).extract([evidence], document_id="live")
    if not result.records:
        pytest.skip("the model returned nothing usable this time")
    for record in result.records:
        for ref in record.evidence_refs:
            assert ref == evidence.evidence_id
