"""The ChemDraw writer (``cdxml.render_route``): structure of the document,
the drawings and the layout. tests/test_cdxml_chemdraw_live.py opens the
result in the real ChemDraw (opt-in)."""

from __future__ import annotations

import itertools
import math
import statistics
import xml.etree.ElementTree as ET

import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from compound_registry import cdxml, routes
from compound_registry.route_model import Participant, Route, Step, Text
from test_routes import Data, molfile_of, example_route, example_world


# --------------------------------------------------------------------------
# Reading the document back
# --------------------------------------------------------------------------

class Doc:
    def __init__(self, text: str):
        assert text.startswith('<?xml version="1.0" encoding="UTF-8" ?>\n<!DOCTYPE CDXML')
        self.text = text
        self.root = ET.fromstring(text)
        assert self.root.tag == "CDXML"
        self.page = self.root.find("page")
        self.by_id: dict[str, ET.Element] = {}
        for el in self.root.iter():
            if "id" in el.attrib and el.tag != "font":
                assert el.attrib["id"] not in self.by_id, f"duplicate id {el.attrib['id']}"
                self.by_id[el.attrib["id"]] = el
        self.steps = [
            {k: v.split() for k, v in s.attrib.items() if k.startswith("ReactionStep")}
            for s in self.root.iter("step")]

    def drawn(self) -> list[ET.Element]:
        return [el for el in self.page if el.tag in ("fragment", "t", "arrow")]

    def box(self, el: ET.Element) -> tuple[float, ...]:
        return tuple(float(v) for v in el.attrib["BoundingBox"].split())

    def text_of(self, el: ET.Element) -> str:
        return "".join(s.text or "" for s in el.iter("s"))

    def texts(self, ids: list[str]) -> list[str]:
        return [self.text_of(self.by_id[i]) for i in ids]

    def fragments(self, ids: list[str]) -> list[ET.Element]:
        return [self.by_id[i] for i in ids if self.by_id[i].tag == "fragment"]


def render(route: Route) -> Doc:
    return Doc(cdxml.render_route(route))


def participant(key: str, molfile: str | None, name: str | None = None,
                label: str | None = None) -> Participant:
    return Participant(key, molfile, name, label, label)


def step(product: Participant, reactants, above=(), below=()) -> Step:
    return Step(product, tuple(reactants), tuple(above), tuple(below), ())


def overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def assert_no_overlap(doc: Doc) -> None:
    for a, b in itertools.combinations(doc.drawn(), 2):
        assert not overlap(doc.box(a), doc.box(b)), (a.attrib.get("id"), b.attrib.get("id"))
    page = doc.box(doc.page)
    for el in doc.drawn():
        x0, y0, x1, y1 = doc.box(el)
        assert page[0] <= x0 and page[1] <= y0 and x1 <= page[2] and y1 <= page[3]


def atoms_of(fragment: ET.Element) -> list[ET.Element]:
    return list(fragment.iter("n"))


def arrow_of(doc: Doc, s: dict) -> tuple[float, float, float]:
    """``(tail x, head x, y)`` of a step's arrow."""
    (arrow,) = [doc.by_id[i] for i in s["ReactionStepArrows"]]
    x1, y = (float(v) for v in arrow.get("Head3D").split()[:2])
    return float(arrow.get("Tail3D").split()[0]), x1, y


# --------------------------------------------------------------------------
# The example route
# --------------------------------------------------------------------------

def test_oracle_route_renders_as_a_four_step_scheme():
    route = example_route()
    doc = render(route)
    assert len(doc.steps) == 4
    for s, rstep in zip(doc.steps, route.steps):
        for i in s["ReactionStepReactants"] + s["ReactionStepProducts"]:
            assert doc.by_id[i].tag in ("fragment", "t")
        assert [doc.by_id[i].tag for i in s["ReactionStepArrows"]] == ["arrow"]
        assert all(doc.by_id[i].tag == "t" for i in s.get("ReactionStepObjectsBelowArrow", []))
        # Above the arrow: the reactants drawn over it (also listed as
        # reactants), then the reagent lines.
        above = s.get("ReactionStepObjectsAboveArrow", [])
        over = [i for i in above if doc.by_id[i].tag == "fragment"]
        assert above[:len(over)] == over and set(over) <= set(s["ReactionStepReactants"])
        assert doc.texts(above[len(over):]) == [t.text for t in rstep.above]
        assert doc.texts(s.get("ReactionStepObjectsBelowArrow", [])) == [t.text for t in rstep.below]
    assert doc.texts(doc.steps[3]["ReactionStepObjectsBelowArrow"]) == \
        ["ABC-008, SM = 85.0 mg, 70 %"]
    assert_no_overlap(doc)


