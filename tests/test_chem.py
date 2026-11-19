"""Independent tests of compound_registry.chem.normalize / normalize_smiles.

Written from the owner's chemistry rules, not from the implementation. Tests
compare keys of different drawings with each other ("same CORE",
"same compound" = same core_key + stereo_key + stereo_mode); they never pin
the text of stereo_key and only pin InChI where the rules do.

Stereo-specific rules (enantiomers, enhanced stereo, modes, atropisomers) are
in test_chem_stereo.py; checks of the test inputs themselves are in
test_chem_fixtures.py.
"""

from __future__ import annotations

import itertools
import re

import pytest

from chem_helpers import (
    keys,
    kekule_molblock,
    mirror_x,
    molblock,
    molblock_3d,
    read_fixture,
    renumber_v2000,
    replace_atom_symbol,
    rotate,
    transform_coords,
)
from compound_registry.chem import normalize, normalize_smiles
from compound_registry.errors import ChemistryError
from compound_registry.models import STEREO_MODES, Normalized
from test_chem_fixtures import deuterium_symbol_molblock


# ---------------------------------------------------------------------------
# wrappers: every result goes through the generic contract checks
# ---------------------------------------------------------------------------

INCHIKEY_RE = re.compile(r"[A-Z]{14}-[A-Z]{10}-[A-Z]")


def check_contract(n: Normalized) -> Normalized:
    assert isinstance(n, Normalized)
    assert n.core_key.startswith("InChI=1S/"), n.core_key
    stereo_layers = [layer for layer in n.core_key.split("/")[1:] if layer[:1] in ("t", "b", "m", "s")]
    assert not stereo_layers, f"core_key has stereo layers {stereo_layers}: {n.core_key}"
    assert isinstance(n.stereo_key, str) and n.stereo_key, "stereo_key must be a non-empty string"
    assert n.stereo_mode in STEREO_MODES
    assert INCHIKEY_RE.fullmatch(n.inchikey), n.inchikey
    assert isinstance(n.smiles, str) and n.smiles
    return n


def mf(molfile: str, mode: str | None = None) -> Normalized:
    return check_contract(normalize(molfile, mode))


def sm(smiles: str, mode: str | None = None) -> Normalized:
    return check_contract(normalize_smiles(smiles, mode))


def assert_same_compound(*results: Normalized):
    first = keys(results[0])
    for r in results[1:]:
        assert keys(r) == first


def assert_all_cores_differ(results: dict[str, Normalized]):
    for (na, a), (nb, b) in itertools.combinations(results.items(), 2):
        assert a.core_key != b.core_key, f"{na} and {nb} share a CORE"


# ---------------------------------------------------------------------------
# basic contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("smiles", [
    "C[C@H](N)C(=O)O",
    "C/C=C/C",
    "O[C@H](C(=O)O)[C@@H](O)C(=O)O",
    "[2H][C@@H](C)O",                     # isotopic stereo lives in the /i sub-layers
    "C[C@H](N)C(=O)O |&1:1|",
    "C[C@@H](N)c1ccccc1.Cl",
    "c1ccccc1",
])
def test_core_key_is_standard_inchi_without_stereo_layers(smiles):
    sm(smiles)  # check_contract does the work


def test_core_key_of_atropisomer_has_no_stereo_layers():
    mf(read_fixture("atropisomer_P_wedge.mol"))


def test_molblock_is_the_input_verbatim():
    for name in ("sciELN_R_1_phenylethylammonium_chloride.mol", "chemdraw_L_alanine_chiral0.mol",
                 "nitrotoluene_pentavalent.mol", "atropisomer_P_wedge.mol"):
        text = read_fixture(name)
        assert normalize(text).molblock == text
        crlf = text.replace("\n", "\r\n")
        assert normalize(crlf).molblock == crlf


def test_molblock_generated_for_smiles_input():
    n = sm("C[C@H](N)C(=O)O")
    assert n.molblock.strip()
    # the generated molfile describes the same compound
    assert keys(mf(n.molblock)) == keys(n)


def test_explicit_none_override_is_no_override():
    assert sm("C[C@H](N)C(=O)O", None) == normalize_smiles("C[C@H](N)C(=O)O")


@pytest.mark.parametrize("bad", ["racemic", "ABS", "Rac", "", " abs", "none", "partial", "mixed",
                                 "meso", "R", "and", "or"])
