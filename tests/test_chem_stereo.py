"""Stereo rules of compound_registry.chem (rules 1, 3, 4 and their consequences).

Same CORE      <=> equal core_key
Same compound  <=> equal (core_key, stereo_key, stereo_mode)
"""

from __future__ import annotations

import itertools

import pytest

from chem_helpers import (
    keys,
    mirror_x,
    molblock,
    read_fixture,
    renumber_v2000,
    set_bond_stereo,
    set_chiral_flag,
    strip_wedges,
    transform_coords,
)
from test_chem import assert_same_compound, mf, sm
from test_chem_fixtures import ATROP_RENUMBER, MESO_TARTARIC_OTHER_END

R_ALA = "C[C@@H](N)C(=O)O"
S_ALA = "C[C@H](N)C(=O)O"
FLAT_ALA = "CC(N)C(=O)O"

L_THR = "C[C@@H](O)[C@H](N)C(=O)O"        # (2S,3R)
D_THR = "C[C@H](O)[C@@H](N)C(=O)O"        # (2R,3S)
L_ALLO_THR = "C[C@H](O)[C@H](N)C(=O)O"    # (2S,3S)
D_ALLO_THR = "C[C@@H](O)[C@@H](N)C(=O)O"  # (2R,3R)
FLAT_THR = "CC(O)C(N)C(=O)O"

RR_TART = "O[C@@H](C(=O)O)[C@@H](O)C(=O)O"
SS_TART = "O[C@H](C(=O)O)[C@H](O)C(=O)O"
MESO_TART = "O[C@H](C(=O)O)[C@@H](O)C(=O)O"


def same_core_different_stereo(a, b):
    assert a.core_key == b.core_key
    assert a.stereo_key != b.stereo_key


def all_different_variants(results: dict):
    cores = {r.core_key for r in results.values()}
    assert len(cores) == 1, "stereo variants must share one CORE"
    for (na, a), (nb, b) in itertools.combinations(results.items(), 2):
        assert keys(a) != keys(b), f"{na} and {nb} are the same STEREO variant"


# ---------------------------------------------------------------------------
# rule 3: stereoisomers share a CORE and differ in STEREO
# ---------------------------------------------------------------------------

def test_enantiomers_smiles():
    r, s = sm(R_ALA), sm(S_ALA)
    same_core_different_stereo(r, s)
    assert r.stereo_mode == s.stereo_mode == "abs"
    assert r.inchikey != s.inchikey


def test_enantiomers_molfile():
    r, s = mf(molblock(R_ALA)), mf(molblock(S_ALA))
    same_core_different_stereo(r, s)
    assert r.stereo_mode == s.stereo_mode == "abs"


def test_sulfoxide_enantiomers():
    same_core_different_stereo(sm("C[S@@](=O)c1ccccc1"), sm("C[S@](=O)c1ccccc1"))


def test_four_stereoisomers_of_threonine():
    all_different_variants({
        "L-thr": sm(L_THR), "D-thr": sm(D_THR),
        "L-allo": sm(L_ALLO_THR), "D-allo": sm(D_ALLO_THR),
    })
    for smi in (L_THR, D_THR, L_ALLO_THR, D_ALLO_THR):
        assert sm(smi).stereo_mode == "abs"


def test_diastereomers_molfile():
    same_core_different_stereo(mf(molblock(L_THR)), mf(molblock(L_ALLO_THR)))


def test_tartaric_acids():
    all_different_variants({"RR": sm(RR_TART), "SS": sm(SS_TART), "meso": sm(MESO_TART)})
    assert_same_compound(sm(MESO_TART), sm(MESO_TARTARIC_OTHER_END), mf(molblock(MESO_TART)))
    assert sm(MESO_TART).stereo_mode == "abs"


@pytest.mark.parametrize("e, z", [
    ("C/C=C/C", "C/C=C\\C"),
    ("OC/C=C/C", "OC/C=C\\C"),
    ("OC(=O)/C=C/C(=O)O", "OC(=O)/C=C\\C(=O)O"),       # fumaric / maleic acid
])
def test_e_z_isomers(e, z):
    same_core_different_stereo(sm(e), sm(z))
    same_core_different_stereo(mf(molblock(e)), mf(molblock(z)))
    assert sm(e).stereo_mode == sm(z).stereo_mode == "abs"