def test_oracle_continues_rows_and_redraws_after_a_wrap():
    doc = render(example_route())
    s1, s2, s3, s4 = doc.steps
    # Step 2 continues the row of step 1: the same drawing of EX0001_01;
    # phenylethylamine is drawn over the arrow, not after EX0001_01.
    assert s1["ReactionStepProducts"][0] == s2["ReactionStepReactants"][0]
    assert s2["ReactionStepReactants"][1:] == s2["ReactionStepObjectsAboveArrow"][:1]
    (amine,) = doc.fragments(s2["ReactionStepReactants"][1:])
    assert len(atoms_of(amine)) == 9
    tail, head, y = arrow_of(doc, s2)
    x0, _, x1, y1 = doc.box(amine)
    assert y1 < y and tail < x0 and x1 < head
    # The first row is full: EX0002_01 is drawn again to start the second row.
    assert not set(s2["ReactionStepProducts"]) & set(s3["ReactionStepReactants"])
    assert s3["ReactionStepProducts"] == s4["ReactionStepReactants"]
    product2 = doc.fragments(s2["ReactionStepProducts"])
    again = doc.fragments(s3["ReactionStepReactants"])[:1]
    assert len(atoms_of(product2[0])) == len(atoms_of(again[0])) == 22
    labels = [doc.text_of(t) for t in doc.page.iter("t") if t.find("s").get("face") == "1"]
    assert labels == ["EX0001_01", "EX0002_01", "EX0002_01", "EX0003_01", "EX0004_01"]


def test_every_drawing_has_the_atoms_and_bonds_of_its_molfile():
    route = example_route()
    doc = render(route)
    for s, rstep in zip(doc.steps, route.steps):
        frags = doc.fragments(s["ReactionStepReactants"])
        expected = [p for p in rstep.reactants]
        mols = [routes.read_molfile(p.molfile) for p in expected]
        assert sum(len(atoms_of(f)) for f in frags) == sum(m.GetNumAtoms() for m in mols)
        assert sum(len(f.findall("b")) for f in frags) == sum(m.GetNumBonds() for m in mols)
        product = routes.read_molfile(rstep.product.molfile)
        (pf,) = doc.fragments(s["ReactionStepProducts"])
        assert len(atoms_of(pf)) == product.GetNumAtoms()
        assert len(pf.findall("b")) == product.GetNumBonds()


def test_methyllithium_is_one_fragment_with_its_lithium():
    doc = render(example_route())
    frags = doc.fragments(doc.steps[2]["ReactionStepReactants"])
    meli = [f for f in frags if any(n.get("Element") == "3" for n in atoms_of(f))]
    assert len(meli) == 1 and len(atoms_of(meli[0])) == 2 and len(meli[0].findall("b")) == 1


def test_hashed_wedges_keep_their_start_atom():
    route = example_route()
    doc = render(route)
    (frag,) = doc.fragments(doc.steps[3]["ReactionStepProducts"])          # EX0004_01
    mol = routes.read_molfile(route.steps[3].product.molfile)
    hashed = [b for b in mol.GetBonds()
              if b.HasProp("_MolFileBondStereo") and b.GetIntProp("_MolFileBondStereo") == 6]
    assert hashed
    nodes = [n.get("id") for n in atoms_of(frag)]
    shown = [(nodes.index(b.get("B")), nodes.index(b.get("E")))
             for b in frag.findall("b") if b.get("Display") == "WedgedHashBegin"]
    assert shown == [(b.GetBeginAtomIdx(), b.GetEndAtomIdx()) for b in hashed]


