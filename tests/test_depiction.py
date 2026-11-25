"""The drawing returned by "Capture reaction": compound_registry.depiction.

The pictures here are made up: a layout from RDKit is turned into what
MolScribe would answer for a picture of it (its coordinate grid of 64 steps,
its squeeze when the white margins are unequal), so no model is needed. All
structures are invented, simple molecules.
"""

from __future__ import annotations

import math
import statistics

import pytest
from rdkit import Chem
from rdkit.Chem import rdDepictor

from compound_registry import depiction, recognition
from compound_registry.routes import read_molfile

BOX = [0, 0, 320, 400]              # the picture MolScribe was given (pixels)
SEEN = [35, 95, 285, 345]           # what it looked at: the drawing with 5 px of white around


def layout(smiles: str, turn: float = 0.0) -> Chem.Mol:
    """The molecule with RDKit's 2D layout, turned by ``turn`` degrees."""
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    conformer = mol.GetConformer()
    cos_t, sin_t = math.cos(math.radians(turn)), math.sin(math.radians(turn))
    for i in range(mol.GetNumAtoms()):
        p = conformer.GetAtomPosition(i)
        conformer.SetAtomPosition(i, (p.x * cos_t - p.y * sin_t, p.x * sin_t + p.y * cos_t, 0.0))
    return mol


def as_molscribe(mol: Chem.Mol, box=BOX, seen=SEEN, grid: bool = True, wedges=None,
                 explicit_h: bool = False) -> str:
    """The molfile MolScribe gives for a picture of ``mol`` as laid out: the
    drawing fills ``seen`` (keeping its shape), every atom sits on the grid of
    64 steps, x is scaled with the shape of the whole ``box``. ``wedges``:
    ``{(begin, end): 1 or 6}`` to draw instead of RDKit's own."""
    conformer = mol.GetConformer()
    pts = [(conformer.GetAtomPosition(i).x, conformer.GetAtomPosition(i).y)
           for i in range(mol.GetNumAtoms())]
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    seen_w, seen_h = seen[2] - seen[0], seen[3] - seen[1]
    pad = 5.0
    scale = min((seen_w - 2 * pad) / max(x1 - x0, 1e-6), (seen_h - 2 * pad) / max(y1 - y0, 1e-6))
    width, height = box[2] - box[0], box[3] - box[1]
    work = Chem.Mol(mol)
    conf = work.GetConformer()
    for i, (x, y) in enumerate(pts):
        px = pad + (x - x0) * scale                 # pixel inside ``seen``; y grows downwards
        py = pad + (y1 - y) * scale
        fx, fy = px / seen_w, py / seen_h
        if grid:
            fx, fy = round(fx * 63) / 63, round(fy * 63) / 63
        conf.SetAtomPosition(i, (fx * 10 * width / height, 1 - fy * 10, 0.0))
    block = Chem.MolToMolBlock(work)
    if wedges is not None:
        lines = block.split("\n")
        first = 4 + work.GetNumAtoms()
        for n in range(work.GetNumBonds()):
            line = lines[first + n]
            a, b = int(line[0:3]) - 1, int(line[3:6]) - 1
            stereo = 0
            for (begin, end), value in wedges.items():
                if {a, b} == {begin, end}:
                    a, b, stereo = begin, end, value
            lines[first + n] = f"{a + 1:3d}{b + 1:3d}{line[6:9]}{stereo:3d}"
        block = "\n".join(lines)
    return block


def drawn_wedges(block: str) -> dict[tuple[int, int], int]:
    lines = block.split("\n")
    atoms, bonds = int(lines[3][0:3]), int(lines[3][3:6])
    out = {}
    for line in lines[4 + atoms:4 + atoms + bonds]:
        stereo = int(line[9:12])
        if stereo in (1, 6):
            out[(int(line[0:3]) - 1, int(line[3:6]) - 1)] = stereo
    return out


def places(block: str) -> dict[int, tuple[float, float]]:
    mol = Chem.MolFromMolBlock(block, sanitize=False, removeHs=False)
    conformer = mol.GetConformer()
    return {i: (conformer.GetAtomPosition(i).x, conformer.GetAtomPosition(i).y)
            for i in range(mol.GetNumAtoms())}


def bond_lengths(block: str) -> list[float]:
    mol = Chem.MolFromMolBlock(block, sanitize=False, removeHs=False)
    p = places(block)
    return [math.dist(p[b.GetBeginAtomIdx()], p[b.GetEndAtomIdx()]) for b in mol.GetBonds()]


MAX_TURN = 6.5      # degrees the drawing may be turned onto the usual bond directions