def test_invalid_mode_override(bad):
    with pytest.raises(ChemistryError) as exc:
        normalize_smiles("C[C@H](N)C(=O)O", bad)
    assert exc.value.code == "invalid_mode"
    with pytest.raises(ChemistryError) as exc:
        normalize(molblock("C[C@H](N)C(=O)O"), bad)
    assert exc.value.code == "invalid_mode"


def test_invalid_mode_override_on_achiral_structure():
    with pytest.raises(ChemistryError) as exc:
        normalize_smiles("c1ccccc1", "racemic")
    assert exc.value.code == "invalid_mode"


# ---------------------------------------------------------------------------
# rule 6: drawing differences do not create new compounds
# ---------------------------------------------------------------------------

def test_kekule_vs_aromatic_molfile():
    assert_same_compound(
        mf(molblock("Cc1ccc(O)cc1")),                       # Kekule bonds
        mf(molblock("Cc1ccc(O)cc1", aromatic_bonds=True)),   # bond type 4
        mf(kekule_molblock("CC1=CC=C(O)C=C1")),
        mf(kekule_molblock("CC1C=CC(O)=CC=1")),              # the other Kekule structure
    )


def test_kekule_vs_aromatic_smiles():
    assert_same_compound(sm("Cc1ccccc1"), sm("CC1=CC=CC=C1"), sm("CC1C=CC=CC=1"))
    assert_same_compound(sm("c1ccc2ccccc2c1"), sm("C1=CC=C2C=CC=CC2=C1"))
    assert_same_compound(sm("Cn1ccnc1"), sm("CN1C=CN=C1"))


def test_kekule_vs_aromatic_with_stereocentre():
    assert_same_compound(
        mf(molblock("C[C@@H](N)c1ccccc1")),
        mf(molblock("C[C@@H](N)c1ccccc1", aromatic_bonds=True)),
        sm("C[C@@H](N)C1=CC=CC=C1"),
    )


def test_explicit_vs_implicit_hydrogens_smiles():
    assert_same_compound(sm("CCO"), sm("[CH3][CH2][OH]"), sm("[H]OC([H])([H])C([H])([H])[H]"))
    assert_same_compound(sm("C[C@H](N)C(=O)O"), sm("[H][C@@](C)(N)C(=O)O"))


def test_explicit_vs_implicit_hydrogens_molfile():
    assert_same_compound(mf(molblock("CCO")), mf(molblock("CCO", explicit_h=True)))


def test_explicit_hydrogens_molfile_keeps_stereo():
    # with explicit H RDKit may put the wedge on the C-H bond: still the same compound
    for smi in ("C[C@H](N)C(=O)O", "C[C@@H](O)[C@H](N)C(=O)O"):
        assert_same_compound(mf(molblock(smi)), mf(molblock(smi, explicit_h=True)), sm(smi))


def test_nitro_pentavalent_vs_charge_separated():
    assert_same_compound(
        mf(read_fixture("nitrotoluene_pentavalent.mol")),
        mf(molblock("Cc1ccc([N+](=O)[O-])cc1")),
        sm("Cc1ccc([N+](=O)[O-])cc1"),
        sm("Cc1ccc(N(=O)=O)cc1"),
    )


@pytest.mark.parametrize("smiles", [
    "C[C@H](N)C(=O)O",
    "C[C@@H](O)[C@H](N)C(=O)O",
    "OC/C=C/C",
    "C[C@@H](N)c1ccccc1.Cl",
    "O=C(O)c1ccccc1O",
])
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_atom_order_does_not_matter(smiles, seed):
    assert keys(mf(molblock(smiles, renumber_seed=seed))) == keys(mf(molblock(smiles)))


def test_hand_renumbered_lab_file():
    text = read_fixture("sciELN_R_1_phenylethylammonium_chloride.mol")
    shuffled = renumber_v2000(text, [10, 4, 9, 2, 8, 3, 7, 1, 6, 5])
    assert keys(mf(shuffled)) == keys(mf(text))


@pytest.mark.parametrize("smiles", ["C[C@H](N)C(=O)O", "C[C@@H](O)[C@H](N)C(=O)O", "OC/C=C/C",
                                    "OC/C=C\\C"])
def test_coordinates_rotation_translation_scale_do_not_matter(smiles):
    base = molblock(smiles)
    ref = keys(mf(base))
    assert keys(mf(transform_coords(base, rotate(137, 12.0, -40.0)))) == ref
    assert keys(mf(transform_coords(base, rotate(-90)))) == ref
    assert keys(mf(transform_coords(base, lambda x, y, z: (30 * x, 30 * y, 30 * z)))) == ref


