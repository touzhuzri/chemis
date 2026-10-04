"""Chemical name normalization -- one home, three named purposes.

Phase 4 Stage B-1 (§12 of the migration). The audit found name tidying
in three places with no statement of how they differ. They *do* differ --
they answer different questions -- so they are named, together, here:

* `normalize_for_identity` -- "would two spellings build the same
  identity key material?"  Used by reaction-key construction, where a
  case difference must not fork a reaction but a real spelling change
  still might.
* `normalize_for_search` -- "would a lookup with this spelling find that
  record?"  Case-folded and trimmed; used when matching names in text,
  in registries, and in provider queries.
* `display_name` -- "what should a person see?"  Trimmed, spelling
  untouched. Never used to decide identity.

These are pure string functions on the bottom layer on purpose: core
models, services and the semantic layer all need them, and a normalization
that lives in one of the upper layers is a normalization that gets
rewritten in the next one.

None of these is a chemical operation. They do not know what a structure
is, they never guess a spelling, and a change here changes identity keys
-- so the outputs are pinned by tests in `tests/identity/`.
"""

from __future__ import annotations


def normalize_for_identity(name: str) -> str:
    """Identity-key material for a name-only component.

    The exact form `ReactionComponent.key_material` has always used
    (`name:<lowercased trimmed>`): changing it would re-key every stored
    reaction, so the spelling here is a compatibility contract.
    """
    return (name or "").strip().lower()


def normalize_for_search(name: str) -> str:
    """The form a case-insensitive lookup compares on."""
    return (name or "").strip().lower()


def display_name(name: str) -> str:
    """The form a person reads: trimmed, spelling preserved."""
    return (name or "").strip()


__all__ = ["display_name", "normalize_for_identity", "normalize_for_search"]