def same_shape(block: str, reference: Chem.Mol, tolerance: float = 0.3,
               max_turn: float = MAX_TURN) -> float:
    """The largest distance (in bond lengths) between an atom of ``block`` and
    the same atom of ``reference`` once both are scaled to one bond length and
    laid on each other. The drawing may be turned by ``max_turn`` degrees at
    most and never mirrored: what is up stays up, what is left stays left."""
    out = Chem.MolFromMolBlock(block)
    matches = out.GetSubstructMatches(reference, useChirality=False, uniquify=False, maxMatches=2000)
    assert matches and len(matches[0]) == reference.GetNumAtoms()
    ref_conf = reference.GetConformer()
    ref = {i: (ref_conf.GetAtomPosition(i).x, ref_conf.GetAtomPosition(i).y)
           for i in range(reference.GetNumAtoms())}
    got_all = places(block)

    def normal(points):
        lengths = [math.dist(points[b.GetBeginAtomIdx()], points[b.GetEndAtomIdx()])
                   for b in reference.GetBonds()]
        scale = 1.0 / statistics.median(lengths) if lengths else 1.0
        cx = sum(p[0] for p in points.values()) / len(points)
        cy = sum(p[1] for p in points.values()) / len(points)
        return {i: ((p[0] - cx) * scale, (p[1] - cy) * scale) for i, p in points.items()}

    def distance(a, b):
        dot = sum(a[i][0] * b[i][0] + a[i][1] * b[i][1] for i in a)
        cross = sum(a[i][0] * b[i][1] - a[i][1] * b[i][0] for i in a)
        turn = math.atan2(cross, dot)                      # the turn that fits best
        limit = math.radians(max_turn)
        turn = max(-limit, min(limit, turn))               # ... but never more than allowed
        cos_t, sin_t = math.cos(turn), math.sin(turn)
        return max(math.dist((a[i][0] * cos_t - a[i][1] * sin_t, a[i][0] * sin_t + a[i][1] * cos_t),
                             b[i]) for i in a)

    # Equal groups (the methyls of a tert-butyl) may be told apart either way.
    a = normal(ref)
    worst = min(distance(a, normal({i: got_all[match[i]] for i in ref})) for match in matches)
    assert worst < tolerance, worst
    return worst


def read_back(block: str) -> str:
    return Chem.MolToSmiles(Chem.MolFromMolBlock(block))


def canonical(smiles: str) -> str:
    return Chem.MolToSmiles(Chem.MolFromSmiles(smiles))


def draw(smiles: str, molscribe: str | None, box=BOX, seen=SEEN) -> str:
    mol = Chem.MolFromSmiles(smiles)
    block = depiction.drawing_molfile(mol, read_molfile(molscribe) if molscribe else None, box, seen)
    assert block is not None and "V2000" in block
    return block


# --------------------------------------------------------------------------- the picture's places

def test_the_squeeze_of_unequal_margins_is_undone():
    """MolScribe looks at the drawing without its white border but scales x
    with the shape of the whole picture: here a square drawing in a tall
    picture comes out 25 % too narrow. With ``seen`` every atom is back at its
    pixel."""
    mol = layout("c1ccccc1")
    molscribe = as_molscribe(mol, grid=False)
    raw = places(molscribe)
    raw_w = max(p[0] for p in raw.values()) - min(p[0] for p in raw.values())
    raw_h = max(p[1] for p in raw.values()) - min(p[1] for p in raw.values())
    true_w = max(p[0] for p in places(Chem.MolToMolBlock(mol)).values()) * 2
    true_h = max(p[1] for p in places(Chem.MolToMolBlock(mol)).values()) * 2
    assert raw_w / raw_h == pytest.approx(true_w / true_h * 0.8, rel=0.02)      # squeezed

    fixed = depiction.picture_places(read_molfile(molscribe), BOX, SEEN)
    fixed_w = max(p[0] for p in fixed.values()) - min(p[0] for p in fixed.values())
    fixed_h = max(p[1] for p in fixed.values()) - min(p[1] for p in fixed.values())
    assert fixed_w / fixed_h == pytest.approx(true_w / true_h, rel=0.01)
    # In pixels of the picture, y up: all inside the rectangle MolScribe looked at.
    assert all(SEEN[0] <= x <= SEEN[2] and -SEEN[3] <= y <= -SEEN[1] for x, y in fixed.values())


def test_without_the_seen_rectangle_the_coordinates_are_taken_as_they_are():
    block = Chem.MolToMolBlock(layout("CCO"))
    assert depiction.picture_places(read_molfile(block)) == places(block)
    assert depiction.picture_places(read_molfile(block), BOX, None) == places(block)
    assert depiction.picture_places(read_molfile(block), [0, 0, 0, 0], SEEN) == places(block)


def test_a_molfile_that_is_not_on_molscribes_scale_is_left_alone():
    mol = layout("CCCCCCCCCCCCCCCCCCCC")              # 25 units wide: no fraction of a picture
    block = Chem.MolToMolBlock(mol)
    assert depiction.picture_places(read_molfile(block), BOX, SEEN) == places(block)


def test_a_drawing_without_coordinates_has_no_places():
    assert depiction.picture_places(Chem.MolFromSmiles("CCO")) is None
    assert depiction.picture_places(None) is None


# --------------------------------------------------------------------------- shape and orientation

