"""Sanity checks of the test INPUTS, using RDKit/InChI only.

These tests do not import ``compound_registry.chem``. They make sure every
hand-written molfile in tests/data and every helper-generated variant really
encodes the structure the chemistry tests claim it does (a wrong fixture is
worse than no fixture).
"""

from __future__ import annotations

import math

import pytest
from rdkit import Chem
from rdkit.Chem import inchi, rdCIPLabeler

from chem_helpers import (
    kekule_molblock,
    mirror_x,
    molblock,
    molblock_3d,
    read_fixture,
    renumber_v2000,
    replace_atom_symbol,
    rotate,
    set_bond_stereo,
    set_chiral_flag,
    strip_wedges,
    transform_coords,
)


def _parse(mb: str) -> Chem.Mol:
    mol = Chem.MolFromMolBlock(mb)
    assert mol is not None
    return mol


def atom_cip(mol: Chem.Mol) -> list[str]:
    mol = Chem.Mol(mol)
    rdCIPLabeler.AssignCIPLabels(mol)
    return [a.GetProp("_CIPCode") for a in mol.GetAtoms() if a.HasProp("_CIPCode")]


def axis_labels(mol: Chem.Mol) -> list[str]:
    mol = Chem.Mol(mol)
    rdCIPLabeler.AssignCIPLabels(mol)
    return [b.GetProp("_CIPCode") for b in mol.GetBonds() if b.HasProp("_CIPCode")]


def std_inchi(mol_or_smiles) -> str:
    mol = Chem.MolFromSmiles(mol_or_smiles) if isinstance(mol_or_smiles, str) else mol_or_smiles
    return inchi.MolToInchi(mol)


# ---------------------------------------------------------------------------
# hand-written files in tests/data
# ---------------------------------------------------------------------------

def test_fixture_sciELN_is_R_phenylethylammonium_chloride():
    mb = read_fixture("sciELN_R_1_phenylethylammonium_chloride.mol")
    lines = mb.splitlines()
    assert Chem.MolFromSmiles(lines[0]) is not None        # line 1 is a SMILES
    assert lines[1] == "  SciELN  17112615393D"
    assert lines[3].endswith("V2000")
    assert int(lines[3][12:15]) == 0                       # chiral flag 0
    assert "M  CHG  1   3   1" in lines and "M  CHG  1  10  -1" in lines
    assert lines[13].split()[3] == "Cl" and lines[13].startswith("    0.0000    0.0000")
    assert lines[6].split()[5] == "3" and lines[13].split()[5] == "5"   # atom-block charges
    mol = _parse(mb)
    conf = mol.GetConformer()
    b = mol.GetBondBetweenAtoms(1, 3)
    p, q = conf.GetAtomPosition(1), conf.GetAtomPosition(3)
    assert math.dist((p.x, p.y), (q.x, q.y)) == pytest.approx(25, abs=0.5)
    assert b is not None
    assert Chem.MolToSmiles(mol) == Chem.MolToSmiles(Chem.MolFromSmiles("C[C@@H]([NH3+])c1ccccc1.[Cl-]"))
    assert atom_cip(mol) == ["R"]
    assert len(Chem.GetMolFrags(mol)) == 2


def test_fixture_chemdraw_alanine_is_L_with_chiral_flag_0():
    mb = read_fixture("chemdraw_L_alanine_chiral0.mol")
    assert mb.splitlines()[1].startswith("  ChemDraw")
    assert int(mb.splitlines()[3][12:15]) == 0
    mol = _parse(mb)
    assert atom_cip(mol) == ["S"]
    assert Chem.MolToSmiles(mol) == Chem.CanonSmiles("C[C@H](N)C(=O)O")
    # chiral flag 1 version used by the tests is the same molecule for RDKit
    assert Chem.MolToSmiles(_parse(set_chiral_flag(mb, 1))) == Chem.MolToSmiles(mol)


def test_fixture_pentavalent_nitro_is_4_nitrotoluene():
    mb = read_fixture("nitrotoluene_pentavalent.mol")
    assert "CHG" not in mb
    assert mb.splitlines()[9].split()[3] == "N"
    assert mb.splitlines()[19:21] == ["  6  7  2  0", "  6  8  2  0"]   # N=O, N=O
    assert Chem.MolToSmiles(_parse(mb)) == Chem.CanonSmiles("Cc1ccc([N+](=O)[O-])cc1")


