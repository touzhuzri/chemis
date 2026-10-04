"""What the agent has on the desk while it works.

Parsed documents, and the Evidence produced from them. Not a database, not a
cache: a session-scoped collection the operator filled by handing the tool
files, and that dies with the process. Nothing here fetches anything.
"""

from __future__ import annotations

import pathlib

from crea.core.models.document import ParsedDocument
from crea.core.models.evidence import Evidence
from crea.core.models.identity import ChemicalIdentityResolution
from crea.services.documents import LocalDocumentService
from crea.services.evidence import EvidenceRegistry


class DocumentLibrary:
    """Documents the operator made available, by `document_id`."""

    def __init__(self, service: LocalDocumentService) -> None:
        self._service = service
        self._documents: dict[str, ParsedDocument] = {}

    def ingest(self, path) -> ParsedDocument:
        """Take a file the user pointed at. Only ever a local path."""
        parsed = self._service.ingest(path)
        self._documents[parsed.document.document_id] = parsed
        return parsed

    def get(self, document_id: str) -> ParsedDocument | None:
        return self._documents.get(document_id)

    def ids(self) -> list[str]:
        return list(self._documents)

    def all(self) -> list[ParsedDocument]:
        return list(self._documents.values())

    def __len__(self) -> int:
        return len(self._documents)

    def __contains__(self, document_id: object) -> bool:
        return document_id in self._documents

    def paths(self) -> list[str]:
        return [d.document.source_path for d in self._documents.values()
                if d.document.source_path]


def registry_from(documents, evidence: list[Evidence]) -> EvidenceRegistry:
    """Collect Evidence so a Claim's references can be resolved."""
    registry = EvidenceRegistry()
    for item in evidence:
        registry.add(item)
    return registry


class RouteWorkbench:
    """Candidate routes a provider proposed, and the validator that grades them.

    Held between tool calls so `validate_route_step` can find the step the agent
    means. In memory and gone with the process, like the document library.
    """

    def __init__(self, provider=None, validator=None) -> None:
        self.provider = provider
        self.routes: list = []
        self.validator = validator
        self.last_result = None
        self.results: list = []
        self.notes: list[str] = []
        #: `ReactionRecord`s the agent has produced, so `route_evidence_report`
        #: can attach them to steps. Held rather than reconstructed from a tool
        #: result because a report that could cite a record nobody holds would
        #: be citing the model instead of the paper.
        self.records: list = []
        #: `RouteEvidenceReport`s, one per route, from the last call.
        self.evidence_reports: list = []

    def add_records(self, records: list) -> None:
        """Keep the records this task produced, one per reaction id."""
        by_id = {r.reaction_id: r for r in self.records}
        for record in records:
            by_id[record.reaction_id] = record
        self.records = list(by_id.values())

    def store(self, result) -> None:
        self.routes = list(result.routes)
        self.last_result = result

    def find_step(self, step_id: str):
        for route in self.routes:
            step = route.step(step_id)
            if step is not None:
                return route, step
        return None, None

    def summary(self) -> dict:
        return {
            "routes": len(self.routes),
            "steps": sum(len(r.steps) for r in self.routes),
        }


class IdentityState:
    """The open identity question, and how far it got. Held between tool calls.

    Phase 3.6. A chemical name or a drawing is not a structure, and the
    question "what did the user mean?" can end in `AMBIGUOUS`. While it is
    open, no chemistry may run: the thing retrosynthesis would be run on does
    not exist yet, and picking one of the candidates to make it exist is
    exactly the guess this phase is built to prevent.

    Holding the resolution here rather than inside a tool means the gate is a
    fact about the session rather than a fact about one call -- the agent
    cannot clear it by calling a different tool first.
    """

    def __init__(self, service=None) -> None:
        self.service = service
        self.resolution: ChemicalIdentityResolution | None = None

    def hold(self, resolution: ChemicalIdentityResolution) -> ChemicalIdentityResolution:
        self.resolution = resolution
        return resolution

    @property
    def open(self) -> bool:
        """An identity question is open when it was asked and not settled."""
        return self.resolution is not None and not self.resolution.is_resolved

    def blocks_chemistry(self) -> tuple[bool, str]:
        """Whether chemistry may run, and why not when it may not.

        Returns the gate as a fact rather than raising it as an opinion, so
        the tool can report the reason to the agent in words the user will see.
        """
        if self.resolution is None:
            return False, ""
        if self.resolution.is_resolved:
            return False, ""
        status = self.resolution.status.value
        count = len({c.inchikey or c.canonical_smiles for c in self.resolution.candidates})
        if self.resolution.is_ambiguous:
            return True, (
                f"发现 {count} 个候选结构，需要进一步确认。"
                f" Chemistry may not run on an unresolved identity: the "
                f"structure retrosynthesis would use has not been established."
            )
        return True, (
            f"the chemical identity is {status}, not resolved; "
            f"{self.resolution.reason or 'no structure has been established'}"
        )