@pytest.mark.parametrize("smiles", ["C[C@H](N)C(=O)O", "C[C@@H](O)[C@H](N)C(=O)O"])
def test_mirrored_drawing_with_inverted_wedges_is_the_same_compound(smiles):
    base = molblock(smiles)
    assert keys(mf(transform_coords(base, mirror_x, swap_wedges=True))) == keys(mf(base))


def test_mirrored_drawing_with_same_wedges_is_the_enantiomer():
    base = molblock("C[C@H](N)C(=O)O")
    mirrored = mf(transform_coords(base, mirror_x))
    assert keys(mirrored) == keys(sm("C[C@@H](N)C(=O)O"))
    assert keys(mirrored) != keys(mf(base))


def test_mirrored_e_double_bond_is_still_e():
    base = molblock("OC/C=C/C")
    assert keys(mf(transform_coords(base, mirror_x))) == keys(sm("OC/C=C/C"))


@pytest.mark.parametrize("smiles", ["C[C@H](N)C(=O)O", "C[C@@H](O)[C@H](N)C(=O)O", "OC/C=C/C"])
def test_3d_molfile_equals_2d_drawing(smiles):
    assert keys(mf(molblock_3d(smiles))) == keys(mf(molblock(smiles))) == keys(sm(smiles))


def test_crlf_line_endings_do_not_matter():
    for name in ("sciELN_R_1_phenylethylammonium_chloride.mol", "chemdraw_L_alanine_chiral0.mol",
                 "atropisomer_M_hash.mol"):
        text = read_fixture(name)
        assert keys(mf(text.replace("\n", "\r\n"))) == keys(mf(text))


# --- tautomers --------------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("O=c1cccc[nH]1", "Oc1ccccn1"),               # 2-pyridone / 2-hydroxypyridine
    ("O=C1C=CC=CN1", "OC1=NC=CC=C1"),             # same, Kekule SMILES
    ("CC(=O)N", "CC(O)=N"),                       # amide / imidic acid
    ("CC(=O)NC", "CC(O)=NC"),
    ("Cc1c[nH]cn1", "Cc1cnc[nH]1"),               # 4-/5-methylimidazole
    ("Oc1ncccn1", "O=c1nccc[nH]1"),               # 2-hydroxypyrimidine / pyrimidin-2-one
])
def test_mobile_h_tautomers_are_the_same_compound(a, b):
    assert_same_compound(sm(a), sm(b))


def test_mobile_h_tautomers_molfile_route():
    assert_same_compound(mf(molblock("O=c1cccc[nH]1")), mf(molblock("Oc1ccccn1")))
    assert_same_compound(mf(molblock("CC(=O)N")), mf(molblock("CC(O)=N")))


def test_mobile_h_tautomer_with_stereocentre_keeps_stereo():
    a = sm("C[C@H](N)C(=O)N")
    b = sm("C[C@H](N)C(O)=N")
    assert_same_compound(a, b)
    assert a.stereo_mode == "abs"


@pytest.mark.parametrize("keto, enol", [
    ("CC(C)=O", "CC(O)=C"),                       # acetone / prop-1-en-2-ol
    ("O=C1CCCCC1", "OC1=CCCCC1"),                 # cyclohexanone / cyclohex-1-enol
    ("CC(=O)CC(C)=O", "CC(O)=CC(C)=O"),           # acetylacetone keto / enol
])
def test_keto_enol_tautomers_are_different_cores_because_standard_inchi_does_not_merge_them(keto, enol):
    assert sm(keto).core_key != sm(enol).core_key


# ---------------------------------------------------------------------------
# rule 2: salts and solvates are different compounds, nothing is stripped
# ---------------------------------------------------------------------------

def test_free_base_and_salt_forms_are_different_cores():
    base = "c1ccc(cc1)N1CCNCC1"   # 1-phenylpiperazine
    forms = {
        "free base": sm(base),
        "HCl": sm(base + ".Cl"),
        "2 HCl": sm(base + ".Cl.Cl"),
        "hemi-HCl (2:1)": sm(f"{base}.{base}.Cl"),
        "TFA": sm(base + ".OC(=O)C(F)(F)F"),
        "2 TFA": sm(base + ".OC(=O)C(F)(F)F.OC(=O)C(F)(F)F"),
        "HBr": sm(base + ".Br"),
        "hydrate": sm(base + ".O"),
        "HCl hydrate": sm(base + ".Cl.O"),
        "methanol solvate": sm(base + ".CO"),
    }
    assert_all_cores_differ(forms)