def test_drawn_orientation_is_kept_and_scaled_to_the_chemdraw_bond_length():
    route = example_route()
    doc = render(route)
    (frag,) = doc.fragments(doc.steps[0]["ReactionStepProducts"])
    mol = routes.read_molfile(route.steps[0].product.molfile)
    pts = [tuple(float(v) for v in n.get("p").split()) for n in atoms_of(frag)]
    ids = [n.get("id") for n in atoms_of(frag)]
    lengths = [math.dist(pts[ids.index(b.get("B"))], pts[ids.index(b.get("E"))])
               for b in frag.findall("b")]
    assert statistics.median(lengths) == pytest.approx(14.4, abs=0.02)
    conf = mol.GetConformer()
    # Same shape: x keeps its direction, y is flipped (ChemDraw's y grows downwards).
    a, b = conf.GetAtomPosition(0), conf.GetAtomPosition(7)
    assert (pts[7][0] - pts[0][0]) * (b.x - a.x) > 0
    assert (pts[7][1] - pts[0][1]) * (b.y - a.y) < 0


def test_molecules_and_plus_signs_are_centred_on_the_arrow_line():
    doc = render(example_route())
    for s in doc.steps:
        (arrow,) = [doc.by_id[i] for i in s["ReactionStepArrows"]]
        y = float(arrow.get("Head3D").split()[1])
        for el in doc.fragments(s["ReactionStepProducts"]):
            x0, y0, x1, y1 = doc.box(el)
            assert (y0 + y1) / 2 == pytest.approx(y, abs=0.05)
        for t in doc.page.iter("t"):
            if doc.text_of(t) == "+":
                ty = float(t.get("p").split()[1])
                assert any(ty == pytest.approx(float(a.get("Head3D").split()[1]) + 3.5, abs=0.05)
                           for a in doc.page.iter("arrow"))
                assert t.get("Justification") == "Center"
    # A structure over an arrow is centred on it horizontally.
    s2 = doc.steps[1]
    (amine,) = doc.fragments(s2["ReactionStepObjectsAboveArrow"])
    x0, _, x1, _ = doc.box(amine)
    tail, head, _ = arrow_of(doc, s2)
    assert (x0 + x1) / 2 == pytest.approx((tail + head) / 2, abs=0.05)


def test_rendering_is_deterministic_and_ids_start_at_one():
    text = cdxml.render_route(example_route())
    assert text == cdxml.render_route(example_route())
    doc = Doc(text)
    ids = sorted(int(i) for i in doc.by_id)
    assert ids == list(range(1, len(ids) + 1))
    assert doc.page.get("id") == "1"


# --------------------------------------------------------------------------
# Synthetic routes
# --------------------------------------------------------------------------

def mol2d(smiles: str) -> str:
    return molfile_of(smiles)


def test_texts_formula_subscripts_wrapping_and_arrow_length():
    long = Text("first part of a long condition, second part of it, third part, fourth part "
                "of the line")
    route = Route(participant("cs:B", mol2d("CCO"), label="B1"), (
        step(participant("cs:B", mol2d("CCO"), label="B1"),
             [participant("ik:a", mol2d("CC=O"), "acetaldehyde")],
             above=[Text("Pd(PPh3)4", True), long], below=[Text("CH2Cl2, 40 °C", True),
                                                           Text("E-1, 50 %")]),))
    doc = render(route)
    (s,) = doc.steps
    above = doc.texts(s["ReactionStepObjectsAboveArrow"])
    assert above[0] == "Pd(PPh3)4"
    assert len(above) >= 3 and " ".join(above[1:]) == long.text
    assert all(line.endswith(",") for line in above[1:-1])
    assert all(cdxml.text_width(line) <= cdxml.MAX_LINE_WIDTH for line in above[1:])
    runs = [(r.text, r.get("face")) for r in doc.by_id[s["ReactionStepObjectsAboveArrow"][0]]]
    assert runs == [("Pd(PPh", "0"), ("3", "32"), (")", "0"), ("4", "32")]
    runs = [(r.text, r.get("face")) for r in doc.by_id[s["ReactionStepObjectsBelowArrow"][0]]]
    assert runs == [("CH", "0"), ("2", "32"), ("Cl", "0"), ("2", "32"), (", 40 °C", "0")]
    (arrow,) = [doc.by_id[i] for i in s["ReactionStepArrows"]]
    x1 = float(arrow.get("Head3D").split()[0])
    x0 = float(arrow.get("Tail3D").split()[0])
    widest = max(cdxml.text_width(t) for t in above + doc.texts(s["ReactionStepObjectsBelowArrow"]))
    assert x1 - x0 == pytest.approx(max(90, widest + 20))
    assert_no_overlap(doc)


