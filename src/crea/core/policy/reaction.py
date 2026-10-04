"""What has to be true before a proposed experimental fact is believed.

The model proposes; this disposes. Every rule here exists because the failure
it prevents is one a reader would not catch:

* a fabricated evidence id, so the claim looks sourced
* a number that is not in the quoted text, so the text and the record disagree
* a plausible default where the paper said nothing, so absence reads as fact
* two quotes that disagree, silently resolved in favour of one of them

The rules are deliberately dumb. There is no chemical plausibility check: a
catalyst loading of 500 mol% is odd, but the paper may well say it, and this
layer's job is to report what is written, not to decide what is sensible.
"""

from __future__ import annotations

import re

from crea.core.models.reaction import (
    Component,
    ComponentRole,
    ExtractionConflict,
    ExtractionRejection,
    FieldProposal,
    FieldStatus,
    Quantity,
    ReactionConditions,
    YieldType,
)

POLICY_VERSION = "reaction-v1"

#: Units we recognise per field. An unrecognised unit is *not* converted and
#: *not* rejected -- it is kept verbatim, because "we could not interpret this
#: unit" and "the paper did not say" are different statements.
TEMPERATURE_UNITS = {"°c", "c", "degc", "°", "k", "kelvin"}
TIME_UNITS = {"s", "sec", "min", "h", "hr", "hour", "day", "d"}
YIELD_UNITS = {"%"}
PRESSURE_UNITS = {"atm", "bar", "kpa", "mpa", "pa", "torr", "psi"}

_UNIT_SETS = {
    "temperature": TEMPERATURE_UNITS,
    "time": TIME_UNITS,
    "yield_value": YIELD_UNITS,
    "pressure": PRESSURE_UNITS,
    "concentration": YIELD_UNITS,
}

_NUMERIC = re.compile(r"-?\d+(?:\.\d+)?")


def numeric_forms(value: float | int | None, raw: str) -> list[str]:
    """Every spelling of this number that a quote might legitimately contain.

    A paper writes `82`, a model may propose `"82"`, and the check must not
    fail merely because a stringified float grew a decimal point.
    """
    forms: list[str] = []
    if raw.strip():
        forms.append(raw.strip())
    if isinstance(value, (int, float)):
        for candidate in (value, int(value) if float(value).is_integer() else value):
            text = str(candidate)
            if text not in forms:
                forms.append(text)
    return forms


def _number_is_quoted(form: str, quotes: str) -> bool:
    pattern = r"(?<![\d.])" + re.escape(form) + r"(?![\d.])"
    return bool(re.search(pattern, quotes))


def number_is_quoted(proposal: FieldProposal, quotes: str) -> bool:
    """Does the cited text actually contain this number?"""
    if proposal.raw_text and proposal.raw_text.strip():
        # the model quoted a span; require it to be present verbatim
        if proposal.raw_text.strip() not in quotes:
            return False
    forms = numeric_forms(proposal.value, proposal.raw_value)
    if not forms:
        return True      # a non-numeric field has nothing to check this way
    # Boundaries, not substring: "8" is a substring of "80", and a proposal that
    # silently turned 80 into 8 is exactly the corruption this check is for.
    for form in forms:
        pattern = r"(?<![\d.])" + re.escape(form) + r"(?![\d.])"
        if re.search(pattern, quotes):
            return True
    return False


def validate_quantity(
    name: str,
    proposal: FieldProposal,
    quotes: str,
    reason_prefix: str = "",
) -> tuple[Quantity | None, str | None]:
    """Build a Quantity, or explain why the proposal does not become one."""
    def reject(reason: str) -> str:
        return f"{reason_prefix}{reason}" if reason_prefix else reason

    status = proposal.status
    if status in (FieldStatus.NOT_FOUND,):
        return Quantity(
            status=FieldStatus.NOT_FOUND, raw_text=proposal.raw_text), None
    if status is FieldStatus.AMBIGUOUS:
        return Quantity(
            status=FieldStatus.AMBIGUOUS, raw_value=proposal.raw_value,
            unit=proposal.unit or "UNKNOWN", raw_unit=proposal.raw_unit,
            evidence_refs=list(proposal.evidence_refs),
            raw_text=proposal.raw_text), None

    if proposal.value is None and not proposal.raw_value.strip():
        return Quantity(status=FieldStatus.NOT_FOUND), None

    value: float | None
    try:
        value = float(proposal.value) if proposal.value is not None else None
    except (TypeError, ValueError):
        return None, reject(f"{name}: value {proposal.value!r} is not a number")

    if not number_is_quoted(proposal, quotes):
        return None, reject(
            f"{name}: {proposal.raw_value or proposal.value!r} does not appear "
            f"in the cited text"
        )

    unit = (proposal.unit or proposal.raw_unit or "UNKNOWN").strip()
    if name == "yield_value" and unit in ("%", ""):
        # a bare number with no unit is not a yield
        if unit == "":
            return None, reject("yield_value: a percentage needs a unit")

    if name == "yield_value" and unit == "%" and value is not None:
        if not (0.0 <= value <= 100.0):
            # A yield above 100% is a typo or a misparse. We say so; we do not
            # repair it, because "82" and "182" differ by a digit we invented.
            return None, reject(
                f"yield_value: {value}% is outside 0-100 and is not accepted")
    elif name == "yield_value" and unit != "%":
        # 82 could be a conversion or a selectivity; without the unit we cannot
        # check the range, and we are not going to assume.
        pass

    known = _UNIT_SETS.get(name)
    if known and unit not in known and unit != "UNKNOWN":
        # kept, not converted, not rejected
        pass

    return Quantity(
        value=value,
        raw_value=(proposal.raw_value or (str(proposal.value) if proposal.value is not None else "")),
        unit=unit,
        raw_unit=(proposal.raw_unit or unit),
        status=status,
        evidence_refs=list(proposal.evidence_refs),
        raw_text=proposal.raw_text,
    ), None