def test_salt_forms_are_different_cores_molfile_route():
    base = "c1ccc(cc1)N1CCNCC1"
    forms = {name: mf(molblock(s)) for name, s in {
        "free base": base, "HCl": base + ".Cl", "2 HCl": base + ".Cl.Cl",
        "TFA": base + ".OC(=O)C(F)(F)F", "hydrate": base + ".O",
    }.items()}
    assert_all_cores_differ(forms)


def test_duplicated_fragment_is_kept():
    """Nothing is stripped or de-duplicated: two copies of a fragment are a different entry."""
    assert sm("CC(=O)O.CC(=O)O").core_key != sm("CC(=O)O").core_key
    assert sm("NCc1ccccc1.Cl.Cl").core_key != sm("NCc1ccccc1.Cl").core_key


def test_carboxylate_salt_is_not_the_acid():
    assert_all_cores_differ({
        "acid": sm("CC(=O)O"),
        "Na salt": sm("CC(=O)[O-].[Na+]"),
        "K salt": sm("CC(=O)[O-].[K+]"),
    })


@pytest.mark.parametrize("variant", [
    "NCc1ccccc1.Cl",                 # neutral amine + HCl
    "Cl.NCc1ccccc1",                 # fragment order
    "[NH3+]Cc1ccccc1.[Cl-]",         # ammonium chloride
    "[Cl-].[NH3+]Cc1ccccc1",
    "[Cl-].NCc1ccccc1.[H+]",         # how the lab notebook server writes it
    "[H+].[Cl-].NCc1ccccc1",
])
def test_hydrochloride_drawing_variants_are_one_compound(variant):
    assert keys(sm(variant)) == keys(sm("NCc1ccccc1.Cl"))


def test_hydrochloride_drawing_variants_molfile_route():
    assert_same_compound(
        mf(molblock("NCc1ccccc1.Cl")),
        mf(molblock("[NH3+]Cc1ccccc1.[Cl-]")),
        mf(molblock("[Cl-].NCc1ccccc1.[H+]")),
        sm("NCc1ccccc1.Cl"),
    )


def test_dihydrochloride_drawing_variants_are_one_compound():
    base = "c1ccc(cc1)N1CCNCC1"
    two_hcl = sm(base + ".Cl.Cl")
    assert_same_compound(
        two_hcl,
        sm("c1ccc(cc1)N1CC[NH2+]CC1.[Cl-].Cl"),
        sm("[Cl-].[Cl-].c1ccc(cc1)N1CCNCC1.[H+].[H+]"),
        sm("Cl.c1ccc(cc1)N1CCNCC1.Cl"),
    )
    assert sm("[Cl-].c1ccc(cc1)N1CCNCC1.[H+]").core_key != two_hcl.core_key


def test_zwitterion_vs_neutral_amino_acid():
    assert_same_compound(sm("NCC(=O)O"), sm("[NH3+]CC(=O)[O-]"))
    assert_same_compound(sm("C[C@H](N)C(=O)O"), sm("C[C@H]([NH3+])C(=O)[O-]"),
                         mf(molblock("C[C@H]([NH3+])C(=O)[O-]")))


def test_salt_of_a_chiral_amine():
    r_base = sm("C[C@@H](N)c1ccccc1")
    r_hcl = sm("C[C@@H](N)c1ccccc1.Cl")
    s_hcl = sm("C[C@H](N)c1ccccc1.Cl")
    assert r_base.core_key != r_hcl.core_key
    assert r_hcl.core_key == s_hcl.core_key
    assert r_hcl.stereo_key != s_hcl.stereo_key
    assert r_hcl.stereo_mode == s_hcl.stereo_mode == "abs"


def test_fragment_order_does_not_matter_molfile_route():
    assert_same_compound(
        mf(molblock("C[C@@H](N)c1ccccc1.Cl")),
        mf(molblock("Cl.C[C@@H](N)c1ccccc1")),
        mf(molblock("C[C@@H](N)c1ccccc1.Cl", renumber_seed=3)),
    )
    assert_same_compound(mf(molblock("O.c1ccccc1N1CCNCC1.Cl")), mf(molblock("Cl.c1ccccc1N1CCNCC1.O")))