@pytest.mark.parametrize("smiles, turn", [
    ("CC(C)(C)c1cccc(-c2ccccc2C(N)C)c1", 0.0),            # a biaryl with a side chain
    ("CC(C)(C)c1cccc(-c2ccccc2C(N)C)c1", 90.0),           # the same picture lying on its side
    ("CC(C)(C)c1cccc(-c2ccccc2C(N)C)c1", 180.0),
    ("O=C(O)c1ccc2[nH]ccc2c1", 30.0),                     # fused rings
    ("CCOC(=O)CCN1CCOCC1", 60.0),                         # a chain and a saturated ring
    ("Cc1ccc(S(=O)(=O)NCC(=O)OCC)cc1", 0.0),
])
def test_the_drawing_keeps_the_shape_and_orientation_of_the_picture(smiles, turn):
    """What is left, right, up and down in the picture stays there: also for a
    picture turned by 90 or 180 degrees, which a fresh layout would never
    reproduce."""
    mol = layout(smiles, turn)
    block = draw(smiles, as_molscribe(mol))
    assert read_back(block) == canonical(smiles)
    same_shape(block, mol)
    # ... and it is evened out: the grid's wobble is gone.
    lengths = bond_lengths(block)
    assert min(lengths) > 1.45 and max(lengths) < 1.55


def test_the_squeezed_coordinates_alone_would_not_have_the_shape():
    """The check above is not empty: MolScribe's coordinates as they are (the
    molfile the card used to show) fail it."""
    mol = layout("CC(C)(C)c1cccc(-c2ccccc2C(N)C)c1")
    with pytest.raises(AssertionError):
        same_shape(as_molscribe(mol), mol)


def off_the_grid(block: str) -> float:
    """The largest angle (degrees) between a bond and the nearest multiple of 30 degrees."""
    out = Chem.MolFromMolBlock(block)
    p = places(block)
    worst = 0.0
    for bond in out.GetBonds():
        a, b = p[bond.GetBeginAtomIdx()], p[bond.GetEndAtomIdx()]
        angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 30.0
        worst = max(worst, min(angle, 30.0 - angle))
    return worst


def upright(smiles: str, turn: float = 0.0) -> Chem.Mol:
    """RDKit's layout turned so that its first bond is horizontal (a drawing
    on the usual bond directions, like one from ChemDraw), then by ``turn``."""
    mol = layout(smiles)
    conformer = mol.GetConformer()
    bond = mol.GetBondWithIdx(0)
    a = conformer.GetAtomPosition(bond.GetBeginAtomIdx())
    b = conformer.GetAtomPosition(bond.GetEndAtomIdx())
    return layout(smiles, turn - math.degrees(math.atan2(b.y - a.y, b.x - a.x)))


def test_a_drawing_a_few_degrees_off_is_turned_onto_the_usual_bond_directions():
    """A scan that is 4 degrees askew, and the noise of the grid."""
    block = draw("CCc1ccccc1", as_molscribe(upright("CCc1ccccc1", 4.0)))
    assert off_the_grid(block) < 1.5
    same_shape(block, upright("CCc1ccccc1"), max_turn=1.0)


def test_a_drawing_that_is_really_at_an_angle_stays_at_that_angle():
    """12 degrees is no noise: the chemist drew it that way, and the card
    must look like the picture."""
    turned = upright("CCc1ccccc1", 12.0)
    block = draw("CCc1ccccc1", as_molscribe(turned))
    assert 9.0 < off_the_grid(block) < 15.0
    same_shape(block, turned, max_turn=2.0)


# --------------------------------------------------------------------------- tidy

def hexagon(noise: float) -> tuple[Chem.Mol, dict[int, tuple[float, float]]]:
    mol = Chem.MolFromSmiles("Cc1ccccc1")
    rdDepictor.Compute2DCoords(mol)
    conformer = mol.GetConformer()
    shifts = [(0.6, -0.4), (-0.5, 0.7), (0.3, 0.8), (-0.9, -0.2), (0.5, 0.5), (-0.4, -0.9), (0.8, 0.1)]
    return mol, {i: (conformer.GetAtomPosition(i).x + noise * shifts[i][0],
                     conformer.GetAtomPosition(i).y + noise * shifts[i][1])
                 for i in range(mol.GetNumAtoms())}


def test_tidy_makes_bonds_equal_and_rings_regular():
    mol, rough = hexagon(0.15)
    neat = depiction.tidy(mol, rough)
    assert neat is not rough
    for bond in mol.GetBonds():
        assert math.dist(neat[bond.GetBeginAtomIdx()], neat[bond.GetEndAtomIdx()]) == \
            pytest.approx(depiction.BOND, abs=0.01)
    ring = [a.GetIdx() for a in mol.GetAtoms() if a.IsInRing()]
    cx = sum(neat[i][0] for i in ring) / 6
    cy = sum(neat[i][1] for i in ring) / 6
    assert all(math.dist(neat[i], (cx, cy)) == pytest.approx(depiction.BOND, abs=0.02) for i in ring)