def test_short_texts_get_the_minimum_arrow():
    p = participant("cs:B", mol2d("CCO"), label="B1")
    doc = render(Route(p, (step(p, [participant("ik:a", mol2d("C"), "methane")]),)))
    (arrow,) = doc.page.iter("arrow")
    assert float(arrow.get("Head3D").split()[0]) - float(arrow.get("Tail3D").split()[0]) == 90
    assert "ReactionStepObjectsAboveArrow" not in doc.steps[0]


def test_participant_without_structure_is_its_name_and_texts_are_escaped():
    product = participant("cs:B", mol2d("CCO"), label="B&1")
    route = Route(product, (step(product, [participant("nm:x", None, "Grubbs <II> & co"),
                                           participant("rw:r:2", None, None)],
                                 below=[Text("E-1 (A & B), 5 %")]),))
    text = cdxml.render_route(route)
    doc = Doc(text)
    (s,) = doc.steps
    names = doc.texts(s["ReactionStepReactants"])
    assert names == ["Grubbs <II> & co", "?"]
    assert "&amp;" in text and "&lt;II&gt;" in text
    assert doc.texts(s["ReactionStepObjectsBelowArrow"]) == ["E-1 (A & B), 5 %"]
    assert_no_overlap(doc)


def test_salt_charges_isotopes_and_explicit_hydrogens_are_kept():
    salt = mol2d("C[NH3+].[Cl-]")
    labelled = mol2d("[13CH3]C(=O)O[2H]")
    m = Chem.AddHs(Chem.MolFromSmiles("CO"))
    AllChem.Compute2DCoords(m)
    explicit = Chem.MolToMolBlock(m)
    product = participant("cs:P", labelled, label="P1")
    route = Route(product, (step(product, [participant("ik:s", salt, "salt"),
                                           participant("ik:h", explicit, "methanol")]),))
    doc = render(route)
    (s,) = doc.steps
    frags = doc.fragments(s["ReactionStepReactants"])
    assert len(frags) == 3                              # ammonium, chloride, methanol
    charges = sorted(n.get("Charge") for f in frags for n in atoms_of(f) if n.get("Charge"))
    assert charges == ["-1", "1"]
    assert sum(1 for n in atoms_of(frags[2]) if n.get("Element") == "1") == 4
    (pf,) = doc.fragments(s["ReactionStepProducts"])
    isotopes = sorted((n.get("Element", "6"), n.get("Isotope")) for n in atoms_of(pf)
                      if n.get("Isotope"))
    assert isotopes == [("1", "2"), ("6", "13")]
    assert_no_overlap(doc)


def test_radicals_are_kept():
    tempo = mol2d("CC1(C)CCCC(C)(C)N1[O]")
    assert "M  RAD" in tempo
    product = participant("cs:P", mol2d("C1CC1"), label="P1")
    route = Route(product, (step(product, [participant("ik:t", tempo, "TEMPO"),
                                           participant("ik:c", mol2d("[CH2]"), "carbene")]),))
    doc = render(route)
    (s,) = doc.steps
    tempo_f, carbene_f = doc.fragments(s["ReactionStepReactants"])
    assert [(n.get("Element"), n.get("Radical")) for n in atoms_of(tempo_f)
            if n.get("Radical")] == [("8", "Doublet")]
    assert [n.get("Radical") for n in atoms_of(carbene_f)] == ["Triplet"]
    (pf,) = doc.fragments(s["ReactionStepProducts"])
    assert not any(n.get("Radical") for n in atoms_of(pf))


