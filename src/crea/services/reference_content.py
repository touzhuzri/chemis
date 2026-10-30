"""M1.7.2 -- reference content retrieval: read sealed bytes, verify,
report honestly.

`ReferenceMaterial` holds a `content_locator` (where an authorized
keeper stored the artifact) and a `content_hash` (what the artifact
should hash to). This service is the only supported way to turn that
locator back into bytes -- and it refuses to call bytes "the reference"
until they hash as declared:

* **Verified or nothing.** `OK` requires a declared hash and a match.
  A missing hash is `HASH_MISSING`; a mismatch is `HASH_MISMATCH` and
  does not return the mismatched bytes as content. Unverified material
  never wears the name of valid reference material.
* **Sealed stays sealed.** A hidden reference reads as `NOT_UNSEALED`.
  Reading is not unsealing; the unseal service is still the only door
  that opens a reference.
* **Locators are data, not instructions.** Only relative paths under
  the configured roots are read. Absolute paths, `..` escapes (checked
  after resolution, so symlinks cannot smuggle an escape), and every
  URL scheme are `NOT_ALLOWED` -- this reader never touches the network,
  so a locator cannot become an SSRF. Error details name the reference
  and the status, never the resolved local path.

Deliberately absent, per this stage's scope: PDF parsing, OCR, SI
parsing, reaction extraction. This service answers "what bytes did the
keeper store, and do they match" -- nothing about what they mean.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

from crea.core.errors import GoldenCaseError
from crea.core.models.golden_case_access import (
    ContentStatus,
    ReferenceContentResult,
)
from crea.services.golden_case_store import GoldenCaseStore

#: The one locator scheme this reader understands. A bare relative path
#: is treated as `file:`. Every other scheme is refused before any I/O.
ALLOWED_SCHEMES = ("file",)

#: A Windows drive prefix ("C:", "d:\") -- which urlparse would otherwise
#: mistake for a URL scheme, and Path.is_absolute can miss when the path
#: is rooted but driveless.
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class ReferenceContentReader:
    """Read one reference's stored bytes, under configured roots."""

    def __init__(self, store: GoldenCaseStore, *,
                 allowed_roots: list[Path] | tuple[Path, ...]) -> None:
        if not allowed_roots:
            raise GoldenCaseError(
                "INVALID_INPUT",
                "ReferenceContentReader requires at least one allowed "
                "root; an unrooted reader would read any path the "
                "locator names")
        # Resolved once, up front: containment is checked against these.
        self._roots = tuple(Path(root).resolve() for root in allowed_roots)
        self._store = store

    def read(self, case_id: str, reference_id: str) -> ReferenceContentResult:
        """Read and verify one reference's content.

        Never raises for a content-level outcome -- missing, unreadable,
        unverified, sealed and out-of-bounds are each a `ContentStatus`
        with `content=None`. It raises only when the case or the
        reference itself does not exist: asking for a record nobody
        stores is a caller error, not a content verdict.
        """
        material = self._store.get_reference(case_id, reference_id)
        base = ReferenceContentResult(
            reference_id=reference_id,
            status=ContentStatus.NOT_FOUND,
            declared_hash=material.content_hash,
            locator=material.content_locator,
        )

        if material.hidden:
            return base.model_copy(update={
                "status": ContentStatus.NOT_UNSEALED,
                "detail": "reference is still sealed; unseal it before "
                          "reading its content"})

        locator = (material.content_locator or "").strip()
        if not locator:
            return base.model_copy(update={
                "status": ContentStatus.NOT_FOUND,
                "detail": "material declares no content_locator"})

        path = self._resolve(locator)
        if path is None:
            return base.model_copy(update={
                "status": ContentStatus.NOT_ALLOWED,
                "detail": "locator uses a forbidden scheme or resolves "
                          "outside the allowed roots"})

        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return base.model_copy(update={
                "status": ContentStatus.NOT_FOUND,
                "detail": "no stored artifact at the declared locator"})
        except OSError as exc:
            # The exception name only: paths are ours, not the caller's
            # to see.
            return base.model_copy(update={
                "status": ContentStatus.UNREADABLE,
                "detail": f"artifact could not be read "
                          f"({type(exc).__name__})"})

        computed = "sha256:" + hashlib.sha256(data).hexdigest()
        declared = self._normalize_hash(material.content_hash)
        if not declared:
            return base.model_copy(update={
                "status": ContentStatus.HASH_MISSING,
                "computed_hash": computed,
                "detail": "material declares no content_hash; content "
                          "cannot be verified and is not returned"})
        if not computed.removeprefix("sha256:").startswith(declared):
            return base.model_copy(update={
                "status": ContentStatus.HASH_MISMATCH,
                "computed_hash": computed,
                "detail": "stored bytes do not match the declared hash; "
                          "content is not returned as verified"})
        return base.model_copy(update={
            "status": ContentStatus.OK,
            "content": data,
            "computed_hash": computed,
            "detail": "content verified against the declared hash"})

    # -- internals ----------------------------------------------------------

    def _resolve(self, locator: str) -> Path | None:
        """The allowed-root-relative path a locator names, or None.

        Scheme first (no URL ever becomes a fetch), then shape, then
        containment. Absolute paths are refused by shape -- on Windows
        `Path.is_absolute` misses a rooted path with no drive, and
        urlparse mistakes a drive letter for a URL scheme, so the drive
        and root prefixes are checked textually. Containment is checked
        after resolution, so `..` and symlinks are judged by where they
        land.
        """
        parsed = urlparse(locator)
        if parsed.scheme and parsed.scheme.lower() not in ALLOWED_SCHEMES:
            return None
        raw = locator.split(":", 1)[1] if parsed.scheme else locator
        if not raw or raw[0] in "/\\" or _DRIVE_PREFIX.match(raw):
            # "/x", "\\x", "C:\x", "C:/x" -- all outside the contract.
            return None
        candidate_raw = Path(raw)
        if not candidate_raw.parts:
            return None
        for root in self._roots:
            candidate = (root / candidate_raw).resolve()
            if self._contains(root, candidate):
                return candidate
        return None

    @staticmethod
    def _contains(root: Path, candidate: Path) -> bool:
        """Containment after resolution, so `..` and symlinks are
        judged by where they land, not where they claim to start."""
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _normalize_hash(declared: str) -> str:
        """The hex part of a declared hash, lowercase.

        Accepts the bare hex form and the `sha256:<hex>` form used
        across this project. Empty means "nothing declared".
        """
        value = (declared or "").strip().lower()
        if value.startswith("sha256:"):
            value = value[len("sha256:"):]
        return value.strip()


__all__ = ["ALLOWED_SCHEMES", "ReferenceContentReader"]
