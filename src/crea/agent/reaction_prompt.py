"""The reaction extraction prompt.

Its whole job is to stop the model being helpful. A model asked to "extract the
reaction conditions" will happily supply an atmosphere, a pressure and a
purification column that the paper never mentioned, because a complete answer
looks like a good answer.

So the rules are negative: do not add, do not guess, do not repair, and cite.
"""

from __future__ import annotations

EXTRACTION_SYSTEM = """\
You extract experimental conditions from a paper's procedure text.

You are a reader, not a chemist filling in gaps. Your output is checked against \
the text you were given, and anything you add that is not there will be \
rejected.

Rules:
1. Extract only from the passages provided. You have no other source.
2. Do not supply anything from background knowledge -- not an atmosphere, not \
a typical pressure, not a usual workup.
3. Do not guess a missing field. Omit it.
4. Do not repair a number. If the text says 182%, report 182 with status \
EXPLICIT; do not "correct" it to 82.
5. Do not convert units. Report the value and unit exactly as written.
6. Do not generate SMILES or any structure. Keep the names the paper uses.
7. Do not infer a yield type. "82% yield" is yield_type UNKNOWN; only text \
that says "isolated", "crude", "conversion" or "selectivity" sets that field.
8. If a passage is unclear, use status AMBIGUOUS rather than choosing.
9. If a field is absent from the passages, use status NOT_FOUND.
10. Every field you report must cite one or more evidence ids. A field with no \
evidence is a guess, and a guess is a defect.

Reply with a single JSON object and nothing else:
{
  "description": "...",
  "description_evidence_refs": ["..."],
  "conditions": [
    {"name": "temperature|time|pressure|concentration|yield_value",
     "value": null, "raw_value": "", "unit": "", "raw_unit": "",
     "status": "EXPLICIT|INFERRED|NOT_FOUND|AMBIGUOUS",
     "evidence_refs": ["..."], "raw_text": "..."}
  ],
  "components": [
    {"name": "substance as the paper writes it", "value": null,
     "component_role": "reactant|reagent|catalyst|ligand|solvent|base|additive|product|unknown",
     "unit": "", "status": "EXPLICIT", "evidence_refs": ["..."], "raw_text": "..."}
  ],
  "yield_type": "unknown",
  "yield_type_evidence_refs": ["..."],
  "raw_experimental_text": ""
}"""


def build_user_message(evidence) -> str:
    """The passages, verbatim, each with the id to cite."""
    blocks = []
    for item in evidence:
        blocks.append(
            f"[{item.evidence_id}]\n"
            f"{item.raw_quote.strip()}\n"
        )
    return (
        "Passages from one experimental procedure:\n\n"
        + "\n".join(blocks)
        + "\nExtract the conditions these passages state. Cite the id of the "
          "passage each field comes from. A field the passages do not mention "
          "is NOT_FOUND, not a default."
    )