# ---------------------------------------------------------------------------
# rule 5: isotopically labelled compounds are different compounds
# ---------------------------------------------------------------------------

def test_isotopologues_are_different_cores():
    assert_all_cores_differ({
        "CH3": sm("COc1ccccc1"),
        "CD3": sm("[2H]C([2H])([2H])Oc1ccccc1"),
        "CHD2": sm("[2H]C([2H])Oc1ccccc1"),
        "13CH3": sm("[13CH3]Oc1ccccc1"),
        "ring-d5": sm("COc1c([2H])c([2H])c([2H])c([2H])c1[2H]"),
        "18O": sm("C[18O]c1ccccc1"),
    })


def test_isotopologues_molfile_route():
    assert sm("COc1ccccc1").core_key != mf(molblock("[2H]C([2H])([2H])Oc1ccccc1")).core_key
    assert_same_compound(mf(molblock("[2H]C([2H])([2H])Oc1ccccc1")), sm("[2H]C([2H])([2H])Oc1ccccc1"))
    assert_same_compound(mf(molblock("[13CH3]Oc1ccccc1")), sm("[13CH3]Oc1ccccc1"))


def test_deuterium_drawn_as_D_symbol():
    assert_same_compound(mf(deuterium_symbol_molblock()), sm("[2H]C([2H])([2H])Oc1ccccc1"))


def test_deuterated_salt():
    assert sm("[2H]C([2H])([2H])N.Cl").core_key != sm("CN.Cl").core_key
    assert sm("[2H]C([2H])([2H])N.Cl").core_key != sm("[2H]C([2H])([2H])N").core_key


def test_isotopic_stereocentre():
    r = sm("[2H][C@H](C)O")
    s = sm("[2H][C@@H](C)O")
    assert r.core_key == s.core_key
    assert r.core_key != sm("CCO").core_key
    assert r.stereo_key != s.stereo_key


# ---------------------------------------------------------------------------
# molfile as written by the lab notebook server
# ---------------------------------------------------------------------------

def test_lab_notebook_molfile_parses():
    n = mf(read_fixture("sciELN_R_1_phenylethylammonium_chloride.mol"))
    assert n.detected_mode == "abs"   # chiral flag 0 is ignored
    assert n.stereo_mode == "abs"


def test_lab_notebook_molfile_is_the_R_hydrochloride():
    n = mf(read_fixture("sciELN_R_1_phenylethylammonium_chloride.mol"))
    assert_same_compound(
        n,
        sm("C[C@@H](N)c1ccccc1.Cl"),
        sm("C[C@@H]([NH3+])c1ccccc1.[Cl-]"),
        sm("[Cl-].C[C@@H](N)c1ccccc1.[H+]"),
        mf(molblock("C[C@@H](N)c1ccccc1.Cl")),
    )
    s = sm("C[C@H](N)c1ccccc1.Cl")
    assert n.core_key == s.core_key and n.stereo_key != s.stereo_key
    assert n.core_key != sm("C[C@@H](N)c1ccccc1").core_key


def test_lab_notebook_molfile_with_charges_only_in_atom_block():
    text = read_fixture("sciELN_R_1_phenylethylammonium_chloride.mol")
    no_chg_lines = "".join(l for l in text.splitlines(keepends=True) if not l.startswith("M  CHG"))
    assert keys(mf(no_chg_lines)) == keys(mf(text))


def test_lab_notebook_molfile_with_title_line_garbage_smiles():
    """Line 1 is free text; a wrong SMILES there must not influence the result."""
    text = read_fixture("sciELN_R_1_phenylethylammonium_chloride.mol")
    lines = text.splitlines(keepends=True)
    lines[0] = "C[C@H]([NH3+])c1ccccc1.[Cl-]\n"     # the S enantiomer in the title
    assert keys(mf("".join(lines))) == keys(mf(text))


# ---------------------------------------------------------------------------
# molfile route and SMILES route agree
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("smiles", [
    "C[C@H](N)C(=O)O",
    "C[C@@H](O)[C@H](N)C(=O)O",
    "CC(O)CC",
    "C[C@H](O)C(C)N",
    "OC/C=C/C",
    "OC/C=C\\C",
    "C[C@@H](N)c1ccccc1.Cl",
    "O[C@H](C(=O)O)[C@@H](O)C(=O)O",
    "[2H][C@@H](C)O",
    "O=c1cccc[nH]1",
    "C[S@@](=O)c1ccccc1",
    "c1ccc(cc1)N1CCNCC1.Cl.Cl",
])
def test_molfile_and_smiles_routes_agree(smiles):
    assert keys(mf(molblock(smiles))) == keys(sm(smiles))


