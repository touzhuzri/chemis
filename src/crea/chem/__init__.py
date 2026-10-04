"""Chemical Core.

Hard constraints, enforced by `tests/architecture/test_layering.py`:

  * every chemistry result here is produced by RDKit. No hand-rolled
    cheminformatics, and no LLM in any numeric or structural decision.
  * no network I/O. This package must not import httpx, requests, or any
    provider.
  * molecules are constructed only by `normalize.normalize`. Calling
    `Chem.MolFromSmiles` outside this package is an architecture violation.

The layer deliberately cannot talk to Literature/Reaction/Compound providers:
structure knowledge must stand on its own.
"""
