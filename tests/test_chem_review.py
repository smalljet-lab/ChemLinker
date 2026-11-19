"""Regression tests for the findings of the independent Phase 1 review.

Each test reproduces a case where an earlier version of ``chem.py`` could have
issued a wrong ID: one compound split into two STEREO numbers, two
stereoisomers merged into one, or drawn stereo dropped without telling anyone.
"""

from __future__ import annotations

import random

import pytest
from rdkit import Chem
from rdkit.Chem import rdCIPLabeler, rdDepictor

from compound_registry.chem import normalize, normalize_smiles
from compound_registry.errors import ChemistryError

from chem_helpers import read_fixture


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------

def _set_flag(lines: list[str], begin: int, end: int, flag: int) -> None:
    """Put a wedge (1) or hash (6) on the bond begin->end (1-based), narrow end at begin."""
    n_atoms, n_bonds = int(lines[3][0:3]), int(lines[3][3:6])
    for i in range(4 + n_atoms, 4 + n_atoms + n_bonds):
        a, b = int(lines[i][0:3]), int(lines[i][3:6])
        if {a, b} == {begin, end}:
            lines[i] = f"{begin:3d}{end:3d}{lines[i][6:9]}{flag:3d}{lines[i][12:]}"
            return
    raise KeyError((begin, end))


def biaryl(smiles: str, wedges: list[tuple[int, int, int]], seed: int | None = None) -> str:
    """V2000 molblock from an atom-mapped SMILES.

    ``wedges`` = [(axis atom map, partner axis atom map, flag)]: the flag goes
    on a ring single bond that starts at the axis atom and does not lead to
    the partner. ``seed`` shuffles the atom order before the coordinates are
    computed, so every seed is a different but valid drawing.
    """
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None, smiles
    if seed is not None:
        order = list(range(mol.GetNumAtoms()))
        random.Random(seed).shuffle(order)
        mol = Chem.RenumberAtoms(mol, order)
    rdDepictor.Compute2DCoords(mol)
    index = {a.GetAtomMapNum(): a.GetIdx() for a in mol.GetAtoms() if a.GetAtomMapNum()}
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    Chem.Kekulize(mol, clearAromaticFlags=True)
    lines = Chem.MolToMolBlock(mol).split("\n")
    for axis_map, partner_map, flag in wedges:
        axis, partner = index[axis_map], index[partner_map]
        ring_neighbours = [
            n.GetIdx()
            for n in mol.GetAtomWithIdx(axis).GetNeighbors()
            if n.GetIdx() != partner
            and mol.GetBondBetweenAtoms(axis, n.GetIdx()).GetBondType() == Chem.BondType.SINGLE
        ]
        assert ring_neighbours, "no single ring bond at the axis atom"
        _set_flag(lines, axis + 1, min(ring_neighbours) + 1, flag)
    return "\n".join(lines)


def cip_axis_labels(molblock: str) -> tuple[str, ...]:
    """Sorted CIP labels of all axes RDKit perceives: the ground truth for
    molecules without tautomers."""
    mol = Chem.MolFromMolBlock(molblock)
    rdCIPLabeler.AssignCIPLabels(mol)
    return tuple(sorted(b.GetProp("_CIPCode") for b in mol.GetBonds() if b.HasProp("_CIPCode")))


def triple(n) -> tuple[str, str, str]:
    return (n.core_key, n.stereo_key, n.stereo_mode)


# --------------------------------------------------------------------------
# 1a. two symmetry-equivalent axes: the meso form must be ONE compound
# --------------------------------------------------------------------------

MESO_BIS_AXIS = "COc1cccc[c:3]1-[c:1]1cc(C)[c:2](-[c:4]2ccccc2OC)cc1C"


