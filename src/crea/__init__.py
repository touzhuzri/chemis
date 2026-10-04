"""Chemical Research Agent.

Layering (enforced by tests/architecture/test_layering.py):

    core       -- data models, errors, provenance. No chemistry, no I/O.
    chem       -- Chemical Core. RDKit only. No network, no LLM, no providers.
    providers  -- reserved adapter interfaces for Phase 2. No implementations.
    services   -- orchestration. Depends on chem + providers.
    cli        -- the API/UI layer.
"""

__version__ = "0.1.0"