def test_ring_cis_trans_isomers():
    same_core_different_stereo(sm("C[C@H]1CC[C@@H](C)CC1"), sm("C[C@H]1CC[C@H](C)CC1"))
    same_core_different_stereo(sm("C[C@H]1CC[C@@H]1C"), sm("C[C@H]1CC[C@H]1C"))


def test_crossed_double_bond_is_undefined_e_z():
    crossed = mf(read_fixture("butenol_crossed_double_bond.mol"))
    assert crossed.stereo_mode == "undefined"
    assert_same_compound(crossed, sm("OCC=CC"), mf(molblock("OCC=CC")))
    all_different_variants({"crossed": crossed, "E": sm("OC/C=C/C"), "Z": sm("OC/C=C\\C")})


# ---------------------------------------------------------------------------
# rule 4: abs / rac / rel / undefined are distinct variants
# ---------------------------------------------------------------------------

def test_abs_rac_rel_undefined_are_four_variants():
    variants = {
        "abs": sm(R_ALA),
        "rac": sm(R_ALA, "rac"),
        "rel": sm(R_ALA, "rel"),
        "undefined": sm(FLAT_ALA),
    }
    all_different_variants(variants)
    assert {k: v.stereo_mode for k, v in variants.items()} == {
        "abs": "abs", "rac": "rac", "rel": "rel", "undefined": "undefined"}


def test_abs_rac_rel_undefined_four_variants_molfile():
    mb = molblock(R_ALA)
    all_different_variants({m: mf(mb, m) for m in ("abs", "rac", "rel", "undefined")})


def test_racemate_does_not_depend_on_drawn_enantiomer():
    assert_same_compound(
        sm(R_ALA, "rac"),
        sm(S_ALA, "rac"),
        sm(R_ALA + " |&1:1|"),
        sm(S_ALA + " |&1:1|"),
        mf(molblock(R_ALA + " |&1:1|", v3000=True)),
        mf(molblock(S_ALA + " |&1:1|", v3000=True)),
        mf(molblock(S_ALA), "rac"),
        mf(read_fixture("chemdraw_L_alanine_chiral0.mol"), "rac"),
    )
    assert sm(R_ALA + " |&1:1|").stereo_mode == "rac"


def test_relative_does_not_depend_on_drawn_enantiomer():
    assert_same_compound(
        sm(R_ALA, "rel"),
        sm(S_ALA, "rel"),
        sm(R_ALA + " |o1:1|"),
        sm(S_ALA + " |o1:1|"),
        mf(molblock(S_ALA + " |o1:1|", v3000=True)),
        mf(molblock(R_ALA), "rel"),
    )
    assert sm(S_ALA + " |o1:1|").stereo_mode == "rel"


def test_rac_and_rel_differ_from_each_other_and_from_both_enantiomers():
    all_different_variants({
        "R": sm(R_ALA), "S": sm(S_ALA), "rac": sm(R_ALA + " |&1:1|"),
        "rel": sm(R_ALA + " |o1:1|"), "undefined": sm(FLAT_ALA),
    })


def test_detected_mode_and_override():
    n = sm(R_ALA, "rac")
    assert n.detected_mode == "abs" and n.stereo_mode == "rac"
    assert n.mode_overridden is True
    n = sm(R_ALA + " |&1:1|")
    assert n.detected_mode == "rac" and n.stereo_mode == "rac"
    n = mf(molblock(R_ALA + " |o1:1|", v3000=True))
    assert n.detected_mode == "rel" and n.stereo_mode == "rel"
    assert sm(R_ALA).detected_mode == "abs"
    assert sm(FLAT_ALA).detected_mode == "undefined"


@pytest.mark.parametrize("flag", [0, 1])
def test_v2000_chiral_flag_is_ignored(flag):
    text = set_chiral_flag(read_fixture("chemdraw_L_alanine_chiral0.mol"), flag)
    n = mf(text)
    assert n.detected_mode == "abs" and n.stereo_mode == "abs"
    assert_same_compound(n, sm(S_ALA), mf(set_chiral_flag(molblock(S_ALA), 1 - flag)))


