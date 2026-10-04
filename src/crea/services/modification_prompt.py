"""The modification-analysis prompt (CREA Stage 6.2).

The prompt is built here, apart from the business logic (§8). It states the
chemistry boundaries the model must respect -- the same ones the whole project
enforces -- and requires a JSON object, so nothing is left to prose (§16).
"""

from __future__ import annotations

import json

SYSTEM = """\
You are assisting a chemist by interpreting a REQUESTED structural modification \
of a molecule, using ONLY the CREA context supplied.

Hard rules:
1. Use only facts present in the CREA context. Never invent a reaction, a \
reagent, a condition, a yield, a citation, a DOI, a paper title or an evidence id.
2. Every candidate MUST cite at least one `reaction_template_id` that appears \
verbatim in the context's reactions. If none fits, return no candidate.
3. `evidence_ids` and `literature_ids` may only repeat ids present in the context.
4. A SMARTS / pattern match is NOT experimental success. A curated reaction is \
NOT a literature proof. A literature link does NOT mean this exact substrate was \
demonstrated. Fulltext permission is independent of evidence validity.
5. Never state or predict a yield. Never state a condition the context does not \
contain. If the context lacks something, say so.
6. `rationale` is your own short interpretation and must not add new facts.

Respond with ONE JSON object, no prose around it:
{
  "candidates": [
    {
      "description": "short description of the structural modification",
      "structural_change": {"from_feature": "...", "to_feature": "...", "scope": "..."},
      "reaction_template_ids": ["<id from the context>"],
      "evidence_ids": ["<id from the context>"],
      "literature_ids": ["<id from the context>"],
      "rationale": "why this reaction template is relevant"
    }
  ],
  "notes": ["anything the context lacks for this request"]
}
If nothing in the context fits the request, return {"candidates": [], "notes": ["..."]}.
"""


def build_user_message(context, query: str, target_smiles: str = "") -> str:
    """The model's user turn: the context and the request, plainly."""
    payload = {
        "requested_modification": query,
        "target_smiles": target_smiles or None,
        "crea_context": context.model_dump(mode="json"),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def required_reaction_ids(context) -> set[str]:
    return {r.template_id for r in context.reactions}


def allowed_evidence_ids(context) -> set[str]:
    return {e.evidence_id for e in context.evidence}


def allowed_literature_ids(context) -> set[str]:
    return {lit.paper_id for lit in context.literature}


__all__ = [
    "SYSTEM",
    "allowed_evidence_ids",
    "allowed_literature_ids",
    "build_user_message",
    "required_reaction_ids",
]