def test_fixture_crossed_double_bond_is_unspecified_butenol():
    mol = _parse(read_fixture("butenol_crossed_double_bond.mol"))
    assert Chem.MolToSmiles(mol) == "CC=CCO"
    db = [b for b in mol.GetBonds() if b.GetBondType() == Chem.BondType.DOUBLE]
    assert db[0].GetStereo() == Chem.BondStereo.STEREOANY
    assert "/b" not in std_inchi(mol)


@pytest.mark.parametrize("name, label", [
    ("atropisomer_P_wedge.mol", "P"),
    ("atropisomer_P_alt_hash.mol", "P"),
    ("atropisomer_M_hash.mol", "M"),
])
def test_fixture_atropisomers(name, label):
    mb = read_fixture(name)
    mol = _parse(mb)
    assert axis_labels(mol) == [label]
    axis = mol.GetBondBetweenAtoms(8, 9)
    assert axis.GetStereo() in (Chem.BondStereo.STEREOATROPCW, Chem.BondStereo.STEREOATROPCCW)
    # the wedge/hash starts at an axis atom (atom 9 or 10)
    wedge = [l for l in mb.splitlines()[23:43] if l[9:12].strip() in ("1", "6")]
    assert len(wedge) == 1 and int(wedge[0][0:3]) in (9, 10)
    # four different ortho substituents: Me/OMe on one ring, Cl/COOH on the other
    assert Chem.MolToSmiles(mol) == Chem.CanonSmiles("Cc1cccc(OC)c1-c1c(Cl)cccc1C(=O)O")


def test_fixture_atropisomer_flat_has_no_stereo():
    mol = _parse(read_fixture("atropisomer_flat.mol"))
    assert axis_labels(mol) == []
    assert all(b.GetStereo() == Chem.BondStereo.STEREONONE for b in mol.GetBonds())


def test_fixture_atropisomers_share_standard_inchi():
    keys = {inchi.MolToInchiKey(_parse(read_fixture(n))) for n in (
        "atropisomer_P_wedge.mol", "atropisomer_M_hash.mol", "atropisomer_flat.mol")}
    assert len(keys) == 1


# ---------------------------------------------------------------------------
# helper-generated variants
# ---------------------------------------------------------------------------

ATROP_RENUMBER = [19, 3, 10, 1, 17, 5, 12, 7, 15, 2, 9, 18, 4, 11, 6, 16, 8, 14, 13]


def test_renumbered_atropisomer_keeps_its_label():
    mb = renumber_v2000(read_fixture("atropisomer_P_wedge.mol"), ATROP_RENUMBER)
    assert axis_labels(_parse(mb)) == ["P"]
    mb = renumber_v2000(read_fixture("atropisomer_M_hash.mol"), ATROP_RENUMBER)
    assert axis_labels(_parse(mb)) == ["M"]


def test_renumbered_sciELN_keeps_structure():
    mb = read_fixture("sciELN_R_1_phenylethylammonium_chloride.mol")
    out = renumber_v2000(mb, [10, 4, 9, 2, 8, 3, 7, 1, 6, 5])
    mol = _parse(out)
    assert Chem.MolToSmiles(mol) == Chem.MolToSmiles(_parse(mb))
    assert atom_cip(mol) == ["R"]


def test_mirror_and_rotation_helpers():
    mb = molblock("C[C@H](N)C(=O)O")
    assert atom_cip(_parse(mb)) == ["S"]
    assert atom_cip(_parse(transform_coords(mb, rotate(137, 5.0, -3.0)))) == ["S"]
    assert atom_cip(_parse(transform_coords(mb, mirror_x, swap_wedges=True))) == ["S"]
    assert atom_cip(_parse(transform_coords(mb, mirror_x))) == ["R"]
    flat = _parse(strip_wedges(mb))
    assert all(a.GetChiralTag() == Chem.ChiralType.CHI_UNSPECIFIED for a in flat.GetAtoms())


def test_mirrored_atropisomer_without_wedge_swap_is_the_other_atropisomer():
    mb = transform_coords(read_fixture("atropisomer_P_wedge.mol"), mirror_x)
    assert axis_labels(_parse(mb)) == ["M"]


def test_wavy_bond_helper_gives_unspecified_centre():
    mb = molblock("C[C@H](O)CC")
    mol = _parse(mb)
    centre = [a.GetIdx() for a in mol.GetAtoms() if a.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED][0]
    nbr = mol.GetAtomWithIdx(centre).GetNeighbors()[0].GetIdx()
    wavy = set_bond_stereo(strip_wedges(mb), centre + 1, nbr + 1, 4)
    assert atom_cip(_parse(wavy)) == []


