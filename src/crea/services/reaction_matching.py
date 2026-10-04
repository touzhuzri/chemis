"""Reaction-level matching: how does a candidate reaction relate to this step?

Molecule similarity is not enough. A paper can be about making the right
product from entirely different substrates, or about the right substrates making
something else, and both look like a hit to a fingerprint.

So the comparison is on both sides at once, and the answer is one of four:

    EXACT          this step's reactants make this step's product
    PARTIAL        one side matches exactly, the other does not
    TRANSFORMATION neither side matches, but the molecules are related
    DISCOVERY      nothing related was found -- an answer, not a failure

Similarity is the last resort and the weakest rung. It is deliberately below
the structural comparisons, and it never produces support on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crea.core.models.route import ReactionMatchType

#: Tanimoto at which two molecules count as "related" rather than "different".
RELATED_FLOOR = 0.55


@dataclass
class ReactionComparison:
    """One candidate reaction measured against one step."""

    source_id: str = ""
    match_type: ReactionMatchType = ReactionMatchType.DISCOVERY
    matched_reactants: list[str] = field(default_factory=list)
    matched_products: list[str] = field(default_factory=list)
    unmatched_reactants: list[str] = field(default_factory=list)
    unmatched_products: list[str] = field(default_factory=list)
    best_similarity: float | None = None
    doi: str | None = None
    title: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "match_type": self.match_type.value,
            "matched_reactants": self.matched_reactants,
            "matched_products": self.matched_products,
            "unmatched_reactants": self.unmatched_reactants,
            "unmatched_products": self.unmatched_products,
            "best_similarity": self.best_similarity,
            "doi": self.doi,
            "title": self.title,
            "notes": self.notes,
        }


def _canonical(smiles: str) -> str:
    from crea.chem.normalize import normalize

    if not smiles:
        return ""
    try:
        return normalize(smiles).canonical_smiles
    except Exception:  # noqa: BLE001
        return smiles


def _similar(a: str, b: str) -> float:
    from crea.chem.fingerprint import screen_similarity
    from crea.chem.normalize import normalize

    try:
        return screen_similarity(normalize(a), normalize(b)).tanimoto_ecfp4
    except Exception:  # noqa: BLE001
        return 0.0


def _side_match(wanted: list[str], offered: list[str]) -> tuple[list, list, float | None]:
    """Split `offered` into those the step also has, and those it does not.

    Order-insensitive, because `A + B` and `B + A` are the same reaction.
    A duplicate in the candidate (two of the same substrate) is matched once and
    reported as unmatched on the second, because a step that does not call for
    two copies did not mean to.
    """
    pool = list(offered)
    matched: list[str] = []
    for want in wanted:
        if want in pool:
            pool.remove(want)
            matched.append(want)
    best = None
    for want in wanted:
        for have in pool:
            score = _similar(want, have)
            if best is None or score > best:
                best = score
    return matched, pool, best


def compare_reactions(
    step_reactants: list[str],
    step_products: list[str],
    candidate_reactants: list[str],
    candidate_products: list[str],
    source_id: str = "",
    doi: str | None = None,
    title: str = "",
) -> ReactionComparison:
    """Measure one candidate reaction against one step. Never consults a model."""
    wanted_r = [_canonical(s) for s in step_reactants if s]
    wanted_p = [_canonical(s) for s in step_products if s]
    have_r = [_canonical(s) for s in candidate_reactants if s]
    have_p = [_canonical(s) for s in candidate_products if s]

    result = ReactionComparison(source_id=source_id, doi=doi, title=title)

    if not have_r and not have_p:
        result.notes.append("the candidate reaction names no molecules")
        return result

    r_hit, r_miss, r_score = _side_match(wanted_r, have_r)
    p_hit, p_miss, p_score = _side_match(wanted_p, have_p)
    result.matched_reactants, result.unmatched_reactants = r_hit, r_miss
    result.matched_products, result.unmatched_products = p_hit, p_miss
    scores = [s for s in (r_score, p_score) if s is not None]
    result.best_similarity = max(scores) if scores else None

    reactants_exact = bool(wanted_r) and not r_miss and r_hit
    products_exact = bool(wanted_p) and not p_miss and p_hit

    if reactants_exact and products_exact:
        result.match_type = ReactionMatchType.EXACT
        return result

    if products_exact or reactants_exact:
        result.match_type = ReactionMatchType.PARTIAL
        result.notes.append(
            "one side matches exactly and the other does not; this is not the "
            "specific substrate combination asked about")
        return result

    if result.best_similarity is not None and result.best_similarity >= RELATED_FLOOR:
        result.match_type = ReactionMatchType.TRANSFORMATION
        result.notes.append(
            "no exact match on either side; the molecules are related and the "
            "transformation may be the same kind, but nothing here was shown "
            "to run this step")
        return result

    result.match_type = ReactionMatchType.DISCOVERY
    result.notes.append("no related reaction was found")
    return result
