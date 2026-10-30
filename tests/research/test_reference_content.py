"""M1.7.2 -- reference content retrieval: verification, and the path
and scheme sandbox.

What is pinned here: content is returned only when it hashes as
declared; a missing hash, a mismatch, an unreadable or absent artifact
each get their own status and no bytes; a sealed reference reads as
NOT_UNSEALED; locators outside the allowed roots -- `..` escapes, absolute
paths, drive letters, every URL scheme -- are refused before any I/O;
and no error detail leaks the resolved local path.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from crea.core.errors import GoldenCaseError
from crea.core.models.golden_case import (
    BlindInput,
    GoldenCase,
    GoldenCaseType,
    ReferenceMaterial,
    ReferenceMaterialKind,
)
from crea.core.models.golden_case_access import ContentStatus
from crea.services.golden_case_store import GoldenCaseStore
from crea.services.reference_content import ReferenceContentReader
from crea.services.knowledge_base import KnowledgeBase

SECRET = b"the expert route: step 1, then step 2\n"


@pytest.fixture()
def kb(tmp_path):
    store = KnowledgeBase(pathlib.Path(tmp_path) / "crea.db")
    store.initialize()
    yield store
    store.close()


@pytest.fixture()
def store(kb):
    return GoldenCaseStore(kb)


@pytest.fixture()
def sealed_dir(tmp_path):
    root = tmp_path / "sealed"
    (root / "refs").mkdir(parents=True)
    return root


@pytest.fixture()
def reader(store, sealed_dir):
    return ReferenceContentReader(store, allowed_roots=[sealed_dir])


def _hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _prepare(store, sealed_dir, *, content_hash: str | None = _hash(SECRET),
             locator: str = "refs/route.txt", hidden: bool = False,
             write: bytes | None = SECRET) -> None:
    store.create(GoldenCase(
        case_id="case-1", name="阿司匹林盲测",
        case_type=GoldenCaseType.BLIND_REPRODUCTION,
        blind_input=BlindInput(goal="g")))
    if write is not None:
        (sealed_dir / "refs" / "route.txt").write_bytes(write)
    store.save_reference("case-1", ReferenceMaterial(
        reference_id="ref-1", material_type=ReferenceMaterialKind.EXPERT_ROUTE,
        title="专家路线", content_locator=locator,
        content_hash=content_hash or "", hidden=hidden))


# ---------------------------------------------------------------------------
# The six verdicts
# ---------------------------------------------------------------------------


def test_matching_bytes_verify_and_come_back(store, sealed_dir, reader):
    _prepare(store, sealed_dir)

    result = reader.read("case-1", "ref-1")

    assert result.status is ContentStatus.OK
    assert result.content == SECRET
    assert result.computed_hash == _hash(SECRET)
    assert result.declared_hash == _hash(SECRET)
    assert result.locator == "refs/route.txt"


def test_a_mismatched_hash_is_reported_and_returns_no_content(
        store, sealed_dir, reader):
    _prepare(store, sealed_dir, content_hash="sha256:" + "0" * 64)

    result = reader.read("case-1", "ref-1")

    assert result.status is ContentStatus.HASH_MISMATCH
    assert result.content is None
    assert result.computed_hash == _hash(SECRET)
    assert "do not match" in result.detail


def test_a_declared_hash_that_is_only_a_prefix_still_verifies(
        store, sealed_dir, reader):
    # The project's own content_hash() truncates; a prefix declaration
    # is the same claim, checked the same way.
    _prepare(store, sealed_dir,
             content_hash="sha256:" + hashlib.sha256(SECRET).hexdigest()[:12])

    assert reader.read("case-1", "ref-1").status is ContentStatus.OK


def test_a_missing_declared_hash_is_its_own_status(store, sealed_dir, reader):
    _prepare(store, sealed_dir, content_hash=None)

    result = reader.read("case-1", "ref-1")

    assert result.status is ContentStatus.HASH_MISSING
    assert result.content is None
    assert result.computed_hash == _hash(SECRET), "仍报告实际哈希供调查"
    assert "cannot be verified" in result.detail


def test_an_absent_artifact_is_not_found(store, sealed_dir, reader):
    _prepare(store, sealed_dir, write=None)

    result = reader.read("case-1", "ref-1")

    assert result.status is ContentStatus.NOT_FOUND
    assert result.content is None


def test_a_locator_that_names_nothing_is_not_found(store, sealed_dir, reader):
    _prepare(store, sealed_dir, locator="", write=None)

    result = reader.read("case-1", "ref-1")

    assert result.status is ContentStatus.NOT_FOUND
    assert "no content_locator" in result.detail


def test_a_sealed_reference_reads_as_not_unsealed(store, sealed_dir, reader):
    _prepare(store, sealed_dir, hidden=True)

    result = reader.read("case-1", "ref-1")

    assert result.status is ContentStatus.NOT_UNSEALED
    assert result.content is None
    assert "sealed" in result.detail


def test_an_unreadable_artifact_is_unreadable(store, sealed_dir, reader):
    # A directory where the artifact should be: open fails, honestly.
    _prepare(store, sealed_dir, write=None)
    (sealed_dir / "refs" / "route.txt").mkdir()

    result = reader.read("case-1", "ref-1")

    assert result.status is ContentStatus.UNREADABLE
    assert result.content is None


# ---------------------------------------------------------------------------
# The sandbox: schemes, traversal, absolute paths, roots
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locator", [
    "http://example.com/route.txt",
    "https://example.com/route.txt",
    "ftp://example.com/route.txt",
    "file://example.com/route.txt",
    "data:text/plain;base64,aGk=",
])
def test_every_url_scheme_is_refused_before_any_io(store, sealed_dir, reader,
                                                   locator):
    _prepare(store, sealed_dir, locator=locator, write=None)

    result = reader.read("case-1", "ref-1")

    assert result.status is ContentStatus.NOT_ALLOWED
    assert result.content is None


@pytest.mark.parametrize("locator", [
    "../outside.txt",
    "refs/../../outside.txt",
])
def test_traversal_is_refused_even_when_the_target_exists(store, tmp_path,
                                                          sealed_dir, reader,
                                                          locator):
    (tmp_path / "outside.txt").write_bytes(b"escape")
    _prepare(store, sealed_dir, locator=locator, write=None)

    result = reader.read("case-1", "ref-1")

    assert result.status is ContentStatus.NOT_ALLOWED
    assert result.content is None


@pytest.mark.parametrize("locator", [
    "/etc/passwd",
    "\\Windows\\win.ini",
    "C:\\secrets\\route.txt",
    "c:/secrets/route.txt",
])
def test_absolute_paths_and_drives_are_refused(store, sealed_dir, reader,
                                               locator):
    _prepare(store, sealed_dir, locator=locator, write=None)

    result = reader.read("case-1", "ref-1")

    assert result.status is ContentStatus.NOT_ALLOWED


def test_a_file_scheme_relative_path_is_allowed(store, sealed_dir, reader):
    _prepare(store, sealed_dir, locator="file:refs/route.txt")

    assert reader.read("case-1", "ref-1").status is ContentStatus.OK


def test_an_error_detail_never_leaks_the_resolved_path(store, sealed_dir,
                                                       reader, tmp_path):
    _prepare(store, sealed_dir, write=None)

    result = reader.read("case-1", "ref-1")

    assert str(sealed_dir) not in result.detail
    assert str(tmp_path) not in result.detail
    assert str(sealed_dir.resolve()) not in result.locator


def test_a_reader_without_roots_cannot_be_built(store):
    with pytest.raises(GoldenCaseError) as excinfo:
        ReferenceContentReader(store, allowed_roots=[])
    assert excinfo.value.code == "INVALID_INPUT"


def test_an_unknown_reference_is_a_typed_error_not_a_verdict(store,
                                                             sealed_dir,
                                                             reader):
    _prepare(store, sealed_dir)
    with pytest.raises(GoldenCaseError) as excinfo:
        reader.read("case-1", "ref-missing")
    assert excinfo.value.code == "REFERENCE_NOT_FOUND"


def test_content_stays_inside_one_case(store, sealed_dir, reader):
    _prepare(store, sealed_dir)
    store.create(GoldenCase(
        case_id="case-2", name="另一个",
        case_type=GoldenCaseType.BLIND_REPRODUCTION,
        blind_input=BlindInput(goal="g")))
    store.save_reference("case-2", ReferenceMaterial(
        reference_id="ref-1", material_type=ReferenceMaterialKind.PAPER,
        content_locator="refs/route.txt", hidden=False))

    # case-2's ref-1 declares no hash: same locator, different record,
    # different verdict -- records are per case, never shared.
    result = reader.read("case-2", "ref-1")
    assert result.status is ContentStatus.HASH_MISSING
    with pytest.raises(GoldenCaseError):
        reader.read("case-2", "ref-other")