def test_two_equivalent_axes_are_keyed_by_configuration_not_by_atom_order():
    by_truth: dict[tuple[str, ...], set] = {}
    for seed in (None, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        for f1 in (1, 6):
            for f2 in (1, 6):
                mb = biaryl(MESO_BIS_AXIS, [(1, 3, f1), (2, 4, f2)], seed=seed)
                truth = cip_axis_labels(mb)
                assert len(truth) == 2, "fixture must have two perceived axes"
                by_truth.setdefault(truth, set()).add(triple(normalize(mb)))
    assert set(by_truth) == {("M", "M"), ("M", "P"), ("P", "P")}
    for truth, keys in by_truth.items():
        assert len(keys) == 1, f"{truth} was split into {len(keys)} variants"
    all_keys = [next(iter(v)) for v in by_truth.values()]
    assert len(set(all_keys)) == 3, "different configurations were merged"


def test_racemate_of_the_meso_bis_axis_form_collapses_to_the_meso_compound():
    for seed in (None, 1, 2, 3):
        for f1, f2 in ((1, 1), (1, 6), (6, 1), (6, 6)):
            mb = biaryl(MESO_BIS_AXIS, [(1, 3, f1), (2, 4, f2)], seed=seed)
            if cip_axis_labels(mb) != ("M", "P"):
                continue
            plain, rac = normalize(mb), normalize(mb, "rac")
            assert plain.stereo_mode == "abs"
            assert triple(rac) == triple(plain)


def test_racemate_of_the_chiral_bis_axis_form_is_one_variant():
    keys = set()
    for seed in (None, 1, 2, 3):
        for f1, f2 in ((1, 1), (1, 6), (6, 1), (6, 6)):
            mb = biaryl(MESO_BIS_AXIS, [(1, 3, f1), (2, 4, f2)], seed=seed)
            if cip_axis_labels(mb) == ("M", "P"):
                continue
            rac = normalize(mb, "rac")
            assert rac.stereo_mode == "rac"
            keys.add(triple(rac))
    assert len(keys) == 1


# --------------------------------------------------------------------------
# 1b. identical fragments
# --------------------------------------------------------------------------

TWO_COPIES = "Cc1cccc(Cl)[c:1]1-[c:2]1c(C)cccc1OC.Cc1cccc(Cl)[c:3]1-[c:4]1c(C)cccc1OC"


def test_axes_in_two_identical_fragments_do_not_depend_on_fragment_order():
    by_truth: dict[tuple[str, ...], set] = {}
    for seed in (None, 1, 2, 3, 4, 5, 6, 7):
        for f1 in (1, 6):
            for f2 in (1, 6):
                mb = biaryl(TWO_COPIES, [(1, 2, f1), (3, 4, f2)], seed=seed)
                truth = cip_axis_labels(mb)
                assert len(truth) == 2
                by_truth.setdefault(truth, set()).add(triple(normalize(mb)))
    assert set(by_truth) == {("M", "M"), ("M", "P"), ("P", "P")}
    assert all(len(keys) == 1 for keys in by_truth.values())
    assert len({next(iter(v)) for v in by_truth.values()}) == 3


def test_one_wedged_fragment_of_two_identical_ones_is_stable():
    by_truth: dict[tuple[str, ...], set] = {}
    for seed in (None, 1, 2, 3, 4, 5, 6, 7):
        for which in ((1, 2), (3, 4)):
            for flag in (1, 6):
                mb = biaryl(TWO_COPIES, [(which[0], which[1], flag)], seed=seed)
                truth = cip_axis_labels(mb)
                assert len(truth) == 1
                by_truth.setdefault(truth, set()).add(triple(normalize(mb)))
    assert set(by_truth) == {("M",), ("P",)}
    assert all(len(keys) == 1 for keys in by_truth.values())


# --------------------------------------------------------------------------
# 1c. the axis sign must not depend on which tautomer was drawn
# --------------------------------------------------------------------------

def tautomer_pair(smiles_a: str, smiles_b: str, axis: int, partner: int, flag: int) -> tuple[str, str]:
    """Two tautomers with identical atom order, coordinates and wedge."""
    mol_a, mol_b = Chem.MolFromSmiles(smiles_a), Chem.MolFromSmiles(smiles_b)
    assert [a.GetSymbol() for a in mol_a.GetAtoms()] == [a.GetSymbol() for a in mol_b.GetAtoms()]
    rdDepictor.Compute2DCoords(mol_a)
    mol_b.RemoveAllConformers()
    mol_b.AddConformer(Chem.Conformer(mol_a.GetConformer()), assignId=True)
    aromatic = [(b.GetBeginAtomIdx(), b.GetEndAtomIdx()) for b in mol_a.GetBonds() if b.GetIsAromatic()]
    blocks = []
    for mol in (mol_a, mol_b):
        Chem.Kekulize(mol, clearAromaticFlags=True)
        blocks.append(mol)
    # Draw the benzene rings with the same Kekule pattern in both tautomers.
    for i, j in aromatic:
        mol_b.GetBondBetweenAtoms(i, j).SetBondType(mol_a.GetBondBetweenAtoms(i, j).GetBondType())
    # a ring bond at the axis atom that is single in BOTH Kekule forms
    neighbour = next(
        n.GetIdx()
        for n in mol_a.GetAtomWithIdx(axis).GetNeighbors()
        if n.GetIdx() != partner
        and all(m.GetBondBetweenAtoms(axis, n.GetIdx()).GetBondType() == Chem.BondType.SINGLE for m in blocks)
    )
    out = []
    for mol in blocks:
        lines = Chem.MolToMolBlock(mol).split("\n")
        _set_flag(lines, axis + 1, neighbour + 1, flag)
        out.append("\n".join(lines))
    return out[0], out[1]


@pytest.mark.parametrize("flag", [1, 6])
@pytest.mark.parametrize(
    "smiles_a, smiles_b, axis, partner",
    [
        # amidine N-H tautomers next to the axis (indices: axis = ring carbon bearing the aryl)
        ("CNC(=N)c1cccc(C#N)c1-c1c(C)cccc1Cl", "CN=C(N)c1cccc(C#N)c1-c1c(C)cccc1Cl", 11, 12),
        # amide / imidic acid
        ("NC(=O)c1cccc(C#N)c1-c1c(C)cccc1Cl", "N=C(O)c1cccc(C#N)c1-c1c(C)cccc1Cl", 10, 11),
    ],
)
def test_same_atropisomer_drawn_as_two_tautomers_is_one_compound(smiles_a, smiles_b, axis, partner, flag):
    block_a, block_b = tautomer_pair(smiles_a, smiles_b, axis, partner, flag)
    a, b = normalize(block_a), normalize(block_b)
    assert "|atrop=" in a.stereo_key, "fixture: the axis must be perceived"
    assert triple(a) == triple(b)


@pytest.mark.parametrize(
    "smiles_a, smiles_b, axis, partner",
    [("CNC(=N)c1cccc(C#N)c1-c1c(C)cccc1Cl", "CN=C(N)c1cccc(C#N)c1-c1c(C)cccc1Cl", 11, 12)],
)
def test_opposite_wedges_stay_opposite_across_tautomers(smiles_a, smiles_b, axis, partner):
    wedge_a, wedge_b = tautomer_pair(smiles_a, smiles_b, axis, partner, 1)
    hash_a, hash_b = tautomer_pair(smiles_a, smiles_b, axis, partner, 6)
    assert normalize(wedge_a).stereo_key == normalize(wedge_b).stereo_key
    assert normalize(hash_a).stereo_key == normalize(hash_b).stereo_key
    assert normalize(wedge_a).stereo_key != normalize(hash_b).stereo_key


def test_axis_between_tautomerically_identical_ortho_groups_is_not_stereogenic():
    # Both ortho positions carry the same amidine; InChI makes them equivalent,
    # so every drawing must give the same, axis-free key.
    a = "CNC(=N)c1cccc(C(N)=NC)c1-c1ccccc1"
    b = "CN=C(N)c1cccc(C(=N)NC)c1-c1ccccc1"
    flat = normalize_smiles(a)
    keys = set()
    for flag in (1, 6):
        block_a, block_b = tautomer_pair(a, b, 13, 14, flag)
        for block in (block_a, block_b):
            n = normalize(block)
            assert "|atrop=" not in n.stereo_key
            keys.add((n.core_key, n.stereo_key, n.stereo_mode))
    assert keys == {triple(flat)}


# --------------------------------------------------------------------------
# 1d. pseudo-asymmetric axis: refuse, never merge
# --------------------------------------------------------------------------

def test_pseudo_asymmetric_axis_is_refused():
    smiles = "C[C@@H](O)c1cccc([C@H](C)O)[c:1]1-[c:2]1c(C)cccc1Cl"
    for flag in (1, 6):
        with pytest.raises(ChemistryError) as err:
            normalize(biaryl(smiles, [(2, 1, flag)]))
        assert err.value.code == "atropisomer_unsupported"


def test_equivalent_axes_with_stereocentres_are_refused():
    smiles = "COc1cccc[c:3]1-[c:1]1cc([C@H](C)O)[c:2](-[c:4]2ccccc2OC)cc1[C@H](C)O"
    with pytest.raises(ChemistryError) as err:
        normalize(biaryl(smiles, [(1, 3, 1), (2, 4, 6)]))
    assert err.value.code == "atropisomer_unsupported"


# --------------------------------------------------------------------------
# 2. drawn stereo that is not in the key is reported, never dropped silently
# --------------------------------------------------------------------------

ALLENE = """
  review

  5  4  0  0  0  0  0  0  0  0999 V2000
   -1.5000    0.8660    0.0000 C   0  0
   -0.7500    0.0000    0.0000 C   0  0
    0.5000    0.0000    0.0000 C   0  0
    1.7500    0.0000    0.0000 C   0  0
    2.5000    0.8660    0.0000 C   0  0
  1  2  1  0
  2  3  2  0
  3  4  2  0
  4  5  1  {flag}
M  END
"""


@pytest.mark.parametrize("flag", [1, 6])
def test_allene_wedge_is_reported_as_ignored(flag):
    n = normalize(ALLENE.format(flag=flag))
    assert n.ignored_stereo, "an allene wedge must not vanish silently"


def test_flat_allene_has_nothing_to_report():
    assert normalize(ALLENE.format(flag=0)).ignored_stereo == ()


def test_undefined_override_clears_the_ignored_list():
    assert normalize(ALLENE.format(flag=1), "undefined").ignored_stereo == ()


def _plain_block(smiles: str) -> tuple[Chem.Mol, list[str]]:
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    index = {a.GetAtomMapNum(): a.GetIdx() for a in mol.GetAtoms() if a.GetAtomMapNum()}
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    mol.index = index  # type: ignore[attr-defined]
    return mol, Chem.MolToMolBlock(mol).split("\n")


@pytest.mark.parametrize("flag", [1, 6])
def test_wedge_on_the_ortho_substituent_bond_is_reported(flag):
    # methyl (6) on the ring atom (5) next to the axis atom; wedge 5 -> 6
    mol, lines = _plain_block("[CH3:6][c:5]1cccc(OC)c1-[c:2]1c(C)cccc1Cl")
    _set_flag(lines, mol.index[5] + 1, mol.index[6] + 1, flag)
    n = normalize("\n".join(lines))
    assert "|atrop=" not in n.stereo_key
    assert n.ignored_stereo


@pytest.mark.parametrize("flag", [1, 6])
def test_wedge_on_the_axis_bond_itself_is_reported(flag):
    mol, lines = _plain_block("COc1cccc(C)[c:5]1-[c:2]1c(C)cccc1Cl")
    _set_flag(lines, mol.index[5] + 1, mol.index[2] + 1, flag)
    n = normalize("\n".join(lines))
    assert "|atrop=" not in n.stereo_key
    assert n.ignored_stereo


def test_wedge_on_a_non_stereocentre_is_reported():
    mol, lines = _plain_block("C[CH:1]([CH3:2])C")  # isopropyl: not a stereocentre
    _set_flag(lines, mol.index[1] + 1, mol.index[2] + 1, 1)
    n = normalize("\n".join(lines))
    assert n.stereo_mode == "none"
    assert n.ignored_stereo


def test_stereo_marks_rdkit_drops_from_a_smiles_are_reported():
    troeger = "Cc1ccc2c(c1)C[N@]1Cc3cc(C)ccc3[N@@](C2)C1"
    n = normalize_smiles(troeger)
    if n.stereo_mode == "none":  # RDKit does not keep bridgehead N stereo
        assert n.ignored_stereo
    assert normalize_smiles("C[C@H](C)O").ignored_stereo  # '@' on a non-centre


@pytest.mark.parametrize(
    "smiles",
    ["C[C@H](N)C(=O)O", "C/C=C/C(=O)O", "C[S@](=O)c1ccccc1", "CCO", "C[C@H]1CC[C@@H](C)CC1", "CC[P@](C)(=O)c1ccccc1"],
)
def test_ordinary_stereo_has_nothing_ignored(smiles):
    assert normalize_smiles(smiles).ignored_stereo == ()
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    assert normalize(Chem.MolToMolBlock(mol)).ignored_stereo == ()


@pytest.mark.parametrize(
    "name",
    [
        "sciELN_R_1_phenylethylammonium_chloride.mol",
        "chemdraw_L_alanine_chiral0.mol",
        "nitrotoluene_pentavalent.mol",
        "butenol_crossed_double_bond.mol",
        "atropisomer_P_wedge.mol",
        "atropisomer_M_hash.mol",
        "atropisomer_P_alt_hash.mol",
        "atropisomer_flat.mol",
    ],
)
def test_fixture_drawings_have_nothing_ignored(name):
    assert normalize(read_fixture(name)).ignored_stereo == ()


def test_wedge_to_an_explicit_hydrogen_counts_as_used():
    mol = Chem.AddHs(Chem.MolFromSmiles("C[C@H](N)C(=O)O"))
    rdDepictor.Compute2DCoords(mol)
    n = normalize(Chem.MolToMolBlock(mol))
    assert n.stereo_mode == "abs"
    assert n.ignored_stereo == ()


# --------------------------------------------------------------------------
# 5. abbreviations, polymer brackets, several records
# --------------------------------------------------------------------------

ETHANOL = """
  review

  3  2  0  0  0  0  0  0  0  0999 V2000
    0.0000    0.0000    0.0000 C   0  0
    1.2990    0.7500    0.0000 C   0  0
    2.5981    0.0000    0.0000 O   0  0
  1  2  1  0
  2  3  1  0
{extra}M  END
"""


def test_plain_ethanol_parses():
    assert normalize(ETHANOL.format(extra="")).formula == "C2H6O"


@pytest.mark.parametrize("extra", ["A    1\nBoc\n", "A    3\nOTs\n"])
def test_atom_alias_is_refused(extra):
    with pytest.raises(ChemistryError) as err:
        normalize(ETHANOL.format(extra=extra))
    assert err.value.code == "structure_has_placeholders"


def test_polymer_bracket_is_refused():
    extra = "M  STY  1   1 SRU\nM  SAL   1  2   1   2\nM  SMT   1 n\n"
    with pytest.raises(ChemistryError) as err:
        normalize(ETHANOL.format(extra=extra))
    assert err.value.code == "structure_unsupported"


def test_several_records_are_refused():
    two = ETHANOL.format(extra="") + "$$$$\n" + ETHANOL.format(extra="")
    with pytest.raises(ChemistryError) as err:
        normalize(two)
    assert err.value.code == "structure_parse_failed"


def test_single_record_with_sdf_terminator_is_accepted():
    assert normalize(ETHANOL.format(extra="") + "$$$$\n").formula == "C2H6O"


# --------------------------------------------------------------------------
# 9 / 11. contradictory groups, routine notices
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cx", ["C[C@H](N)C(=O)O |&1:1,o1:1|", "C[C@H](N)C(=O)O |a:1,&1:1|"])
def test_centre_in_two_stereo_groups_is_refused(cx):
    try:
        n = normalize_smiles(cx)
    except ChemistryError as err:
        assert err.code in ("invalid_stereo_groups", "structure_parse_failed")
    else:  # RDKit may already have reduced it to one group while parsing
        assert n.stereo_mode in ("abs", "rac", "rel")


def test_nitro_compounds_carry_no_routine_inchi_warning():
    assert normalize_smiles("Cc1ccc(cc1)[N+](=O)[O-]").warnings == ()
    assert normalize(read_fixture("nitrotoluene_pentavalent.mol")).warnings == ()
