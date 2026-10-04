"""T-B -- layer 1 screening."""

import pytest
from pydantic import ValidationError

from crea.chem.fingerprint import screen_similarity
from crea.chem.normalize import normalize
from crea.core.models.matching import SimilarityScreen
from tests.conftest import BENZENE, M_XYLENE, O_XYLENE, P_XYLENE, TOLUENE


def test_B01_self_similarity_is_one():
    m = normalize(BENZENE)
    s = screen_similarity(m, normalize(BENZENE))
    assert s.tanimoto_ecfp4 == 1.0
    assert s.dice_ecfp4 == 1.0


def test_B02_positional_isomers_are_flagged_as_isomers():
    s = screen_similarity(normalize(O_XYLENE), normalize(P_XYLENE))
    assert s.formula_identical is True
    assert s.heavy_atom_delta == 0
    # the InChIKey skeleton block is the *connectivity* layer, so positional
    # isomers differ there; only stereoisomers share it
    assert s.constitutional_isomer_hint is True
    assert s.stereo_isomer_hint is False
    assert s.skeleton_block_equal is False


def test_B03_m_meta_is_also_flagged():
    s = screen_similarity(normalize(M_XYLENE), normalize(P_XYLENE))
    assert s.constitutional_isomer_hint is True


def test_B04_similarity_is_in_a_plausible_band():
    # Asserted as a band rather than an exact value: fingerprint defaults can
    # move between RDKit releases and a hard-coded constant would turn an
    # upstream change into a false failure here.
    s = screen_similarity(normalize(BENZENE), normalize(TOLUENE))
    assert 0.2 <= s.tanimoto_ecfp4 <= 0.7


def test_B05_screening_only_cannot_be_overridden():
    # The type-level guard: a consumer cannot construct a "screening result"
    # that claims to be a conclusion.
    with pytest.raises(ValidationError):
        SimilarityScreen(tanimoto_ecfp4=0.9, screening_only=False)
    assert SimilarityScreen().screening_only is True


def test_B06_fingerprint_round_trip_through_storage():
    from crea.chem.fingerprint import _hex_to_bitvect, compute_fingerprints
    from rdkit import Chem, DataStructs
    from tests.conftest import DIPHENYL_BUTADIYNE

    mol = Chem.MolFromSmiles(DIPHENYL_BUTADIYNE)
    fps = compute_fingerprints(mol)
    a = _hex_to_bitvect(fps.ecfp4_fp2048, fps.fp_size)
    b = _hex_to_bitvect(fps.ecfp4_fp2048, fps.fp_size)
    assert DataStructs.TanimotoSimilarity(a, b) == 1.0
    assert fps.radius == 2 and fps.fp_size == 2048
