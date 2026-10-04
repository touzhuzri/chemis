"""RDKit version lookup.

Lives in the Chemical Core, not in `core`: the `core` layer must not import
RDKit at all, and knowing which RDKit produced a number is a fact about the
Chemical Core.
"""

from __future__ import annotations


def rdkit_version() -> str:
    import rdkit

    return rdkit.__version__