def test_tidy_refuses_to_move_atoms_far_from_the_picture():
    """Places that are no drawing at all (every atom somewhere) are not
    "tidied" into something the picture never showed."""
    mol = Chem.MolFromSmiles("CCCCCCCC")
    wild = {i: (0.0, 1.5 * i) if i % 2 else (4.0 * (i % 4), -3.0 * i) for i in range(8)}
    assert depiction.tidy(mol, wild) is wild


def test_tidy_draws_the_bond_to_an_end_atom_shorter_where_groups_would_meet():
    """On ideal angles the methyl of this tert-butyl lands exactly on a ring
    atom (the two nearly touch in such a picture). It is drawn with a shorter
    bond; nothing is bent."""
    smiles = "CC(C)(C)C(N)c1ccccc1-c1ccccc1"
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    conformer = mol.GetConformer()
    start = {i: (conformer.GetAtomPosition(i).x, conformer.GetAtomPosition(i).y)
             for i in range(mol.GetNumAtoms())}
    neat = depiction.tidy(mol, start)
    atoms = sorted(neat)
    bonded = {frozenset((b.GetBeginAtomIdx(), b.GetEndAtomIdx())) for b in mol.GetBonds()}
    nearest = min(math.dist(neat[i], neat[j]) for n, i in enumerate(atoms) for j in atoms[n + 1:]
                  if frozenset((i, j)) not in bonded)
    assert nearest > 0.25 * depiction.BOND
    lengths = [math.dist(neat[b.GetBeginAtomIdx()], neat[b.GetEndAtomIdx()]) for b in mol.GetBonds()]
    assert all(length <= depiction.BOND * 1.02 for length in lengths)
    ring = [b for b in mol.GetBonds() if b.IsInRing()]
    assert all(math.dist(neat[b.GetBeginAtomIdx()], neat[b.GetEndAtomIdx()]) ==
               pytest.approx(depiction.BOND, abs=0.02) for b in ring)


# --------------------------------------------------------------------------- wedges

AMINE = "C[C@H](N)c1ccccc1"          # atoms: 0 methyl, 1 the stereocentre, 2 N, 3.. the ring


def test_every_stereocentre_gets_a_wedge_that_agrees_with_the_smiles():
    for smiles in (AMINE, "C[C@@H](N)c1ccccc1", "C[C@H](O)[C@@H](N)C(=O)O",
                   "C[C@H]1CC[C@@H](C(C)C)[C@H](O)C1"):
        mol = layout(smiles)
        block = draw(smiles, as_molscribe(mol))
        centres = len(Chem.FindMolChiralCenters(Chem.MolFromSmiles(smiles)))
        assert len({begin for begin, _end in drawn_wedges(block)}) == centres
        assert read_back(block) == canonical(smiles)
        assert recognition._smiles_of(recognition._mol_from_molfile(block)) == canonical(smiles)


def test_the_pictures_own_wedge_is_kept_when_it_tells_the_truth():
    """The picture has a hash from the stereocentre to the methyl. RDKit
    would rather wedge another bond; the card shows the picture's."""
    mol = layout(AMINE)
    for bond, value in (((1, 0), None), ((1, 2), None), ((1, 3), None)):
        # Find out which wedge on this bond is the truth, then draw exactly that.
        for trial in (1, 6):
            if read_back(as_molscribe(mol, wedges={bond: trial})) == canonical(AMINE):
                value = trial
        assert value is not None
        block = draw(AMINE, as_molscribe(mol, wedges={bond: value}))
        assert read_back(block) == canonical(AMINE)
        out = Chem.MolFromMolBlock(block)
        match = out.GetSubstructMatch(Chem.MolFromSmiles(AMINE), useChirality=True)
        assert drawn_wedges(block) == {(match[bond[0]], match[bond[1]]): value}


def test_a_wedge_that_contradicts_the_smiles_is_never_copied():
    """The picture's wedge may be what was misread. The SMILES is the
    structure; the wedge stays on the picture's bond but says what the SMILES
    says."""
    mol = layout(AMINE)
    truth = 1 if read_back(as_molscribe(mol, wedges={(1, 0): 1})) == canonical(AMINE) else 6
    wrong = as_molscribe(mol, wedges={(1, 0): 7 - truth})
    assert read_back(wrong) != canonical(AMINE)
    block = draw(AMINE, wrong)
    assert read_back(block) == canonical(AMINE)
    out = Chem.MolFromMolBlock(block)
    match = out.GetSubstructMatch(Chem.MolFromSmiles(AMINE), useChirality=True)
    assert drawn_wedges(block) == {(match[1], match[0]): truth}


def test_a_stereocentre_the_picture_draws_without_a_wedge_gets_one():
    mol = layout(AMINE)
    flat = as_molscribe(mol, wedges={})
    assert drawn_wedges(flat) == {}
    block = draw(AMINE, flat)
    assert len(drawn_wedges(block)) == 1 and read_back(block) == canonical(AMINE)