def test_v3000_explicit_abs_collection_equals_plain_drawing():
    assert_same_compound(
        mf(molblock(S_ALA + " |a:1|", v3000=True)),
        mf(molblock(S_ALA, v3000=True)),
        mf(molblock(S_ALA)),
        sm(S_ALA),
    )


def test_override_replaces_enhanced_stereo_from_the_file():
    rac_file = molblock(S_ALA + " |&1:1|", v3000=True)
    assert_same_compound(mf(rac_file, "abs"), sm(S_ALA))
    assert_same_compound(mf(rac_file, "rel"), sm(R_ALA + " |o1:1|"))
    assert_same_compound(mf(rac_file, "undefined"), sm(FLAT_ALA))
    assert mf(rac_file, "abs").detected_mode == "rac"


# --- undefined --------------------------------------------------------------

def test_undefined_override_equals_flat_drawing():
    assert_same_compound(
        sm(FLAT_ALA),
        sm(R_ALA, "undefined"),
        sm(S_ALA, "undefined"),
        sm(R_ALA + " |&1:1|", "undefined"),
        mf(strip_wedges(molblock(R_ALA))),
        mf(molblock(R_ALA), "undefined"),
        mf(read_fixture("chemdraw_L_alanine_chiral0.mol"), "undefined"),
    )
    assert sm(FLAT_ALA).stereo_mode == "undefined"


def test_undefined_override_two_centres():
    flat = sm(FLAT_THR)
    assert flat.stereo_mode == "undefined"
    for smi in (L_THR, D_THR, L_ALLO_THR):
        assert_same_compound(sm(smi, "undefined"), flat, mf(molblock(smi), "undefined"))
    assert_same_compound(mf(strip_wedges(molblock(L_THR))), flat)


def test_wavy_bond_is_undefined():
    mb = strip_wedges(molblock("C[C@H](O)CC"))
    # atom 2 is the stereocentre in RDKit's output for this SMILES
    wavy = set_bond_stereo(mb, 2, 1, 4)
    assert_same_compound(mf(wavy), sm("CC(O)CC"), mf(mb))
    assert mf(wavy).stereo_mode == "undefined"


def test_undefined_override_keeps_e_z():
    e_wedged = "C/C=C/[C@H](O)CC"
    e_flat = "C/C=C/C(O)CC"
    z_flat = "C/C=C\\C(O)CC"
    assert_same_compound(sm(e_wedged, "undefined"), sm(e_flat), mf(molblock(e_wedged), "undefined"),
                         mf(molblock(e_flat)))
    assert keys(sm(e_wedged, "undefined")) != keys(sm(z_flat))
    assert keys(sm("C/C=C\\[C@H](O)CC", "undefined")) == keys(sm(z_flat))


def test_undefined_override_on_pure_e_z_compound_keeps_geometry():
    e = sm("OC/C=C/C", "undefined")
    z = sm("OC/C=C\\C", "undefined")
    assert keys(e) != keys(z)
    assert keys(e) != keys(sm("OCC=CC"))
    # nothing is wedge-defined, so the override has nothing to drop
    assert keys(e) == keys(sm("OC/C=C/C"))


# --- rac/rel/abs on achiral and meso compounds -------------------------------

@pytest.mark.parametrize("smiles", ["c1ccccc1", "CCO", "NCc1ccccc1.Cl", "CC(C)C(=O)O",
                                    "C1CCCCC1", "CC(C)=CC"])
def test_no_stereo_element_is_mode_none_whatever_the_override(smiles):
    base = sm(smiles)
    assert base.stereo_mode == "none"
    for mode in ("abs", "rac", "rel", "undefined"):
        n = sm(smiles, mode)
        assert n.stereo_mode == "none"
        assert keys(n) == keys(base)
    assert keys(mf(molblock(smiles), "rac")) == keys(base)


@pytest.mark.parametrize("smiles", [MESO_TART, "C[C@H]1CC[C@@H](C)CC1", "C[C@H]1CC[C@H](C)CC1",
                                    "C/C=C/C", "OC(=O)/C=C\\C(=O)O", "C[C@H]1CC[C@H]1C"])