def test_aromatic_input_is_drawn_with_kekule_bonds():
    aromatic = Chem.MolToMolBlock(Chem.MolFromSmiles("c1ccc2ccccc2c1"), kekulize=False)
    assert "  1  2  4" in aromatic or " 4  0" in aromatic
    p = participant("cs:N", aromatic, label="N1")
    doc = render(Route(p, ()))
    (frag,) = doc.page.iter("fragment")
    orders = [b.get("Order", "1") for b in frag.findall("b")]
    assert sorted(orders) == ["1"] * 6 + ["2"] * 5


def test_wedge_hash_and_wavy_bonds():
    base = mol2d("CC(N)C(F)Cl")
    lines = base.split("\n")
    counts = lines[3]
    n_atoms = int(counts[:3])
    bond_lines = list(range(4 + n_atoms, 4 + n_atoms + int(counts[3:6])))
    flags = {0: "  1", 1: "  6", 3: "  4"}
    for k, flag in flags.items():
        line = lines[bond_lines[k]]
        lines[bond_lines[k]] = line[:9] + flag + line[12:]
    p = participant("cs:W", "\n".join(lines), label="W1")
    doc = render(Route(p, ()))
    (frag,) = doc.page.iter("fragment")
    displays = [b.get("Display") for b in frag.findall("b")]
    assert displays[0] == "WedgeBegin" and displays[1] == "WedgedHashBegin"
    assert displays[3] == "Wavy" and displays[2] is None


def test_a_route_draws_a_double_bond_marked_either_as_wavy_and_no_other():
    """Changed on purpose in 0.12.0 (CHANGELOG, "ChemDraw routes"): a double
    bond the molfile marks "E or Z unknown" (bond stereo 3, which RDKit
    writes for an oxime read from a SMILES without / or \\) is drawn wavy in
    a ROUTE too; without it ChemDraw would read a definite E or Z from the
    coordinates. A double bond without the mark (geometry given by the
    drawing, or written by a program that leaves the field 0) stays plain,
    as in 0.11.0."""
    def double_bond_display(molfile: str) -> str | None:
        doc = render(Route(participant("cs:D", molfile, label="D1"), ()))
        (frag,) = doc.page.iter("fragment")
        (double,) = [b for b in frag.findall("b") if b.get("Order") == "2"]
        return double.get("Display")

    unknown = mol2d("CC(C)=NO")      # symmetric: no geometry at all ...
    assert double_bond_display(unknown) is None
    either = mol2d("CCC(C)=NO")      # ... this one has E or Z, and the SMILES says neither
    (line,) = [l for l in either.split("\n") if l[6:9] == "  2"]
    assert line[9:12] == "  3"
    assert double_bond_display(either) == "Wavy"
    assert double_bond_display(mol2d("CC/C(C)=N/O")) is None
    plain = "\n".join(l[:9] + "  0" + l[12:] if l[6:9] == "  2" and len(l) >= 12 else l
                      for l in either.split("\n"))
    assert double_bond_display(plain) is None


def test_three_d_and_flat_files_get_new_2d_coordinates():
    m = Chem.AddHs(Chem.MolFromSmiles("C[C@H](N)C(=O)O"))
    AllChem.EmbedMolecule(m, randomSeed=7)
    three_d = Chem.MolToMolBlock(Chem.RemoveHs(m))
    flat = Chem.MolFromSmiles("CC(C)O")
    AllChem.Compute2DCoords(flat)
    conf = flat.GetConformer()
    for i in range(flat.GetNumAtoms()):
        conf.SetAtomPosition(i, (0.0, 0.0, 0.0))
    zero = Chem.MolToMolBlock(flat)
    for molfile, wedges in ((three_d, 1), (zero, 0)):
        p = participant("cs:Z", molfile, label="Z1")
        doc = render(Route(p, ()))
        (frag,) = doc.page.iter("fragment")
        pts = [tuple(float(v) for v in n.get("p").split()) for n in atoms_of(frag)]
        ids = [n.get("id") for n in atoms_of(frag)]
        lengths = [math.dist(pts[ids.index(b.get("B"))], pts[ids.index(b.get("E"))])
                   for b in frag.findall("b")]
        assert min(lengths) > 10 and statistics.median(lengths) == pytest.approx(14.4, abs=0.05)
        assert sum(1 for b in frag.findall("b") if b.get("Display")) == wedges