@pytest.mark.parametrize("cxsmiles", [
    "C[C@H](N)C(=O)O |&1:1|",
    "C[C@H](N)C(=O)O |o1:1|",
    "C[C@H](N)C(=O)O |a:1|",
    "C[C@@H](O)[C@H](N)C(=O)O |&1:1,3|",
    "C[C@H](O)[C@@H](C)N |a:1,&1:3|",
])
def test_v3000_and_cxsmiles_routes_agree(cxsmiles):
    assert keys(mf(molblock(cxsmiles, v3000=True))) == keys(sm(cxsmiles))


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

def _code(fn, *args):
    with pytest.raises(ChemistryError) as exc:
        fn(*args)
    return exc.value.code


@pytest.mark.parametrize("blank", ["", " ", "\n", "\r\n", "  \n \t\n  "])
def test_empty_input(blank):
    assert _code(normalize, blank) == "structure_empty"
    assert _code(normalize_smiles, blank) == "structure_empty"


def test_molfile_without_atoms_is_empty():
    empty = "\n  SciELN  17112615393D\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n"
    assert _code(normalize, empty) == "structure_empty"


@pytest.mark.parametrize("garbage", [
    "this is not a molfile",
    "C1CC(\nnot\na\nmolfile\n",
    "\n\n\n  5  4  0  0  0  0  0  0  0  0999 V2000\n    0.0000    0.0000    0.0000 C   0  0\nM  END\n",
    "\x00\x01\x02 binary junk \xff",
])
def test_garbage_molfile(garbage):
    assert _code(normalize, garbage) == "structure_parse_failed"


@pytest.mark.parametrize("garbage", ["C1CC(", "this is not a smiles", "c1ccccc", "C(C"])
def test_garbage_smiles(garbage):
    assert _code(normalize_smiles, garbage) == "structure_parse_failed"


@pytest.mark.parametrize("symbol, extra", [
    ("R#", ("M  RGP  1   1   1",)),
    ("R", ()),
    ("*", ()),
    ("A", ()),
    ("Q", ()),
])
def test_placeholder_atoms_in_molfile(symbol, extra):
    mb = replace_atom_symbol(molblock("Cc1ccccc1"), 1, symbol, extra)
    assert _code(normalize, mb) == "structure_has_placeholders"


@pytest.mark.parametrize("smiles", [
    "*c1ccccc1",
    "[*]c1ccccc1",
    "[*:1]c1ccccc1",
    "[1*]c1ccccc1",
    "*c1ccccc1 |$_R1;;;;;;;$|",
    "*C(=O)O.Cl",
])
def test_placeholder_atoms_in_smiles(smiles):
    assert _code(normalize_smiles, smiles) == "structure_has_placeholders"


def test_placeholder_in_v3000_molfile():
    mb = molblock("*c1ccccc1", v3000=True)
    assert _code(normalize, mb) == "structure_has_placeholders"


# ---------------------------------------------------------------------------
# purity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["sciELN_R_1_phenylethylammonium_chloride.mol",
                                  "atropisomer_P_wedge.mol", "butenol_crossed_double_bond.mol"])
def test_normalize_is_pure(name):
    text = read_fixture(name)
    first = normalize(text)
    for mode in (None, "rac", "abs", None):
        normalize(text, mode)
    assert normalize(text) == first
    assert normalize(text, "rac") == normalize(text, "rac")


@pytest.mark.parametrize("smiles", ["C[C@H](N)C(=O)O |&1:1|", "C[C@@H](N)c1ccccc1.Cl", "c1ccccc1"])
def test_normalize_smiles_is_pure(smiles):
    first = normalize_smiles(smiles)
    normalize_smiles("CCO")
    normalize_smiles(smiles, "rel")
    assert normalize_smiles(smiles) == first


def test_fragment_order_does_not_matter_smiles():
    assert_same_compound(
        sm("C[C@@H](N)c1ccccc1.Cl.O"),
        sm("O.Cl.C[C@@H](N)c1ccccc1"),
        sm("Cl.O.C[C@@H](N)c1ccccc1"),
    )