def test_a_wedge_sets_the_chiral_flag_and_no_wedge_does_not():
    """Without the flag OpenChemLib reads the centre as a racemic "&1" group."""
    with_wedge = draw(AMINE, as_molscribe(layout(AMINE)))
    without = draw("CC(N)c1ccccc1", as_molscribe(layout("CC(N)c1ccccc1")))
    assert with_wedge.split("\n")[3][12:15] == "  1"
    assert without.split("\n")[3][12:15] == "  0"
    assert drawn_wedges(without) == {}                  # unknown stereo is not made definite


def test_a_hydrogen_the_picture_draws_keeps_its_wedge():
    """Ring fusions are drawn with the H on a wedge. The H stays an atom, so
    the wedge can stay where the picture has it."""
    smiles = "C[C@H](N)c1ccccc1"
    with_h = Chem.AddHs(Chem.MolFromSmiles(smiles), onlyOnAtoms=[1])
    rdDepictor.Compute2DCoords(with_h)
    h = with_h.GetNumAtoms() - 1
    truth = None
    for trial in (1, 6):
        if read_back(as_molscribe(with_h, wedges={(1, h): trial})) == canonical(smiles):
            truth = trial
    assert truth is not None
    block = draw(smiles, as_molscribe(with_h, wedges={(1, h): truth}))
    out = Chem.MolFromMolBlock(block, removeHs=False)
    hydrogens = [a.GetIdx() for a in out.GetAtoms() if a.GetAtomicNum() == 1]
    assert len(hydrogens) == 1
    assert [(end, value) for (_begin, end), value in drawn_wedges(block).items()] == [(hydrogens[0], truth)]
    assert read_back(block) == canonical(smiles)


# --------------------------------------------------------------------------- double bonds

@pytest.mark.parametrize("smiles, picture", [
    ("C/C=C/c1ccccc1", "C/C=C/c1ccccc1"),
    ("C/C=C\\c1ccccc1", "C/C=C\\c1ccccc1"),
    ("C/C=C/c1ccccc1", "C/C=C\\c1ccccc1"),               # the picture shows the other geometry
    ("C/C=C\\c1ccccc1", "C/C=C/c1ccccc1"),
    ("OC(=O)/C=C/C=C\\C", "OC(=O)/C=C/C=C\\C"),
])
def test_the_geometry_of_a_double_bond_is_the_one_of_the_smiles(smiles, picture):
    block = draw(smiles, as_molscribe(layout(picture)))
    assert read_back(block) == canonical(smiles)
    if canonical(smiles) == canonical(picture):
        same_shape(block, layout(picture))


def test_an_unknown_geometry_is_not_made_definite():
    """The picture shows E, the structure says nothing: the molfile marks the
    bond "either" (3), so nothing reads E from the coordinates."""
    block = draw("CC=Cc1ccccc1", as_molscribe(layout("C/C=C/c1ccccc1")))
    assert read_back(block) == "CC=Cc1ccccc1"
    lines = block.split("\n")
    atoms, bonds = int(lines[3][0:3]), int(lines[3][3:6])
    doubles = [line for line in lines[4 + atoms:4 + atoms + bonds]
               if int(line[6:9]) == 2 and int(line[9:12]) == 3]
    assert len(doubles) == 1


# --------------------------------------------------------------------------- abbreviations

def abbreviated(smiles: str, labels: dict[int, str], turn: float = 0.0, **kwargs) -> str:
    """A picture of ``smiles`` (with ``*`` where a label stands) in MolScribe's
    words: the placeholder is an ``R`` atom with an alias line."""
    mol = layout(smiles, turn)
    for index, label in labels.items():
        mol.GetAtomWithIdx(index).SetProp("molFileAlias", label)
    return as_molscribe(mol, **kwargs)


def test_an_expanded_abbreviation_starts_where_its_label_stands():
    """"OMe" on a benzene ring, the picture turned by 90 degrees: the ring
    stays as drawn and the O takes the place of the label."""
    picture = layout("*c1ccccc1", 90.0)
    molscribe = abbreviated("*c1ccccc1", {0: "OMe"}, 90.0)
    block = draw("COc1ccccc1", molscribe)
    assert read_back(block) == "COc1ccccc1"
    out = Chem.MolFromMolBlock(block)
    match = out.GetSubstructMatch(Chem.MolFromSmiles("Oc1ccccc1"))
    reference = Chem.RWMol(picture)
    reference.GetAtomWithIdx(0).SetAtomicNum(8)
    Chem.SanitizeMol(reference)
    conformer = out.GetConformer()
    got = {i: (conformer.GetAtomPosition(match[i]).x, conformer.GetAtomPosition(match[i]).y)
           for i in range(7)}
    ref_conf = picture.GetConformer()
    ref = {i: (ref_conf.GetAtomPosition(i).x, ref_conf.GetAtomPosition(i).y) for i in range(7)}
    cg = (sum(p[0] for p in got.values()) / 7, sum(p[1] for p in got.values()) / 7)
    cr = (sum(p[0] for p in ref.values()) / 7, sum(p[1] for p in ref.values()) / 7)
    for i in range(7):
        assert math.dist((got[i][0] - cg[0], got[i][1] - cg[1]),
                         (ref[i][0] - cr[0], ref[i][1] - cr[1])) < 0.3 * depiction.BOND
    assert depiction.clash_score(out, places(block)) == 0
    lengths = bond_lengths(block)
    assert min(lengths) > 1.4 and max(lengths) < 1.6