def test_rac_and_rel_on_achiral_compound_with_stereo_elements_equal_abs(smiles):
    ref = sm(smiles)
    assert ref.stereo_mode == "abs"
    for mode in ("abs", "rac", "rel"):
        assert keys(sm(smiles, mode)) == keys(ref), mode


def test_meso_undefined_is_different_from_meso():
    assert keys(sm(MESO_TART, "undefined")) == keys(sm("OC(C(=O)O)C(O)C(=O)O"))
    assert keys(sm(MESO_TART, "undefined")) != keys(sm(MESO_TART))


def test_racemic_tartaric_acid_is_not_meso():
    all_different_variants({
        "rac": sm(RR_TART, "rac"), "meso": sm(MESO_TART), "RR": sm(RR_TART), "SS": sm(SS_TART),
    })
    assert_same_compound(sm(RR_TART, "rac"), sm(SS_TART, "rac"))


# --- two centres, whole-molecule groups ---------------------------------------

def test_racemic_diastereomer():
    rac_thr = sm(L_THR, "rac")
    assert_same_compound(
        rac_thr,
        sm(D_THR, "rac"),
        sm(L_THR + " |&1:1,3|"),
        sm(D_THR + " |&1:1,3|"),
        mf(molblock(D_THR + " |&1:1,3|", v3000=True)),
    )
    assert rac_thr.stereo_mode == "rac"
    all_different_variants({
        "rac-thr": rac_thr, "rac-allo": sm(L_ALLO_THR, "rac"),
        "L-thr": sm(L_THR), "D-thr": sm(D_THR), "undefined": sm(FLAT_THR),
    })


def test_relative_diastereomer():
    rel_thr = sm(L_THR, "rel")
    assert_same_compound(rel_thr, sm(D_THR, "rel"), sm(L_THR + " |o1:1,3|"), sm(D_THR + " |o1:1,3|"))
    assert rel_thr.stereo_mode == "rel"
    all_different_variants({
        "rel-thr": rel_thr, "rel-allo": sm(L_ALLO_THR, "rel"), "rac-thr": sm(L_THR, "rac"),
        "L-thr": sm(L_THR),
    })


def test_independent_and_groups_are_a_different_variant_from_one_group():
    one_group = sm(L_THR + " |&1:1,3|")
    two_groups = sm(L_THR + " |&1:1,&2:3|")
    assert one_group.core_key == two_groups.core_key
    assert keys(one_group) != keys(two_groups)
    # group numbers are labels only
    assert keys(sm(L_THR + " |&2:1,&1:3|")) == keys(two_groups)
    # two independent AND groups describe all four stereoisomers: drawn diastereomer irrelevant
    assert keys(sm(L_ALLO_THR + " |&1:1,&2:3|")) == keys(two_groups)


def test_meso_compound_with_and_group_equals_meso():
    """rac on a meso compound is the same variant as abs - also when it comes from the file."""
    # stereocentres of MESO_TART are atoms 1 and 5
    assert keys(sm(MESO_TART + " |&1:1,5|")) == keys(sm(MESO_TART))
    assert keys(mf(molblock(MESO_TART + " |&1:1,5|", v3000=True))) == keys(sm(MESO_TART))
    assert keys(sm(MESO_TART + " |o1:1,5|")) == keys(sm(MESO_TART))


def test_racemic_salt_does_not_depend_on_drawn_enantiomer():
    r_hcl, s_hcl = "C[C@@H](N)c1ccccc1.Cl", "C[C@H](N)c1ccccc1.Cl"
    assert_same_compound(sm(r_hcl, "rac"), sm(s_hcl, "rac"), sm(r_hcl + " |&1:1|"),
                         mf(read_fixture("sciELN_R_1_phenylethylammonium_chloride.mol"), "rac"))
    assert keys(sm(r_hcl, "rac")) != keys(sm("C[C@@H](N)c1ccccc1", "rac"))


AMINE_R = "C[C@@H](N)c1ccccc1"
AMINE_S = "C[C@H](N)c1ccccc1"
MANDELIC_R = "O[C@@H](C(=O)O)c1ccccc1"
MANDELIC_S = "O[C@H](C(=O)O)c1ccccc1"


