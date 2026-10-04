"""The workbench HTTP API (Phase 4 §5).

`create_app` builds the FastAPI application over `crea.app`. The frontend
bundle is served from the same process (§103).
"""

from crea.api.app import create_app

__all__ = ["create_app"]