def test_unreadable_structure_is_written_as_its_name():
    p = participant("cs:U", "garbage", name="U1", label="U1")
    doc = render(Route(p, ()))
    assert not list(doc.page.iter("fragment"))
    assert [doc.text_of(t) for t in doc.page.iter("t")] == ["U1", "U1"]


def chain_route(n: int) -> Route:
    """n steps, each product the reactant of the next (alkyl chain)."""
    parts = [participant(f"cs:C{i}", mol2d("C" * (i + 3) + "O"), label=f"C{i:04d}_01")
             for i in range(n + 1)]
    steps = tuple(step(parts[i + 1], [parts[i], participant(f"ik:{i}", mol2d("CI"), "MeI")],
                       below=[Text(f"E-{i + 1}, SM = 10.0 mg, 50 %")]) for i in range(n))
    return Route(parts[-1], steps)


def test_long_rows_wrap_and_redraw_the_last_product():
    route = chain_route(6)
    doc = render(route)
    assert len(doc.steps) == 6
    wraps = 0
    for prev, cur in zip(doc.steps, doc.steps[1:]):
        shared = set(prev["ReactionStepProducts"]) & set(cur["ReactionStepReactants"])
        mei = cur["ReactionStepReactants"][1]
        if not shared:
            wraps += 1
            first = doc.fragments(cur["ReactionStepReactants"])[0]
            prod = doc.fragments(prev["ReactionStepProducts"])[0]
            assert len(atoms_of(first)) == len(atoms_of(prod))
            # The step starts the new row: MeI left of the arrow, after a "+".
            assert "ReactionStepObjectsAboveArrow" not in cur
            assert doc.box(doc.by_id[mei])[2] < arrow_of(doc, cur)[0]
        else:
            assert cur["ReactionStepObjectsAboveArrow"] == [mei]
    assert wraps >= 1
    width = cdxml.MAX_ROW_WIDTH + 2 * cdxml.SLIDE_MARGIN
    for el in doc.drawn():
        assert doc.box(el)[2] <= width
    assert_no_overlap(doc)


def test_a_step_not_using_the_last_product_starts_a_new_row():
    a = participant("cs:A", mol2d("CCO"), label="A1")
    b = participant("cs:B", mol2d("CCCO"), label="B1")
    c = participant("cs:C", mol2d("CCCCOCCO"), label="C1")
    m = participant("ik:m", mol2d("C=O"), "formaldehyde")
    route = Route(c, (step(a, [m]), step(b, [m]), step(c, [a, b])))
    doc = render(route)
    s1, s2, s3 = doc.steps
    arrows = [float(doc.by_id[s["ReactionStepArrows"][0]].get("Head3D").split()[1])
              for s in doc.steps]
    assert arrows[0] < arrows[1] == arrows[2]          # step 2 on a new row; step 3 continues it
    assert s3["ReactionStepReactants"][0] in s2["ReactionStepProducts"]
    assert not set(s3["ReactionStepReactants"]) & set(s1["ReactionStepProducts"])  # A drawn again
    assert_no_overlap(doc)