def test_kekule_helper_keeps_bond_pattern():
    a = kekule_molblock("CC1=CC=CC=C1")
    b = kekule_molblock("CC1C=CC=CC=1")
    assert a != b
    assert Chem.MolToSmiles(_parse(a)) == Chem.MolToSmiles(_parse(b)) == "Cc1ccccc1"
    assert " 4  0" not in a and "  2  0" in a
    assert "  4  0" in molblock("Cc1ccccc1", aromatic_bonds=True)


def test_explicit_h_and_3d_helpers():
    mb = molblock("C[C@H](N)C(=O)O", explicit_h=True)
    assert sum(1 for l in mb.splitlines()[4:] if l[31:34].strip() == "H") == 7
    mol = _parse(mb)
    assert atom_cip(mol) == ["S"]
    m3 = _parse(molblock_3d("C[C@H](N)C(=O)O"))
    assert m3.GetConformer().Is3D()
    assert atom_cip(m3) == ["S"]


@pytest.mark.parametrize("cx, tag", [
    ("C[C@H](N)C(=O)O |&1:1|", "MDLV30/STERAC1"),
    ("C[C@H](N)C(=O)O |o1:1|", "MDLV30/STEREL1"),
    ("C[C@H](N)C(=O)O |a:1|", "MDLV30/STEABS"),
    ("C[C@H](O)[C@@H](C)N |a:1,&1:3|", "MDLV30/STERAC1"),
])
def test_v3000_enhanced_stereo_written(cx, tag):
    mb = molblock(cx, v3000=True)
    assert "V3000" in mb and tag in mb
    mol = _parse(mb)
    assert len(mol.GetStereoGroups()) >= 1


def _cxsmiles_used_in_suite() -> list[str]:
    """Every CXSMILES literal with a stereo group in the chemistry test modules."""
    import ast
    from pathlib import Path
    found = set()
    for name in ("test_chem.py", "test_chem_stereo.py"):
        tree = ast.parse((Path(__file__).parent / name).read_text(encoding="utf-8"))
        consts = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                consts[node.targets[0].id] = node.value.value
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "|" in node.value \
                    and any(t in node.value for t in ("&", "o1", "a:")):
                found.add(node.value)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) \
                    and isinstance(node.left, ast.Name) and node.left.id in consts \
                    and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                found.add(consts[node.left.id] + node.right.value)
    return sorted(s for s in found
                  if s.rstrip().endswith("|") and " |" in s and s.split(" |")[0].strip())


@pytest.mark.parametrize("cx", _cxsmiles_used_in_suite())
def test_cxsmiles_stereo_groups_point_at_stereocentres(cx):
    """Guards against off-by-one atom indices in the |&1:..| groups of the tests."""
    mol = Chem.MolFromSmiles(cx)
    assert mol is not None
    groups = mol.GetStereoGroups()
    assert groups, cx
    for g in groups:
        for a in g.GetAtoms():
            assert a.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED, (cx, a.GetIdx())


def test_placeholder_molfiles_parse_as_dummy_or_query_atoms():
    base = molblock("Cc1ccccc1")
    for sym, extra in [("R#", ("M  RGP  1   1   1",)), ("A", ()), ("Q", ()), ("*", ()), ("R", ())]:
        mol = _parse(replace_atom_symbol(base, 1, sym, extra))
        assert mol.GetAtomWithIdx(0).GetAtomicNum() == 0, sym


def test_deuterium_symbol_molfile_is_CD3():
    mb = deuterium_symbol_molblock()
    assert Chem.MolToSmiles(_parse(mb)) == Chem.CanonSmiles("[2H]C([2H])([2H])Oc1ccccc1")


def deuterium_symbol_molblock() -> str:
    """Anisole with the three methyl H drawn as atom symbol 'D' (no M ISO)."""
    mol = Chem.AddHs(Chem.MolFromSmiles("COc1ccccc1"))
    from rdkit.Chem import rdDepictor
    rdDepictor.Compute2DCoords(mol)
    mb = Chem.MolToMolBlock(mol)
    methyl_h = [n.GetIdx() + 1 for n in mol.GetAtomWithIdx(0).GetNeighbors() if n.GetAtomicNum() == 1]
    assert len(methyl_h) == 3
    for idx in methyl_h:
        mb = replace_atom_symbol(mb, idx, "D")
    return mb


