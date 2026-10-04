"""Stable identifier helpers.

Identifiers must be reproducible across runs so that golden fixtures and
cached results stay valid.
"""

from __future__ import annotations

import hashlib
import re

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_DOI_PREFIX = re.compile(
    r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/|info:doi/doi/|doi\.org/)", re.IGNORECASE
)
_DOI_TRAILING = re.compile(r"[.,;)\]]+$")
_WS = re.compile(r"\s+")


def slug(text: str, prefix: str = "", max_len: int = 32) -> str:
    """Lowercase, hyphen-separated, stable slug of arbitrary text."""
    out = _SLUG_STRIP.sub("-", text.strip().lower()).strip("-")
    out = out[:max_len].strip("-")
    return f"{prefix}{out}" if prefix else out


def content_hash(text: str, length: int = 16) -> str:
    """Short content hash. Used for full-text provenance and fixture identity."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def normalize_doi(value: str | None) -> str | None:
    """Canonical DOI string, or None if there is nothing usable.

    Lives here rather than in any one layer because three places need the same
    answer: providers that receive DOIs in resolver form, the dedup key, and
    the cache key. Two implementations of this that drift would quietly split
    one work into two, which is the exact failure dedup exists to prevent.

    Handles the forms that actually turn up: prefixed resolvers, a trailing
    period left by a sentence, a URL query string, and citation brackets.
    """
    if not value:
        return None
    out = _WS.sub(" ", str(value)).strip()
    out = _DOI_PREFIX.sub("", out)
    out = out.split("#", 1)[0].split("?", 1)[0]
    while len(out) > 1 and out[0] in "([{" and out[-1] in ")]}":
        out = out[1:-1].strip()
    out = _DOI_TRAILING.sub("", out).strip().lower()
    return out or None