def over_route() -> Route:
    """A -> B, then B + C (registered) + D (a salt) + "Grubbs II" -> E: the
    second step continues the row with its other reactants over the arrow."""
    a = participant("ik:a", mol2d("CCCCCCBr"), "bromohexane")
    b = participant("cs:B", mol2d("c1ccccc1C=O"), label="B0001_01")
    c = participant("cs:C", mol2d("NC(c1ccccc1)CO"), label="C0002_01")
    d = participant("ik:d", mol2d("[Na+].[BH4-]"), "NaBH4")
    g = participant("nm:g", None, "Grubbs II")
    e = participant("cs:E", mol2d("c1ccccc1CNC(c1ccccc1)CO"), label="E0003_01")
    return Route(e, (step(b, [a], above=[Text("Mg")], below=[Text("E-1, 50 %")]),
                     step(e, [b, c, d, g], above=[Text("AcOH"), Text("molecular sieves 4 Å")],
                          below=[Text("MeOH"), Text("E-2, SM = 1.00 g, 70 %")])))


def test_other_reactants_of_a_continued_step_are_drawn_over_its_arrow():
    doc = render(over_route())
    s1, s2 = doc.steps
    tail, head, y = arrow_of(doc, s2)
    reactants = s2["ReactionStepReactants"]
    assert reactants[0] == s1["ReactionStepProducts"][0]          # B, drawn once
    over = reactants[1:]
    # C, the two ions of NaBH4, the name of Grubbs II.
    assert [doc.by_id[i].tag for i in over] == ["fragment"] * 3 + ["t"]
    above = s2["ReactionStepObjectsAboveArrow"]
    assert above[:4] == over
    assert doc.texts(above[4:]) == ["AcOH", "molecular sieves 4 Å"]
    over_boxes = [doc.box(doc.by_id[i]) for i in over]
    # Over the arrow, centred on it, in the step's order.
    assert all(tail < b[0] and b[2] < head and b[3] < y for b in over_boxes)
    salt = over_boxes[1:3]
    assert over_boxes[0][2] < min(b[0] for b in salt)
    assert max(b[2] for b in salt) < over_boxes[3][0]
    left, right = min(b[0] for b in over_boxes), max(b[2] for b in over_boxes)
    assert (left + right) / 2 == pytest.approx((tail + head) / 2, abs=1.0)
    # Joined by "+" (between the three compounds), on their line.
    pluses = [t for t in doc.page.iter("t") if doc.text_of(t) == "+"]
    assert len(pluses) == 2
    assert all(float(t.get("p").split()[1]) < y - 10 for t in pluses)
    # The registered one has its bold label under it, still above the arrow.
    (label,) = [t for t in doc.page.iter("t") if doc.text_of(t) == "C0002_01"]
    assert label.find("s").get("face") == "1"
    lb = doc.box(label)
    assert lb[1] > over_boxes[0][3] and lb[3] < y - 3
    assert tail < lb[0] and lb[2] < head
    # The reagent lines are above all of it; the arrow is long enough for it.
    texts = [doc.box(doc.by_id[i]) for i in above[4:]]
    assert max(t[3] for t in texts) < min(b[1] for b in over_boxes + [lb])
    assert head - tail > right - left + 10
    # The product follows the arrow.
    (product,) = doc.fragments(s2["ReactionStepProducts"])
    assert doc.box(product)[0] > head
    assert_no_overlap(doc)


def test_row_below_a_tall_over_arrow_block_does_not_overlap():
    first = over_route()
    other = participant("cs:X", mol2d("CCCCCCCCO"), label="X0009_01")
    route = Route(first.target, first.steps + (
        step(other, [participant("ik:y", mol2d("CCCCCCCC=O"), "octanal")]),
        step(first.target, [other, *first.steps[1].reactants[1:]],
             above=[Text("first line"), Text("second line"), Text("third line")]),
    ))
    doc = render(route)
    s1, s2, s3, s4 = doc.steps
    assert arrow_of(doc, s3)[2] == arrow_of(doc, s4)[2] > arrow_of(doc, s2)[2]
    assert len(s4["ReactionStepObjectsAboveArrow"]) == 4 + 3
    assert_no_overlap(doc)


def test_route_without_steps_draws_the_target():
    p = participant("cs:T", mol2d("CCN"), label="T1")
    doc = render(Route(p, ()))
    assert doc.steps == [] and doc.root.find(".//scheme") is None
    assert len(list(doc.page.iter("fragment"))) == 1


