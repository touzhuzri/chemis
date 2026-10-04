"""Loading `.env` into the process environment, once.

Credentials live in a file the operator fills in, not in code and not on a
command line where they end up in shell history. The file is read at the point
where a credential is actually needed, so importing this package never touches
it.

`.env` is in `.gitignore`. If you have not created one, nothing happens and
nothing is invented -- a missing credential stays a missing credential and
surfaces as a clear error where it is used.
"""

from __future__ import annotations

import pathlib

ENV_FILENAME = ".env"
_loaded = False


def project_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3]


def load_local_env(force: bool = False) -> bool:
    """Load `.env` into `os.environ` if it exists. Idempotent.

    Values already in the environment win: an explicit export should not be
    undone by a file.
    """
    global _loaded
    if _loaded and not force:
        return False
    _loaded = True

    path = project_root() / ENV_FILENAME
    if not path.exists():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        return _read_manually(path)
    loaded = bool(load_dotenv(path, override=False))
    _drop_empty(path)
    return loaded


def _drop_empty(path: pathlib.Path) -> None:
    """Remove keys the file set to an empty value.

    python-dotenv happily sets `KEY=` as an empty string, so a placeholder
    left in the template becomes a set-but-blank credential. Consumers already
    treat a blank as absent, but the manual fallback skips blanks outright and
    the two paths should not disagree about what a file means.
    """
    import os

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name and not value.strip().strip("'\"") and os.environ.get(name) == "":
            os.environ.pop(name, None)


def _read_manually(path: pathlib.Path) -> bool:
    """A fallback for an environment without python-dotenv."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    import os

    loaded = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip("'\"")
        if name and value and name not in os.environ:
            os.environ[name] = value
            loaded = True
    return loaded
