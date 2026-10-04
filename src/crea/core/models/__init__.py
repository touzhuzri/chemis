"""Data models. This package is the contract shared by every other layer and
must not import from chem, providers, or services.
"""

from crea.core.models.evidence import (
    Claim,
    Evidence,
    EvidenceType,
    Locator,
    evidence_identity,
)
from crea.core.models.fragments import (
    FRAGMENT_FORMULA_CAVEAT,
    AttachmentMatch,
    AttachmentPoint,
    Fragment,
    FragmentMatch,
    LocalEnvironment,
    RoleSignature,
)
from crea.core.models.matching import (
    AlignmentCandidate,
    CoreMatch,
    MatchConflicts,
    MCSParams,
    ScaffoldMatch,
    SimilarityScreen,
    UnmatchedComponent,
)
from crea.core.models.molecule import (
    AtomRef,
    BondRef,
    Descriptors,
    FingerprintSet,
    Molecule,
    SourceRef,
)
from crea.core.models.report import MatchInput, StructureMatchReport

__all__ = [
    "FRAGMENT_FORMULA_CAVEAT",
    "Claim",
    "Evidence",
    "EvidenceType",
    "Locator",
    "evidence_identity",
    "AlignmentCandidate",
    "AtomRef",
    "AttachmentMatch",
    "AttachmentPoint",
    "BondRef",
    "CoreMatch",
    "Descriptors",
    "FingerprintSet",
    "Fragment",
    "FragmentMatch",
    "LocalEnvironment",
    "MCSParams",
    "MatchConflicts",
    "MatchInput",
    "Molecule",
    "RoleSignature",
    "ScaffoldMatch",
    "SimilarityScreen",
    "SourceRef",
    "StructureMatchReport",
    "UnmatchedComponent",
]