_ROLE_MAP = {
    "reactant": ComponentRole.REACTANT,
    "reagent": ComponentRole.REAGENT,
    "catalyst": ComponentRole.CATALYST,
    "ligand": ComponentRole.LIGAND,
    "solvent": ComponentRole.SOLVENT,
    "base": ComponentRole.BASE,
    "additive": ComponentRole.ADDITIVE,
    "product": ComponentRole.PRODUCT,
}


def validate_component(proposal: FieldProposal, quotes: str):
    """Build a Component, or explain the rejection.

    The substance name lives in `name`, which is what the schema says and what
    a real model fills in; the amount, when there is one, arrives in `value`.
    Reading the name out of `value` instead turned a live proposal of
    "Phenylacetylene, 1.0 mmol" into a substance called "1.0".
    """
    name = (proposal.name or proposal.raw_value or "").strip()
    if not name and isinstance(proposal.value, str):
        name = proposal.value.strip()
    if not name:
        return None, "component: no name proposed"
    if isinstance(proposal.value, (int, float)) and not _number_is_quoted(
            str(proposal.value), quotes):
        return None, (
            f"component {name!r}: the amount {proposal.value!r} does not "
            f"appear in the cited text")
    if proposal.raw_text and proposal.raw_text.strip() \
            and proposal.raw_text.strip() not in quotes:
        return None, f"component {name!r}: quoted span is not in the cited text"
    role = _ROLE_MAP.get((proposal.component_role or "").lower(),
                         ComponentRole.UNKNOWN)
    amount = None
    if proposal.value not in (None, "") and isinstance(proposal.value, (int, float)):
        amount = Quantity(
            value=float(proposal.value),
            raw_value=str(proposal.value),
            unit=proposal.unit or "UNKNOWN",
            raw_unit=proposal.raw_unit or proposal.unit or "UNKNOWN",
            status=FieldStatus.EXPLICIT,
            evidence_refs=list(proposal.evidence_refs),
        )
    return Component(
        name=name,
        role=role,
        amount=amount,
        unit=proposal.unit or "",
        raw_text=proposal.raw_text or name,
        evidence_refs=list(proposal.evidence_refs),
    ), None


def detect_conflicts(
    fields: list[tuple[str, str, str]],
) -> list[ExtractionConflict]:
    """Two evidence saying different things about one field.

    `fields` is (name, rendered value, evidence id). A name carrying two
    different values is a conflict, and it stays a conflict: the record keeps
    both and the report says so.
    """
    by_name: dict[str, dict[str, str]] = {}
    refs: dict[str, list[str]] = {}
    for name, rendered, ref in fields:
        if not rendered:
            continue
        by_name.setdefault(name, {})
        by_name[name][ref] = rendered
        refs.setdefault(name, []).append(ref)

    out: list[ExtractionConflict] = []
    for name, values in by_name.items():
        distinct = set(values.values())
        if len(distinct) > 1:
            out.append(ExtractionConflict(
                field=name,
                values=dict(values),
                evidence_refs=sorted(set(refs[name])),
                note="the provided passages disagree; neither value was chosen",
            ))
    return out


def validate_yield_type(value: str, evidence_refs: list[str],
                        quotes: str) -> tuple[YieldType, str | None]:
    """A yield type needs its own evidence.

    "82% yield" does not say isolated. Converting that to `isolated` because
    it usually is would be the single most likely invention in this whole
    module, so the default is UNKNOWN and changing it requires a quote.
    """
    normalised = (value or "unknown").strip().lower()
    try:
        kind = YieldType(normalised)
    except ValueError:
        return YieldType.UNKNOWN, (
            f"yield_type {value!r} is not a known kind; recorded as unknown")
    if kind is YieldType.UNKNOWN:
        return YieldType.UNKNOWN, None
    if not evidence_refs:
        return YieldType.UNKNOWN, "yield_type was claimed with no supporting evidence"
    marker = {
        YieldType.ISOLATED: "isolat",
        YieldType.CRUDE: "crude",
        YieldType.CONVERSION: "conversion",
        YieldType.SELECTIVITY: "selectiv",
        YieldType.YIELD: "yield",
    }[kind]
    lowered = quotes.lower()
    if not any(r in lowered for r in (marker,)):
        return YieldType.UNKNOWN, (
            f"yield_type {kind.value!r} is not supported by the cited text")
    return kind, None


def empty_conditions() -> ReactionConditions:
    return ReactionConditions()


def rejection(field: str, reason: str, proposal: Any = None) -> ExtractionRejection:
    """Record what was thrown away and why.

    `proposal` is a `FieldProposal` when there is one, and the raw mapping
    otherwise -- a bad evidence reference belongs to a whole record rather than
    to any single field.
    """
    if isinstance(proposal, FieldProposal):
        payload = proposal.model_dump(mode="json")
    elif isinstance(proposal, dict):
        payload = dict(proposal)
    elif proposal is None:
        payload = {}
    else:
        payload = {"value": str(proposal)}
    return ExtractionRejection(field=field, reason=reason, proposal=payload)