def test_diastereomeric_salts():
    all_different_variants({
        "R-amine R-acid": sm(f"{AMINE_R}.{MANDELIC_R}"),
        "R-amine S-acid": sm(f"{AMINE_R}.{MANDELIC_S}"),
        "S-amine S-acid": sm(f"{AMINE_S}.{MANDELIC_S}"),
        "S-amine R-acid": sm(f"{AMINE_S}.{MANDELIC_R}"),
    })
    # the racemic salt: (R,R) + its mirror image (S,S); different from the (R,S)/(S,R) racemic salt
    assert_same_compound(sm(f"{AMINE_R}.{MANDELIC_R}", "rac"), sm(f"{AMINE_S}.{MANDELIC_S}", "rac"))
    assert keys(sm(f"{AMINE_R}.{MANDELIC_R}", "rac")) != keys(sm(f"{AMINE_R}.{MANDELIC_S}", "rac"))
    # fragment order irrelevant
    assert keys(sm(f"{MANDELIC_S}.{AMINE_R}")) == keys(sm(f"{AMINE_R}.{MANDELIC_S}"))


def test_salt_with_two_chiral_cations():
    rr = sm(f"{AMINE_R}.{AMINE_R}.OS(=O)(=O)O")
    rs = sm(f"{AMINE_R}.{AMINE_S}.OS(=O)(=O)O")
    ss = sm(f"{AMINE_S}.{AMINE_S}.OS(=O)(=O)O")
    all_different_variants({"RR": rr, "RS": rs, "SS": ss})
    assert keys(sm(f"{AMINE_S}.OS(=O)(=O)O.{AMINE_R}")) == keys(rs)
    assert_same_compound(sm(f"{AMINE_R}.{AMINE_R}.OS(=O)(=O)O", "rac"),
                         sm(f"{AMINE_S}.{AMINE_S}.OS(=O)(=O)O", "rac"))
    assert keys(sm(f"{AMINE_R}.{AMINE_R}.OS(=O)(=O)O", "rac")) != keys(rs)


def test_salt_of_both_enantiomers_is_its_own_mirror_image():
    """(R)-amine.(S)-amine.H2SO4 is superimposable on its mirror image (like a meso
    compound), so 'drawn structure + mirror image' is the drawn structure itself."""
    rs = f"{AMINE_R}.{AMINE_S}.OS(=O)(=O)O"
    assert keys(sm(rs, "rac")) == keys(sm(rs))


def test_racemate_keeps_e_z():
    r_e = "C/C=C/[C@H](O)CC"
    s_e = "C/C=C/[C@@H](O)CC"
    r_z = "C/C=C\\[C@H](O)CC"
    assert_same_compound(sm(r_e, "rac"), sm(s_e, "rac"), sm(r_e + " |&1:3|"))
    assert keys(sm(r_e, "rac")) != keys(sm(r_z, "rac"))
    assert keys(sm(r_e, "rac")) != keys(sm(r_e))


# --- per-centre groups: epimeric mixture -------------------------------------

EPI_A = "C[C@H](O)[C@@H](C)N"   # centres at atoms 1 and 3
EPI_B = "C[C@H](O)[C@H](C)N"    # same centre 1, other configuration at 3


def test_epimeric_mixture_is_its_own_variant():
    mix = sm(EPI_A + " |a:1,&1:3|")
    assert mix.stereo_mode == "mixed"
    assert_same_compound(
        mix,
        sm(EPI_B + " |a:1,&1:3|"),                       # drawn configuration at the AND centre irrelevant
        mf(molblock(EPI_A + " |a:1,&1:3|", v3000=True)),
        mf(molblock(EPI_B + " |a:1,&1:3|", v3000=True)),
    )
    all_different_variants({
        "mixture": mix,
        "pure A": sm(EPI_A),
        "pure B": sm(EPI_B),
        "rac A": sm(EPI_A + " |&1:1,3|"),
        "rac B": sm(EPI_B + " |&1:1,3|"),
        "other ABS centre": sm("C[C@@H](O)[C@@H](C)N |a:1,&1:3|"),
        "undefined": sm("CC(O)C(C)N"),
        "partial": sm("C[C@H](O)C(C)N"),
    })


def test_epimeric_mixture_ungrouped_centre_is_abs():
    """In a file with stereo groups, a defined centre not in any group is ABS."""
    assert keys(sm(EPI_A + " |&1:3|")) == keys(sm(EPI_A + " |a:1,&1:3|"))