@pytest.mark.parametrize("smiles, picture, labels", [
    ("CC(C)(C)OC(=O)N1CCCC1C(=O)O", "*N1CCCC1C(=O)O", {0: "Boc"}),
    ("O=S(=O)(Oc1ccc(Br)cc1)C(F)(F)F", "*c1ccc(Br)cc1", {0: "OTf"}),
    ("CCOC(=O)C(Cc1ccccc1)C(=O)OCC", "*C(C*)*", {0: "CO2Et", 3: "Ph", 4: "CO2Et"}),
    ("Cc1ccc(S(=O)(=O)NCC(=O)OCC)cc1", "*NCC(=O)O*", {0: "Ts", 6: "Et"}),
])
def test_expanded_groups_are_laid_out_without_overlap(smiles, picture, labels):
    block = draw(smiles, abbreviated(picture, labels))
    assert read_back(block) == canonical(smiles)
    out = Chem.MolFromMolBlock(block)
    assert depiction.clash_score(out, places(block)) == 0
    lengths = bond_lengths(block)
    assert min(lengths) > 0.6 * depiction.BOND and max(lengths) < 1.6 * depiction.BOND


def test_labels_on_one_atom_get_the_group_of_their_size():
    """Ph3P+-Me drawn as a cross with the "Me" on top: the methyl is on top
    in the drawing too, not one of the phenyl groups."""
    picture = layout("*[P+](*)(*)*")
    conformer = picture.GetConformer()
    for index, (x, y) in {0: (0.0, 1.5), 1: (0.0, 0.0), 2: (-1.5, 0.0), 3: (1.5, 0.0),
                          4: (0.0, -1.5)}.items():
        conformer.SetAtomPosition(index, (x, y, 0.0))
    for index, label in {0: "Me", 2: "Ph", 3: "Ph", 4: "Ph"}.items():
        picture.GetAtomWithIdx(index).SetProp("molFileAlias", label)
    smiles = "C[P+](c1ccccc1)(c1ccccc1)c1ccccc1"
    block = draw(smiles, as_molscribe(picture))
    assert read_back(block) == canonical(smiles)
    out = Chem.MolFromMolBlock(block)
    p = places(block)
    phosphorus = next(a for a in out.GetAtoms() if a.GetSymbol() == "P")
    methyl = next(n for n in phosphorus.GetNeighbors() if n.GetDegree() == 1)
    others = [n for n in phosphorus.GetNeighbors() if n.GetIdx() != methyl.GetIdx()]
    assert all(p[methyl.GetIdx()][1] > p[n.GetIdx()][1] + 1.0 for n in others)


def test_the_label_of_a_placeholder_that_stays_is_kept():
    block = draw("*c1ccccc1", abbreviated("*c1ccccc1", {0: "Ar"}))
    assert "Ar" in block.split("\n")
    assert recognition._smiles_of(recognition._mol_from_molfile(block)) == "*c1ccccc1"


# --------------------------------------------------------------------------- salts

def boxes(block: str) -> list[tuple[float, float, float, float]]:
    mol = Chem.MolFromMolBlock(block)
    p = places(block)
    out = []
    for fragment in Chem.GetMolFrags(mol):
        xs, ys = [p[i][0] for i in fragment], [p[i][1] for i in fragment]
        out.append((min(xs), min(ys), max(xs), max(ys)))
    return sorted(out)


@pytest.mark.parametrize("smiles", [
    "O=C([O-])c1ccccc1.[Na+]",
    "CCCCn1cc[n+](C)c1.F[B-](F)(F)F",
    "CN(C)C(=N)NC(=N)N.Cl",
    "C[N+](C)(C)Cc1ccccc1.[Br-].O",
])
def test_the_fragments_of_a_salt_are_set_side_by_side_in_a_fresh_layout(smiles):
    block = draw(smiles, None)
    assert read_back(block) == canonical(smiles)
    found = boxes(block)
    assert len(found) == smiles.count(".") + 1
    for left, right in zip(found, found[1:]):
        assert right[0] - left[2] >= depiction.BOND * 0.99      # a clear gap, no overlap


def test_the_ions_of_a_salt_stay_where_the_picture_has_them():
    smiles = "O=C([O-])c1ccccc1.[Na+]"
    mol = layout(smiles)
    conformer = mol.GetConformer()
    sodium = next(a.GetIdx() for a in mol.GetAtoms() if a.GetSymbol() == "Na")
    conformer.SetAtomPosition(sodium, (0.0, -6.0, 0.0))          # drawn BELOW the anion
    block = draw(smiles, as_molscribe(mol))
    out = Chem.MolFromMolBlock(block)
    p = places(block)
    na = next(a.GetIdx() for a in out.GetAtoms() if a.GetSymbol() == "Na")
    assert all(p[na][1] < p[i][1] - 1.0 for i in p if i != na)