# ---------------------------------------------------------------------------
# meaning of the SMILES used in the chemistry tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("smiles, cip", [
    ("C[C@@H](N)C(=O)O", ["R"]),
    ("C[C@H](N)C(=O)O", ["S"]),
    ("C[C@@H](O)[C@H](N)C(=O)O", ["R", "S"]),           # L-threonine (2S,3R)
    ("C[C@H](O)[C@H](N)C(=O)O", ["S", "S"]),            # L-allo-threonine (2S,3S)
    ("O[C@@H](C(=O)O)[C@@H](O)C(=O)O", ["R", "R"]),     # (R,R)-tartaric acid
    ("O[C@H](C(=O)O)[C@H](O)C(=O)O", ["S", "S"]),       # (S,S)-tartaric acid
    ("O[C@H](C(=O)O)[C@@H](O)C(=O)O", ["S", "R"]),      # meso-tartaric acid
    ("C[S@@](=O)c1ccccc1", ["R"]),
    ("C[S@](=O)c1ccccc1", ["S"]),
    ("C[C@@H](N)c1ccccc1", ["R"]),
])
def test_cip_of_test_smiles(smiles, cip):
    assert atom_cip(Chem.MolFromSmiles(smiles)) == cip


def test_meso_tartaric_acid_is_achiral_for_inchi():
    meso = std_inchi("O[C@H](C(=O)O)[C@@H](O)C(=O)O")
    assert "/t" in meso and "/m" not in meso
    assert std_inchi(MESO_TARTARIC_OTHER_END) == meso
    assert sorted(atom_cip(Chem.MolFromSmiles(MESO_TARTARIC_OTHER_END))) == ["R", "S"]


# meso-tartaric acid written from the other end of the chain
MESO_TARTARIC_OTHER_END = "OC(=O)[C@@H](O)[C@@H](O)C(=O)O"


def test_dimethylcyclohexane_diastereomers_are_distinct_and_achiral():
    a = std_inchi("C[C@H]1CC[C@@H](C)CC1")
    b = std_inchi("C[C@H]1CC[C@H](C)CC1")
    assert a != b and "/m" not in a and "/m" not in b
    # 1,2-dimethylcyclobutane: cis is meso (achiral), trans is chiral
    cis, trans = std_inchi("C[C@H]1CC[C@H]1C"), std_inchi("C[C@H]1CC[C@@H]1C")
    assert "/t" in cis and "/m" not in cis and "/m" in trans


@pytest.mark.parametrize("a, b", [
    ("O=c1cccc[nH]1", "Oc1ccccn1"),
    ("CC(=O)N", "CC(O)=N"),
    ("Cc1c[nH]cn1", "Cc1cnc[nH]1"),
    ("NCc1ccccc1.Cl", "[NH3+]Cc1ccccc1.[Cl-]"),
    ("NCc1ccccc1.Cl", "[Cl-].NCc1ccccc1.[H+]"),
    ("NCC(=O)O", "[NH3+]CC(=O)[O-]"),
    ("O=C1C=CC=CN1", "OC1=NC=CC=C1"),
    ("CC(=O)NC", "CC(O)=NC"),
    ("Oc1ncccn1", "O=c1nccc[nH]1"),
    ("C[C@H](N)C(=O)N", "C[C@H](N)C(O)=N"),
    ("C[C@H](N)C(=O)O", "C[C@H]([NH3+])C(=O)[O-]"),
    ("c1ccc(cc1)N1CCNCC1.Cl.Cl", "c1ccc(cc1)N1CC[NH2+]CC1.[Cl-].Cl"),
    ("c1ccc(cc1)N1CCNCC1.Cl.Cl", "[Cl-].[Cl-].c1ccc(cc1)N1CCNCC1.[H+].[H+]"),
    ("C[C@@H](N)c1ccccc1.Cl", "[Cl-].C[C@@H](N)c1ccccc1.[H+]"),
])
def test_standard_inchi_merges_these_drawings(a, b):
    """Documents that standard InChI already treats these as one structure."""
    assert std_inchi(a) == std_inchi(b)


@pytest.mark.parametrize("a, b", [
    ("CC(C)=O", "CC(O)=C"),
    ("O=C1CCCCC1", "OC1=CCCCC1"),
    ("CC(=O)CC(C)=O", "CC(O)=CC(C)=O"),
])
def test_standard_inchi_keeps_keto_and_enol_apart(a, b):
    assert std_inchi(a) != std_inchi(b)