def test_route_from_fake_data_with_odd_rows_renders():
    world = example_world()
    world.reactions["4180"]["reactants"].append(
        {"row": 2, "name": "Mystery", "formula": None, "cas": None, "source_experiment_ref": None,
         "source_experiment_code": None, "molfile": None, "actual": None, "planned": None})
    route = routes.build_route("EX0004_01", Data(world))
    doc = render(route)
    assert "Mystery" in doc.texts(doc.steps[-1]["ReactionStepReactants"])
    assert_no_overlap(doc)


@pytest.mark.parametrize("text,formula,runs", [
    ("K2CO3", True, [("K", False), ("2", True), ("CO", False), ("3", True)]),
    ("CuSO4·5H2O", True, [("CuSO", False), ("4", True), ("·5H", False), ("2", True), ("O", False)]),
    ("K2CO3", False, [("K2CO3", False)]),
    ("", True, []),
])
def test_formula_runs(text, formula, runs):
    assert cdxml.formula_runs(text, formula) == runs


def test_text_width_estimates():
    assert cdxml.text_width("ABC-001, SM = 2.31 g, 52.4 %") == pytest.approx(137.0, abs=0.5)
    assert cdxml.text_width("W", bold=True) > cdxml.text_width("i", bold=True)
    assert cdxml.text_width("漢") == 10.0


# --------------------------------------------------------------------------- slide page, ACS 1996 style

def test_document_uses_the_acs_document_1996_style():
    doc = render(example_route())
    for name, value in cdxml.ACS_1996.items():
        assert doc.root.get(name) == value
    assert doc.root.get("BondLength") == "14.4"


def test_page_is_one_widescreen_slide_and_the_scheme_is_centred_on_it():
    doc = render(example_route())
    assert doc.page.get("DrawingSpace") == "poster"
    assert (doc.page.get("Width"), doc.page.get("Height")) == ("960", "540")
    assert doc.page.get("BoundingBox") == "0 0 960 540"
    boxes = [doc.box(el) for el in doc.drawn()]
    left = min(b[0] for b in boxes)
    right = max(b[2] for b in boxes)
    top = min(b[1] for b in boxes)
    bottom = max(b[3] for b in boxes)
    assert left >= cdxml.SLIDE_MARGIN - 0.01 and right <= 960 - cdxml.SLIDE_MARGIN + 0.01
    assert top >= cdxml.SLIDE_MARGIN - 0.01 and bottom <= 540 - cdxml.SLIDE_MARGIN + 0.01
    assert abs(left - (960 - right)) < 1.0          # centred across
    assert abs(top - (540 - bottom)) < 1.0          # centred down (one slide)


def test_molecules_are_never_scaled():
    # A long route keeps the ACS bond length: the median bond is 14.4 pt.
    doc = render(chain_route(14))
    lengths = []
    for frag in doc.root.iter("fragment"):
        pos = {n.get("id"): tuple(map(float, n.get("p").split())) for n in frag.iter("n")}
        for b in frag.iter("b"):
            (x1, y1), (x2, y2) = pos[b.get("B")], pos[b.get("E")]
            lengths.append(math.hypot(x2 - x1, y2 - y1))
    assert abs(statistics.median(lengths) - 14.4) < 0.05
    for s in doc.root.iter("s"):
        assert float(s.get("size")) == 10.0


def test_long_route_continues_on_further_slides_without_cutting_a_row():
    doc = render(chain_route(14))
    height = float(doc.page.get("Height"))
    slides = round(height / 540)
    assert slides >= 2 and abs(height - slides * 540) < 0.01
    assert doc.page.get("Width") == "960"
    # Every arrow, with what is drawn over, above and below it, lies on one slide.
    for s in doc.steps:
        ids = [i for k, v in s.items() for i in v if k != "ReactionStepReactants"]
        boxes = [doc.box(doc.by_id[i]) for i in ids if "BoundingBox" in doc.by_id[i].attrib]
        first = {int(b[1] // 540) for b in boxes}
        last = {int((b[3] - 0.01) // 540) for b in boxes}
        assert first == last and len(first) == 1
    assert_no_overlap(doc)