def test_a_group_drawn_out_over_the_counter_ion_pushes_it_aside():
    """"Ph" expands to a ring that reaches the place where "Br-" stands."""
    picture = layout("*[P+](C)(C)C.[Br-]")
    conformer = picture.GetConformer()
    for index, (x, y) in {0: (1.5, 0.0), 1: (0.0, 0.0), 2: (-1.5, 0.0), 3: (0.0, 1.5),
                          4: (0.0, -1.5), 5: (3.6, 0.0)}.items():
        conformer.SetAtomPosition(index, (x, y, 0.0))
    picture.GetAtomWithIdx(0).SetProp("molFileAlias", "Ph")
    smiles = "C[P+](C)(C)c1ccccc1.[Br-]"
    block = draw(smiles, as_molscribe(picture))
    assert read_back(block) == canonical(smiles)
    found = boxes(block)
    assert found[1][0] - found[0][2] >= depiction.BOND * 0.99
    out = Chem.MolFromMolBlock(block)
    assert depiction.clash_score(out, places(block)) == 0


# --------------------------------------------------------------------------- no usable drawing

GOOD_LAYOUTS = [
    "CC(C)(C)c1cccc(-c2ccccc2[C@H](N)C)c1",
    "Cc1ccc(NC(=O)c2ccc(CN3CCN(C)CC3)cc2)cc1Nc1nccc(-c2cccnc2)n1",
    "CC(C)c1c(C(=O)Nc2ccccc2)c(-c2ccccc2)c(-c2ccc(F)cc2)n1CC[C@@H](O)C[C@@H](O)CC(=O)O",
    "CC1(C)S[C@@H]2[C@H](NC(=O)Cc3ccccc3)C(=O)N2[C@H]1C(=O)O",
    "CCCc1nn(C)c2c(=O)[nH]c(-c3cc(S(=O)(=O)N4CCN(C)CC4)ccc3OCC)nc12",
    "C1CCCCCCCCCCC1",
    "O=C1CCCCCCCCCCCCCCO1",
    "CCO",
    "C",
    "[Na+].[Cl-]",
]


@pytest.mark.parametrize("smiles", GOOD_LAYOUTS)
def test_the_computed_layout_has_no_overlap_and_sensible_bonds(smiles):
    mol = Chem.MolFromSmiles(smiles)
    fresh = depiction.fresh_layout(mol)
    assert fresh is not None and len(fresh) == mol.GetNumAtoms()
    assert depiction.clash_score(mol, fresh) == 0
    lengths = [math.dist(fresh[b.GetBeginAtomIdx()], fresh[b.GetEndAtomIdx()]) for b in mol.GetBonds()]
    if lengths:
        assert statistics.median(lengths) == pytest.approx(depiction.BOND, abs=0.01)
        assert min(lengths) > 0.7 * depiction.BOND and max(lengths) < 1.4 * depiction.BOND
    block = draw(smiles, None)
    assert read_back(block) == canonical(smiles)


def test_the_better_of_two_layouts_is_taken(monkeypatch):
    """When RDKit's own layout has a clash, CoordGen's is tried and the one
    with fewer clashes wins."""
    from rdkit.Chem import rdCoordGen

    mol = Chem.MolFromSmiles("CCc1ccccc1")
    real = rdDepictor.Compute2DCoords
    asked = []

    def piled_up(work, **kwargs):
        real(work)
        conformer = work.GetConformer()
        conformer.SetAtomPosition(0, conformer.GetAtomPosition(4))      # an atom on another

    def coordgen(work):
        asked.append(True)
        real(work)

    monkeypatch.setattr(rdDepictor, "Compute2DCoords", piled_up)
    monkeypatch.setattr(rdCoordGen, "AddCoords", coordgen)
    fresh = depiction.fresh_layout(mol)
    assert asked and depiction.clash_score(mol, fresh) == 0


@pytest.mark.parametrize("smiles, molscribe_smiles", [
    ("CC(N)c1ccccn1", "CC(N)c1ccccc1"),          # another element in the drawing
    ("CCOC(=O)c1ccccc1", "CCO"),                 # most of the molecule missing from the drawing
    ("CCO", "CC(N)c1ccccc1"),                    # the drawing is another, larger molecule
])
def test_a_drawing_of_another_structure_gives_the_computed_layout(smiles, molscribe_smiles):
    block = draw(smiles, as_molscribe(layout(molscribe_smiles, 90.0)))
    assert read_back(block) == canonical(smiles)
    out = Chem.MolFromMolBlock(block)
    assert depiction.clash_score(out, places(block)) == 0


def test_a_bond_order_or_charge_that_differs_still_uses_the_pictures_places():
    """MolScribe's two answers sometimes differ in a bond order or a charge.
    The SMILES is the structure; the atoms are still where the picture has
    them."""
    picture = layout("CC(=O)c1ccccc1", 90.0)              # the drawing has a ketone
    block = draw("CC(O)c1ccccc1", as_molscribe(picture))  # the SMILES an alcohol
    assert read_back(block) == "CC(O)c1ccccc1"
    reference = Chem.RWMol(picture)
    reference.GetBondBetweenAtoms(1, 2).SetBondType(Chem.BondType.SINGLE)
    Chem.SanitizeMol(reference)
    same_shape(block, reference.GetMol())