def test_epimeric_mixture_with_or_group():
    mix_or = sm(EPI_A + " |a:1,o1:3|")
    assert mix_or.stereo_mode == "mixed"
    assert keys(sm(EPI_B + " |a:1,o1:3|")) == keys(mix_or)
    assert keys(mix_or) != keys(sm(EPI_A + " |a:1,&1:3|"))


# --- partial ------------------------------------------------------------------

def test_partially_defined_stereo():
    p1 = sm("C[C@H](O)C(C)N")
    p2 = sm("C[C@@H](O)C(C)N")
    p3 = sm("CC(O)[C@H](C)N")
    assert p1.stereo_mode == p2.stereo_mode == p3.stereo_mode == "partial"
    all_different_variants({
        "partial-1": p1, "partial-2": p2, "partial-3": p3,
        "undefined": sm("CC(O)C(C)N"), "abs A": sm(EPI_A), "abs B": sm(EPI_B),
    })
    assert_same_compound(p1, mf(molblock("C[C@H](O)C(C)N")))


# ---------------------------------------------------------------------------
# atropisomers
# ---------------------------------------------------------------------------

def test_atropisomers_share_core_and_inchikey_but_differ_in_stereo():
    p = mf(read_fixture("atropisomer_P_wedge.mol"))
    m = mf(read_fixture("atropisomer_M_hash.mol"))
    assert p.core_key == m.core_key
    assert p.inchikey == m.inchikey
    assert p.stereo_key != m.stereo_key
    assert p.stereo_mode == m.stereo_mode == "abs"


def test_atropisomer_equivalent_wedge_choice():
    assert_same_compound(mf(read_fixture("atropisomer_P_wedge.mol")),
                         mf(read_fixture("atropisomer_P_alt_hash.mol")))


def test_atropisomer_renumbered():
    for name in ("atropisomer_P_wedge.mol", "atropisomer_M_hash.mol", "atropisomer_P_alt_hash.mol"):
        text = read_fixture(name)
        assert keys(mf(renumber_v2000(text, ATROP_RENUMBER))) == keys(mf(text)), name


def test_atropisomer_mirrored_drawing_is_the_other_atropisomer():
    p_text = read_fixture("atropisomer_P_wedge.mol")
    assert keys(mf(transform_coords(p_text, mirror_x))) == keys(mf(read_fixture("atropisomer_M_hash.mol")))
    assert keys(mf(transform_coords(p_text, mirror_x, swap_wedges=True))) == keys(mf(p_text))


def test_atropisomer_flat_is_a_third_variant():
    p = mf(read_fixture("atropisomer_P_wedge.mol"))
    m = mf(read_fixture("atropisomer_M_hash.mol"))
    flat = mf(read_fixture("atropisomer_flat.mol"))
    assert flat.core_key == p.core_key
    all_different_variants({"P": p, "M": m, "flat": flat})


def test_atropisomer_racemate_does_not_depend_on_drawn_atropisomer():
    p = read_fixture("atropisomer_P_wedge.mol")
    m = read_fixture("atropisomer_M_hash.mol")
    assert_same_compound(mf(p, "rac"), mf(m, "rac"), mf(read_fixture("atropisomer_P_alt_hash.mol"), "rac"))
    assert keys(mf(p, "rac")) != keys(mf(p))


def test_atropisomer_undefined_override_equals_flat_drawing():
    assert_same_compound(mf(read_fixture("atropisomer_P_wedge.mol"), "undefined"),
                         mf(read_fixture("atropisomer_M_hash.mol"), "undefined"),
                         mf(read_fixture("atropisomer_flat.mol")))


# ---------------------------------------------------------------------------
# stereo survives the drawing-difference normalisations
# ---------------------------------------------------------------------------

def test_stereo_variant_independent_of_route_and_drawing():
    ref = sm(L_THR)
    for mb in (molblock(L_THR), molblock(L_THR, aromatic_bonds=True), molblock(L_THR, explicit_h=True),
               molblock(L_THR, renumber_seed=11), molblock(L_THR, v3000=True)):
        assert keys(mf(mb)) == keys(ref)