def test_atoms_on_one_spot_are_no_drawing_to_keep():
    mol = layout("CCOCC")
    conformer = mol.GetConformer()
    for i in range(mol.GetNumAtoms()):
        conformer.SetAtomPosition(i, (2.0, -3.0, 0.0))
    block = draw("CCOCC", Chem.MolToMolBlock(mol), None, None)
    out = Chem.MolFromMolBlock(block)
    assert read_back(block) == "CCOCC" and depiction.clash_score(out, places(block)) == 0


def test_nothing_raises_and_a_single_atom_is_drawn():
    assert read_back(draw("C", None)) == "C"
    assert read_back(draw("[Na+]", as_molscribe(layout("[Na+]")))) == "[Na+]"
    assert depiction.drawing_molfile(Chem.MolFromSmiles("CCO"), object()) is not None


def test_reads_back_as_tells_stereo_apart():
    block = Chem.MolToMolBlock(layout(AMINE))
    assert depiction.reads_back_as(block, Chem.MolFromSmiles(AMINE))
    assert not depiction.reads_back_as(block, Chem.MolFromSmiles("C[C@@H](N)c1ccccc1"))
    assert not depiction.reads_back_as(block, Chem.MolFromSmiles("CC(N)c1ccccc1"))
    assert not depiction.reads_back_as("junk", Chem.MolFromSmiles("C"))


# --------------------------------------------------------------------------- through recognition

def test_build_result_passes_the_place_in_the_picture_on():
    """``recognize`` end to end on the host side: the worker's ``seen``
    reaches the drawing, and the card gets the picture's shape."""
    smiles = "CC(C)(C)c1cccc(-c2ccccc2[C@H](N)C)c1"
    mol = layout(smiles, 90.0)
    raw = {"width": 640, "height": 400, "reactions": [],
           "molecules": [{"box": [300, 0, 620, 400], "seen": SEEN, "smiles": smiles,
                          "molfile": as_molscribe(mol), "confidence": 0.9}]}
    result = recognition.build_result(raw, "molecule", 1.0)
    molecule = result["molecules"][0]
    assert molecule["smiles"] == canonical(smiles) and molecule["warnings"] == ["stereo"]
    same_shape(molecule["molfile"], mol)
    assert len(drawn_wedges(molecule["molfile"])) == 1
    described = recognition.describe_structures([{"molfile": molecule["molfile"]}])
    assert described["structures"][0]["smiles"] == molecule["smiles"]


def test_an_older_worker_without_seen_still_gets_a_drawing():
    smiles = "CC(N)c1ccccc1"
    raw = {"width": 320, "height": 400, "reactions": [],
           "molecules": [{"box": BOX, "smiles": smiles, "molfile": as_molscribe(layout(smiles)),
                          "confidence": 0.9}]}
    molecule = recognition.build_result(raw, "molecule", 1.0)["molecules"][0]
    assert read_back(molecule["molfile"]) == canonical(smiles)
    lengths = bond_lengths(molecule["molfile"])
    assert min(lengths) > 0.5 * depiction.BOND


def test_the_chemdraw_document_keeps_the_shape_and_the_wedges():
    """"Open in ChemDraw" gets the same molfile: cdxml.py keeps given
    coordinates and wedges, so the document shows the picture's shape."""
    import xml.etree.ElementTree as ET

    from compound_registry import cdxml

    smiles = "C[C@H](N)c1ccccc1"
    mol = layout(smiles, 90.0)
    block = draw(smiles, as_molscribe(mol))
    text = cdxml.render_reaction([cdxml.ReactionStep((block,), (), (block,), ())])
    root = ET.fromstring(text[text.index("<CDXML"):])
    fragment = root.find(".//fragment")
    nodes = fragment.findall("n")
    bonds = fragment.findall("b")
    assert len(nodes) == 9
    displays = [b.get("Display") for b in bonds if b.get("Display")]
    wedge = list(drawn_wedges(block).values())[0]
    assert displays == ["WedgeBegin" if wedge == 1 else "WedgedHashBegin"]
    # The same shape: CDXML's y grows downwards, the molfile's upwards.
    got = {i: tuple(float(v) for v in node.get("p").split()) for i, node in enumerate(nodes)}
    ref = places(block)
    scale = cdxml.BOND_LENGTH / depiction.BOND
    cg = (sum(p[0] for p in got.values()) / 9, sum(p[1] for p in got.values()) / 9)
    cr = (sum(p[0] for p in ref.values()) / 9, sum(p[1] for p in ref.values()) / 9)
    for i in range(9):
        assert got[i][0] - cg[0] == pytest.approx((ref[i][0] - cr[0]) * scale, abs=0.3)
        assert got[i][1] - cg[1] == pytest.approx(-(ref[i][1] - cr[1]) * scale, abs=0.3)
