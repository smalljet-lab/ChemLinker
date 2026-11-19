"""ChemDraw documents (CDXML) of synthesis routes.

:func:`render_route` is pure: it lays a :class:`~compound_registry.route_model.Route`
out and returns the text of a CDXML document that the ChemDraw desktop
program opens as a reaction scheme (one ``<step>`` per reaction arrow, naming
its reactants, products and the texts above and below it). No I/O, no time
stamps: the same route always gives the same text.

ChemDraw does not keep those ``<step>`` elements as written: when it opens
the file it reads each row again by its geometry. Everything right of an
arrow up to the next arrow counts as that arrow's products, so a continued
row drawn "A + B -> C + D -> E" would make D a second product of the first
arrow. The layout below avoids that: the other reactants of a continued step
are drawn over its arrow. The file lists them both as the step's reactants
and as objects above its arrow; ChemDraw keeps them as objects above that
arrow (it reads any structure drawn over an arrow that way: in its reaction
SMILES they are the middle part, "A>B>C"), never as products of the arrow
before (checked by re-saving in ChemDraw 26.0, see
tests/test_cdxml_chemdraw_live.py).

What is drawn
-------------
* A compound with a structure is drawn from its molfile as the chemist drew
  it: the 2D orientation is kept (scaled so the median bond is 14.4 pt, the
  ChemDraw default); a file without usable 2D coordinates (all atoms on one
  spot, or 3D) gets coordinates from RDKit. Aromatic bonds are written as
  alternating single and double bonds; wedges, hashes and wavy bonds keep the
  atom they start at; charges, isotopes, radicals (one unpaired electron as
  a doublet, two as a triplet), explicit H atoms and every fragment (salts,
  counter-ions, the C-Li of methyllithium) are kept. ChemDraw itself
  writes the atom labels with their implicit hydrogens (a radical atom gets
  as many fewer).
* A registered compound has its ``CORE_STEREO`` in bold under its drawing. A
  compound without a structure is written as its name.
* Reagents above the arrow, solvents and the run lines below it, centred on
  the arrow. A line wider than :data:`MAX_LINE_WIDTH` is wrapped after a
  comma. Digit runs of formula-like texts (``Text.formula``) are subscripts.

Layout
------
Steps are drawn in route order, in rows. A step continues the current row
when one of its reactants is the product drawn last in that row: that drawing
is reused (its fragments are the step's reactant), then come the arrow and
the new product. The step's OTHER reactants are drawn above its arrow,
centred on it and joined by "+" (a registered one with its label under it),
with the reagent lines above them; the arrow is made long enough for them.
Otherwise the step starts a new row: its reactants joined by "+", the arrow
and the product. A row that would get wider than :data:`MAX_ROW_WIDTH`
starts again on a new row, with the last product drawn once more in front
(the step then starts that row). Molecules, "+" and texts of a row are
centred on its arrow line, those over an arrow on their own line above it;
rows are stacked so that nothing overlaps (sizes of texts are estimated with
Arial's character widths).

Style and size: ChemDraw's "ACS Document 1996" settings (:data:`ACS_1996`:
bond length 0.2 in = 14.4 pt, line 0.6 pt, bold 2.0 pt, hash spacing 2.5 pt,
margin 1.6 pt, bond spacing 18 %, chain angle 120 degrees, Arial 10 pt labels
and captions). Nothing is ever scaled, so a molecule has the same size in
every route and on every slide.

Page: slides of a widescreen PowerPoint deck (:data:`SLIDE_WIDTH` x
:data:`SLIDE_HEIGHT` points, 13.33 x 7.5 in), as ONE ChemDraw "poster" page
that is one slide wide and as many slides tall as needed. Rows wrap at the
slide's width less its margins; rows are packed onto slides top to bottom,
never split across a slide's edge; each slide's rows are centred on it
vertically, and the scheme horizontally. (A single row taller than a slide
starts at its slide's top margin and runs over.)

Coordinates are points (1/72 inch); y grows downwards. A text's position is
its baseline (``Justification="Center"`` centres it horizontally).
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from typing import Any
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from rdkit import Chem, rdBase
from rdkit.Chem import rdDepictor

from .route_model import Participant, Route, Step, Text
from .routes import read_molfile

__all__ = ["render_route", "ACS_1996", "BOND_LENGTH", "MAX_ROW_WIDTH", "MAX_LINE_WIDTH", "SLIDE_WIDTH",
           "SLIDE_HEIGHT", "SLIDE_MARGIN", "text_width",
           "formula_runs"]

#: ChemDraw's default bond length (points).
BOND_LENGTH = 14.4
#: ChemDraw's "ACS Document 1996" style, written on the document.
ACS_1996 = {
    "BondLength": "14.4", "LineWidth": "0.6", "BoldWidth": "2", "HashSpacing": "2.5",
    "MarginWidth": "1.6", "BondSpacing": "18", "ChainAngle": "120",
    "LabelFont": "3", "LabelSize": "10", "LabelFace": "96",
    "CaptionFont": "3", "CaptionSize": "10", "CaptionFace": "0",
    "LabelJustification": "Auto", "CaptionJustification": "Left",
}
#: The page: a widescreen (16:9) PowerPoint slide, in points, and its margin.
SLIDE_WIDTH = 960.0
SLIDE_HEIGHT = 540.0
SLIDE_MARGIN = 24.0
#: A row is not made wider than this (points), unless one step alone is.
MAX_ROW_WIDTH = SLIDE_WIDTH - 2 * SLIDE_MARGIN
#: Texts at an arrow wider than this (points) are wrapped after a comma.
MAX_LINE_WIDTH = 220.0

#: Unpaired electrons (RDKit) -> CDXML ``Radical``. RDKit reads a molfile's
#: singlet and triplet alike as 2 electrons; both are written as a triplet.
_RADICALS = {1: "Doublet", 2: "Triplet"}

_FONT_ID = 3
_FONT_SIZE = 10.0
_MIN_ARROW = 90.0
_ARROW_MARGIN = 20.0          # arrow length = widest text + this
_LINE_PITCH = 12.0
_ASCENT = 8.0                 # estimated text extents around the baseline (10 pt Arial)
_DESCENT = 2.5
_CENTRE_DROP = 3.5            # baseline of a text centred on a line = line + this
_ABOVE_GAP = 6.0              # arrow line (or structures over it) -> baseline of lowest text
_OVER_GAP = 8.0               # arrow line -> bottom of the structures drawn over it
_BELOW_FIRST = 14.0           # arrow line -> baseline of the first text below
_GAP_PLUS = 9.0               # molecule <-> "+"
_GAP_ARROW = 12.0             # molecule <-> arrow
_ROW_GAP = 26.0
_LABEL_GAP = 5.0              # drawing -> top of its label
_FRAGMENT_GAP = 7.0           # between the fragments of one compound (salts)
_HEAD = "WedgeBegin"
_HASH = "WedgedHashBegin"
_WAVY = "Wavy"

# Arial / Helvetica advance widths (1/1000 em), regular and bold.
_W_REG = dict(zip(
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"
    "abcdefghijklmnopqrstuvwxyz{|}~°µ",
    [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278]
    + [556] * 10
    + [278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500,
       667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278,
       278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222,
       833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334,
       584, 400, 556]))
_W_BOLD = dict(zip(
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"
    "abcdefghijklmnopqrstuvwxyz{|}~°µ",
    [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278]
    + [556] * 10
    + [333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556,
       722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333,
       278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278,
       889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389,
       584, 400, 611]))

_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_SUBSCRIPT_DIGITS = re.compile(r"(?<=[A-Za-z)\]])[0-9]+")


def text_width(text: str, size: float = _FONT_SIZE, bold: bool = False) -> float:
    """Estimated width of ``text`` in Arial (points). Unknown characters count
    as wide ones, so the estimate errs on the large side."""
    table = _W_BOLD if bold else _W_REG
    return sum(table.get(ch, 1000 if ord(ch) > 0x2E80 else 600) for ch in text) * size / 1000.0


def formula_runs(text: str, formula: bool) -> list[tuple[str, bool]]:
    """``text`` split into ``(piece, subscript)`` runs: with ``formula`` true,
    every digit run that directly follows a letter or a closing bracket is a
    subscript ("Pd(PPh3)4" -> "Pd(PPh", "3", ")", "4")."""
    if not formula:
        return [(text, False)] if text else []
    out: list[tuple[str, bool]] = []
    pos = 0
    for match in _SUBSCRIPT_DIGITS.finditer(text):
        if match.start() > pos:
            out.append((text[pos:match.start()], False))
        out.append((match.group(0), True))
        pos = match.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out


def _clean(text: str) -> str:
    return _XML_BAD.sub("", text)


def _f(value: float) -> str:
    text = f"{value:.2f}"
    return "0" if text in ("-0.00", "0.00") else text


Box = tuple[float, float, float, float]          # x0, y0, x1, y1


def _box_attr(box: Box) -> str:
    return " ".join(_f(v) for v in box)


# --------------------------------------------------------------------------
# Molecules
# --------------------------------------------------------------------------

@dataclass
class _Atom:
    x: float
    y: float
    element: int
    charge: int
    isotope: int
    radicals: int
    alias: str | None
    hydrogens: int
    bonded: bool


@dataclass
class _Bond:
    begin: int
    end: int
    order: str
    display: str | None


@dataclass
class _Drawing:
    atoms: list[_Atom]
    bonds: list[_Bond]
    fragments: list[tuple[list[int], list[int]]]       # (atom indices, bond indices)
    boxes: list[Box]                                   # one per fragment, labels included

    @property
    def box(self) -> Box:
        return (min(b[0] for b in self.boxes), min(b[1] for b in self.boxes),
                max(b[2] for b in self.boxes), max(b[3] for b in self.boxes))


_ORDERS = {Chem.BondType.DOUBLE: "2", Chem.BondType.TRIPLE: "3", Chem.BondType.AROMATIC: "1.5",
           Chem.BondType.DATIVE: "dative", Chem.BondType.DATIVEONE: "dative",
           Chem.BondType.ZERO: "0.5", Chem.BondType.QUADRUPLE: "4"}
_V2000_STEREO = {1: _HEAD, 6: _HASH, 4: _WAVY}
_V3000_CFG = {1: _HEAD, 3: _HASH, 2: _WAVY}
_DIRS = {Chem.BondDir.BEGINWEDGE: _HEAD, Chem.BondDir.BEGINDASH: _HASH,
         Chem.BondDir.UNKNOWN: _WAVY}


def _int_prop(obj: Any, name: str) -> int | None:
    try:
        return int(obj.GetProp(name)) if obj.HasProp(name) else None
    except Exception:  # noqa: BLE001
        return None


def _drawn_display(bond: Chem.Bond) -> str | None:
    """Wedge / hash / wavy as written in the molfile."""
    stereo = _int_prop(bond, "_MolFileBondStereo")
    if stereo is not None:
        return _V2000_STEREO.get(stereo)
    cfg = _int_prop(bond, "_MolFileBondCfg")
    return _V3000_CFG.get(cfg) if cfg is not None else None


def _kekulized(mol: Chem.Mol) -> Chem.Mol:
    if not any(b.GetBondType() == Chem.BondType.AROMATIC for b in mol.GetBonds()):
        return mol
    copy = Chem.Mol(mol)
    try:
        copy.UpdatePropertyCache(strict=False)
        Chem.FastFindRings(copy)
        Chem.Kekulize(copy, clearAromaticFlags=True)
        return copy
    except Exception:  # noqa: BLE001 - drawn as delocalised (1.5) bonds instead
        return mol


def _new_coordinates(mol: Chem.Mol, three_d: bool) -> tuple[Chem.Mol, dict[int, str]]:
    """2D coordinates from RDKit for a drawing without usable ones. Stereo of
    3D input is turned into wedges; a flat file's wedges mean nothing and are
    dropped."""
    try:
        work = Chem.Mol(mol)
        Chem.SanitizeMol(work)
        if three_d:
            Chem.AssignStereochemistryFrom3D(work)
        rdDepictor.Compute2DCoords(work)
        Chem.WedgeMolBonds(work, work.GetConformer())
        displays = {b.GetIdx(): _DIRS[b.GetBondDir()] for b in work.GetBonds()
                    if b.GetBondDir() in _DIRS}
        Chem.Kekulize(work, clearAromaticFlags=True)
        return work, displays
    except Exception:  # noqa: BLE001
        pass
    work = _kekulized(mol)
    work = Chem.Mol(work)
    work.UpdatePropertyCache(strict=False)
    Chem.FastFindRings(work)
    rdDepictor.Compute2DCoords(work)
    return work, {}


def _label_extent(atom: _Atom) -> tuple[float, float]:
    """Half width and half height of the label ChemDraw writes at an atom
    (0 for a plain skeleton carbon). Generous: the side of the H is unknown."""
    labelled = (atom.element != 6 or atom.charge or atom.isotope or atom.alias
                or not atom.bonded)
    if not labelled:
        return 1.0, 1.0
    text = atom.alias or Chem.GetPeriodicTable().GetElementSymbol(max(atom.element, 0)) \
        if atom.element > 0 else (atom.alias or "R")
    half = text_width(text) / 2 + 1.0
    if atom.hydrogens:
        half += text_width("H") + (text_width("2") if atom.hydrogens > 1 else 0)
    if atom.charge:
        half += 5.0
    if atom.isotope:
        half += text_width(str(atom.isotope)) * 0.7
    # ChemDraw may put the H under or over the symbol (an NH between two bonds).
    return half, 13.0 if atom.hydrogens else 6.5


def _fragment_box(drawing: _Drawing, atoms: list[int]) -> Box:
    xs0, ys0, xs1, ys1 = [], [], [], []
    for i in atoms:
        a = drawing.atoms[i]
        hw, hh = _label_extent(a)
        xs0.append(a.x - hw)
        xs1.append(a.x + hw)
        ys0.append(a.y - hh)
        ys1.append(a.y + hh)
    return min(xs0), min(ys0), max(xs1), max(ys1)


def _overlaps(a: Box, b: Box, gap: float = 0.0) -> bool:
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def _draw_molecule(molfile: str | None) -> _Drawing | None:
    """The drawing of a molfile in points, centred on (0, 0), or None when it
    cannot be read."""
    mol = read_molfile(molfile)
    if mol is None:
        return None
    with rdBase.BlockLogs():
        try:
            return _layout_molecule(mol)
        except Exception:  # noqa: BLE001 - an undrawable structure is written as its name
            return None


def _layout_molecule(mol: Chem.Mol) -> _Drawing:
    n = mol.GetNumAtoms()
    conf = mol.GetConformer() if mol.GetNumConformers() else None
    pos = ([tuple(conf.GetAtomPosition(i)) for i in range(n)] if conf is not None
           else [(0.0, 0.0, 0.0)] * n)
    three_d = any(abs(p[2]) > 1e-4 for p in pos)
    extent = max(max(p[0] for p in pos) - min(p[0] for p in pos),
                 max(p[1] for p in pos) - min(p[1] for p in pos))
    degenerate = conf is None or (n > 1 and extent < 1e-3)
    if mol.GetNumBonds() and not degenerate and not three_d:
        lengths = [math.dist(pos[b.GetBeginAtomIdx()][:2], pos[b.GetEndAtomIdx()][:2])
                   for b in mol.GetBonds()]
        degenerate = max(lengths) < 1e-4

    if three_d or degenerate:
        work, displays = _new_coordinates(mol, three_d)
        conf2 = work.GetConformer()
        pos = [tuple(conf2.GetAtomPosition(i)) for i in range(n)]
    else:
        work = _kekulized(mol)
        displays = {b.GetIdx(): d for b in mol.GetBonds() if (d := _drawn_display(b))}

    try:
        work.UpdatePropertyCache(strict=False)
    except Exception:  # noqa: BLE001
        pass

    # Scale: the median bond becomes BOND_LENGTH; without bonds the atoms
    # (ions) are set two bond lengths apart.
    lengths = [d for b in work.GetBonds()
               if (d := math.dist(pos[b.GetBeginAtomIdx()][:2], pos[b.GetEndAtomIdx()][:2])) > 1e-6]
    if lengths:
        scale = BOND_LENGTH / statistics.median(lengths)
    elif n > 1:
        nearest = [min(math.dist(pos[i][:2], pos[j][:2]) for j in range(n) if j != i)
                   for i in range(n)]
        positive = [d for d in nearest if d > 1e-6]
        scale = 2 * BOND_LENGTH / statistics.median(positive) if positive else 1.0
    else:
        scale = 1.0

    atoms = []
    for atom in work.GetAtoms():
        i = atom.GetIdx()
        alias = None
        for prop in ("molFileAlias", "dummyLabel"):
            if atom.HasProp(prop) and atom.GetProp(prop).strip():
                alias = atom.GetProp(prop).strip()
                break
        if atom.GetAtomicNum() != 0 and alias == "*":
            alias = None
        try:
            hydrogens = atom.GetNumImplicitHs()
        except Exception:  # noqa: BLE001
            hydrogens = 0
        atoms.append(_Atom(x=pos[i][0] * scale, y=-pos[i][1] * scale, element=atom.GetAtomicNum(),
                           charge=atom.GetFormalCharge(), isotope=atom.GetIsotope(),
                           radicals=atom.GetNumRadicalElectrons(), alias=alias,
                           hydrogens=hydrogens, bonded=atom.GetDegree() > 0))
    bonds = [_Bond(b.GetBeginAtomIdx(), b.GetEndAtomIdx(), _ORDERS.get(b.GetBondType(), "1"),
                   displays.get(b.GetIdx())) for b in work.GetBonds()]

    try:
        groups = [sorted(g) for g in Chem.GetMolFrags(work, sanitizeFrags=False)]
    except Exception:  # noqa: BLE001
        groups = [list(range(n))]
    groups.sort(key=lambda g: g[0])
    owner = {i: k for k, g in enumerate(groups) for i in g}
    fragments: list[tuple[list[int], list[int]]] = [(g, []) for g in groups]
    for j, bond in enumerate(bonds):
        fragments[owner[bond.begin]][1].append(j)
    drawing = _Drawing(atoms, bonds, fragments, [])
    drawing.boxes = [_fragment_box(drawing, g) for g, _ in fragments]
    _separate_fragments(drawing)
    _centre(drawing)
    return drawing


def _separate_fragments(drawing: _Drawing) -> None:
    """Move fragments (ions of a salt) to the right until no two of them
    overlap, keeping their drawn order from left to right."""
    order = sorted(range(len(drawing.fragments)), key=lambda k: (drawing.boxes[k][0], k))
    placed: list[int] = []
    for k in order:
        for _ in range(len(placed) + 1):
            hits = [p for p in placed if _overlaps(drawing.boxes[k], drawing.boxes[p],
                                                   _FRAGMENT_GAP)]
            if not hits:
                break
            shift = max(drawing.boxes[p][2] for p in hits) + _FRAGMENT_GAP - drawing.boxes[k][0]
            for i in drawing.fragments[k][0]:
                drawing.atoms[i].x += shift
            b = drawing.boxes[k]
            drawing.boxes[k] = (b[0] + shift, b[1], b[2] + shift, b[3])
        placed.append(k)


def _centre(drawing: _Drawing) -> None:
    x0, y0, x1, y1 = drawing.box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for a in drawing.atoms:
        a.x -= cx
        a.y -= cy
    drawing.boxes = [(b[0] - cx, b[1] - cy, b[2] - cx, b[3] - cy) for b in drawing.boxes]


# --------------------------------------------------------------------------
# Document writer
# --------------------------------------------------------------------------

class _Doc:
    def __init__(self) -> None:
        self.next_id = 2                # 1 is the page
        self.parts: list[str] = []
        self.boxes: list[Box] = []

    def new_id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value

    def text(self, x: float, baseline: float, text: str, *, bold: bool = False,
             formula: bool = False) -> int:
        tid = self.new_id()
        text = _clean(text)
        width = text_width(text, bold=bold)
        box = (x - width / 2, baseline - _ASCENT, x + width / 2, baseline + _DESCENT)
        self.boxes.append(box)
        face = 1 if bold else 0
        runs = "".join(
            f'<s font="{_FONT_ID}" size="{_f(_FONT_SIZE)}" face="{32 if sub else face}">'
            f"{escape(piece)}</s>" for piece, sub in formula_runs(text, formula))
        self.parts.append(f'<t id="{tid}" p="{_f(x)} {_f(baseline)}" BoundingBox="{_box_attr(box)}"'
                          f' Justification="Center" InterpretChemically="no">{runs}</t>')
        return tid

    def molecule(self, drawing: _Drawing, dx: float, dy: float) -> list[int]:
        ids = []
        for (atom_ix, bond_ix), box in zip(drawing.fragments, drawing.boxes):
            fid = self.new_id()
            ids.append(fid)
            node_ids = {i: self.new_id() for i in atom_ix}
            shifted = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
            self.boxes.append(shifted)
            out = [f'<fragment id="{fid}" BoundingBox="{_box_attr(shifted)}">']
            for i in atom_ix:
                a = drawing.atoms[i]
                attrs = f'id="{node_ids[i]}" p="{_f(a.x + dx)} {_f(a.y + dy)}"'
                label = None
                if a.element == 0:
                    attrs += ' NodeType="GenericNickname" GenericNickname=' \
                        + quoteattr(_clean(a.alias or "R"))
                    label = a.alias or "R"
                else:
                    if a.element != 6:
                        attrs += f' Element="{a.element}"'
                    if a.alias:
                        label = a.alias
                if a.charge:
                    attrs += f' Charge="{a.charge}"'
                if a.isotope:
                    attrs += f' Isotope="{a.isotope}"'
                if a.radicals in _RADICALS:
                    attrs += f' Radical="{_RADICALS[a.radicals]}"'
                if label:
                    out.append(f"<n {attrs}><t p=\"{_f(a.x + dx - 3.5)} {_f(a.y + dy + 3.5)}\">"
                               f'<s font="{_FONT_ID}" size="{_f(_FONT_SIZE)}" face="96">'
                               f"{escape(_clean(label))}</s></t></n>")
                else:
                    out.append(f"<n {attrs}/>")
            for j in bond_ix:
                b = drawing.bonds[j]
                attrs = f'id="{self.new_id()}" B="{node_ids[b.begin]}" E="{node_ids[b.end]}"'
                if b.order != "1":
                    attrs += f' Order="{b.order}"'
                if b.display:
                    attrs += f' Display="{b.display}"'
                out.append(f"<b {attrs}/>")
            out.append("</fragment>")
            self.parts.append("".join(out))
        return ids

    def arrow(self, x0: float, x1: float, y: float) -> int:
        aid = self.new_id()
        box = (x0, y - 3.0, x1, y + 3.0)
        self.boxes.append(box)
        self.parts.append(
            f'<arrow id="{aid}" BoundingBox="{_box_attr(box)}" FillType="None" '
            f'ArrowheadHead="Full" ArrowheadType="Solid" HeadSize="1000" '
            f'ArrowheadCenterSize="875" ArrowheadWidth="250" '
            f'Head3D="{_f(x1)} {_f(y)} 0" Tail3D="{_f(x0)} {_f(y)} 0"/>')
        return aid


# --------------------------------------------------------------------------
# Layout items
# --------------------------------------------------------------------------

@dataclass
class _Item:
    width: float = 0.0
    up: float = 0.0               # extent above the row line
    down: float = 0.0             # extent below it
    ids: list[int] = field(default_factory=list)

    def emit(self, doc: _Doc, x: float, line: float) -> None:
        raise NotImplementedError


class _Compound(_Item):
    def __init__(self, participant: Participant):
        super().__init__()
        self.participant = participant
        self.drawing = _draw_molecule(participant.molfile) if participant.molfile else None
        self.label = participant.label
        self.label_width = text_width(self.label, bold=True) if self.label else 0.0
        if self.drawing is not None:
            x0, y0, x1, y1 = self.drawing.box
            self.body_width, self.body_up, self.body_down = x1 - x0, -y0, y1
        else:
            self.name = participant.name or participant.label or "?"
            self.body_width = text_width(self.name)
            self.body_up, self.body_down = _ASCENT - _CENTRE_DROP, _CENTRE_DROP + _DESCENT
        self.width = max(self.body_width, self.label_width)
        self.up = self.body_up
        self.down = self.body_down + ((_LABEL_GAP + _ASCENT + _DESCENT) if self.label else 0.0)

    def emit(self, doc: _Doc, x: float, line: float) -> None:
        cx = x + self.width / 2
        if self.drawing is not None:
            self.ids = doc.molecule(self.drawing, cx, line)
        else:
            self.ids = [doc.text(cx, line + _CENTRE_DROP, self.name)]
        if self.label:
            doc.text(cx, line + self.body_down + _LABEL_GAP + _ASCENT, self.label, bold=True)


class _Plus(_Item):
    def __init__(self) -> None:
        super().__init__(width=text_width("+"), up=_ASCENT - _CENTRE_DROP,
                         down=_CENTRE_DROP + _DESCENT)

    def emit(self, doc: _Doc, x: float, line: float) -> None:
        self.ids = [doc.text(x + self.width / 2, line + _CENTRE_DROP, "+")]


def _wrap(text: Text, limit: float = MAX_LINE_WIDTH) -> list[Text]:
    """Split a line wider than ``limit`` after ", " (the comma stays)."""
    if text_width(text.text) <= limit or ", " not in text.text:
        return [text]
    pieces = text.text.split(", ")
    lines: list[str] = []
    current = pieces[0]
    for piece in pieces[1:]:
        candidate = f"{current}, {piece}"
        if text_width(candidate) <= limit:
            current = candidate
        else:
            lines.append(current + ",")
            current = piece
    lines.append(current)
    return [Text(line, text.formula) for line in lines]


class _Arrow(_Item):
    """A reaction arrow with its texts and, for a continued step, the
    compounds drawn over it (``over``: the step's other reactants)."""

    def __init__(self, step: Step, over: list[_Compound] | None = None):
        super().__init__()
        self.over = list(over or [])
        self.over_items = _joined(self.over, lead=False)
        self.above = [line for t in step.above for line in _wrap(t)]
        self.below = [line for t in step.below for line in _wrap(t)]
        self.over_width = sum(item.width for item in self.over_items) \
            + _GAP_PLUS * max(len(self.over_items) - 1, 0)
        widest = max([text_width(t.text) for t in self.above + self.below] + [self.over_width])
        self.width = max(_MIN_ARROW, widest + _ARROW_MARGIN)
        # The structures over the arrow sit on their own line: their lowest
        # point (a label) _OVER_GAP above the arrow.
        self.over_up = max((item.up for item in self.over_items), default=0.0)
        self.over_down = max((item.down for item in self.over_items), default=0.0)
        self.over_rise = (_OVER_GAP + self.over_down) if self.over else 0.0
        # Reagent lines start above the arrow, or above the structures over it.
        self.texts_from = (self.over_rise + self.over_up) if self.over else 0.0
        if self.above:
            self.up = (self.texts_from + _ABOVE_GAP + _ASCENT
                       + _LINE_PITCH * (len(self.above) - 1))
        else:
            self.up = self.texts_from if self.over else 3.0
        self.down = (_BELOW_FIRST + _DESCENT + _LINE_PITCH * (len(self.below) - 1)) \
            if self.below else 3.0
        self.above_ids: list[int] = []
        self.below_ids: list[int] = []

    @property
    def over_ids(self) -> list[int]:
        return [i for item in self.over for i in item.ids]

    def emit(self, doc: _Doc, x: float, line: float) -> None:
        self.ids = [doc.arrow(x, x + self.width, line)]
        cx = x + self.width / 2
        if self.over:
            over_line = line - self.over_rise
            ox = cx - self.over_width / 2
            for k, item in enumerate(self.over_items):
                if k:
                    ox += _GAP_PLUS
                item.emit(doc, ox, over_line)
                ox += item.width
        lowest = line - self.texts_from - _ABOVE_GAP
        n = len(self.above)
        self.above_ids = [doc.text(cx, lowest - _LINE_PITCH * (n - 1 - k), t.text,
                                   formula=t.formula) for k, t in enumerate(self.above)]
        self.below_ids = [doc.text(cx, line + _BELOW_FIRST + _LINE_PITCH * k, t.text,
                                   formula=t.formula) for k, t in enumerate(self.below)]


def _gap(left: _Item, right: _Item) -> float:
    if isinstance(left, _Arrow) or isinstance(right, _Arrow):
        return _GAP_ARROW
    return _GAP_PLUS


class _Row:
    def __init__(self) -> None:
        self.items: list[_Item] = []
        self.width = 0.0

    def extra_width(self, items: list[_Item]) -> float:
        width, previous = 0.0, (self.items[-1] if self.items else None)
        for item in items:
            width += (_gap(previous, item) if previous is not None else 0.0) + item.width
            previous = item
        return width

    def add(self, items: list[_Item]) -> None:
        self.width += self.extra_width(items)
        self.items.extend(items)


@dataclass
class _StepParts:
    reactants: list[_Item]
    arrow: _Arrow
    product: _Compound


def _joined(compounds: list[_Item], lead: bool) -> list[_Item]:
    """Compounds separated by "+" (and preceded by one when ``lead``)."""
    out: list[_Item] = []
    for k, item in enumerate(compounds):
        if k or lead:
            out.append(_Plus())
        out.append(item)
    return out


def _layout(route: Route) -> tuple[list[_Row], list[_StepParts]]:
    rows: list[_Row] = []
    parts: list[_StepParts] = []
    last: _Compound | None = None          # the product drawn last in rows[-1]
    for step in route.steps:
        product = _Compound(step.product)
        shared = None
        if last is not None:
            shared = next((p for p in step.reactants if p.key == last.participant.key), None)
        reactants: list[_Item]
        if shared is not None and last is not None:
            others = [_Compound(p) for p in _without(step.reactants, shared)]
            arrow = _Arrow(step, over=others)
            row = rows[-1]
            if row.width + row.extra_width([arrow, product]) <= MAX_ROW_WIDTH:
                row.add([arrow, product])
                reactants = [last] + others
            else:
                # The step starts the next row, from the last product drawn again.
                arrow = _Arrow(step)
                again = _Compound(last.participant)
                row = _Row()
                rows.append(row)
                row.add(_joined([again] + others, lead=False) + [arrow, product])
                reactants = [again] + others
        else:
            arrow = _Arrow(step)
            compounds: list[_Item] = [_Compound(p) for p in step.reactants]
            row = _Row()
            rows.append(row)
            row.add(_joined(compounds, lead=False) + [arrow, product])
            reactants = compounds
        parts.append(_StepParts(reactants, arrow, product))
        last = product
    if not route.steps:
        row = _Row()
        row.add([_Compound(route.target)])
        rows.append(row)
    return rows, parts


def _without(participants: tuple[Participant, ...], shared: Participant) -> list[Participant]:
    out, skipped = [], False
    for p in participants:
        if not skipped and p.key == shared.key:
            skipped = True
            continue
        out.append(p)
    return out


def render_route(route: Route) -> str:
    """The CDXML document (text) of ``route``. Pure and deterministic."""
    rows, parts = _layout(route)
    doc = _Doc()
    for row, top in zip(rows, _row_tops(rows)):
        up = max(item.up for item in row.items)
        line = top + up
        x = SLIDE_MARGIN
        previous: _Item | None = None
        for item in row.items:
            if previous is not None:
                x += _gap(previous, item)
            item.emit(doc, x, line)
            x += item.width
            previous = item

    scheme_id = doc.new_id() if parts else 0
    steps = []
    for part in parts:
        attrs = {
            "ReactionStepReactants": [i for item in part.reactants for i in item.ids],
            "ReactionStepProducts": part.product.ids,
            "ReactionStepArrows": part.arrow.ids,
            "ReactionStepObjectsAboveArrow": part.arrow.over_ids + part.arrow.above_ids,
            "ReactionStepObjectsBelowArrow": part.arrow.below_ids,
        }
        sid = doc.new_id()
        text = "".join(f' {k}="{" ".join(str(i) for i in v)}"' for k, v in attrs.items() if v)
        steps.append(f'<step id="{sid}"{text}/>')
    scheme = f'<scheme id="{scheme_id}">{"".join(steps)}</scheme>' if steps else ""

    boxes = doc.boxes or [(0.0, 0.0, 1.0, 1.0)]
    content = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
    slides = max(1, math.ceil((content[3] + SLIDE_MARGIN) / SLIDE_HEIGHT - 1e-9))
    page_width = max(SLIDE_WIDTH, content[2] - content[0] + 2 * SLIDE_MARGIN)
    page_height = slides * SLIDE_HEIGHT
    style = " ".join(f'{k}="{v}"' for k, v in ACS_1996.items())
    drawn = (
        f'<CDXML BoundingBox="{_box_attr(content)}" {style}>\n'
        f'<fonttable><font id="{_FONT_ID}" charset="iso-8859-1" name="Arial"/></fonttable>\n'
        f'<page id="1" BoundingBox="0 0 {page_width:g} {page_height:g}" '
        f'DrawingSpace="poster" Width="{page_width:g}" Height="{page_height:g}">\n'
        + "\n".join(doc.parts) + ("\n" + scheme if scheme else "")
        + "\n</page>\n</CDXML>"
    )
    dx = (page_width - (content[2] - content[0])) / 2 - content[0]
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        '<!DOCTYPE CDXML SYSTEM "http://www.cambridgesoft.com/xml/cdxml.dtd" >\n'
        + _shift(drawn, dx) + "\n"
    )


def _row_tops(rows: list) -> list[float]:
    """Top of each row: rows packed onto slides (a row never crosses a
    slide's bottom margin unless it alone is taller than a slide), each
    slide's rows centred vertically on it."""
    heights = [max(i.up for i in r.items) + max(i.down for i in r.items) for r in rows]
    room = SLIDE_HEIGHT - 2 * SLIDE_MARGIN
    slides: list[list[int]] = []
    used = 0.0
    for index, h in enumerate(heights):
        need = h if not slides or not slides[-1] else used + _ROW_GAP + h
        if slides and slides[-1] and need > room:
            slides.append([])
            need = h
        if not slides:
            slides.append([])
        slides[-1].append(index)
        used = need
    tops = [0.0] * len(rows)
    for number, members in enumerate(slides):
        total = sum(heights[i] for i in members) + _ROW_GAP * (len(members) - 1)
        top = number * SLIDE_HEIGHT + SLIDE_MARGIN + max(0.0, (room - total) / 2)
        for i in members:
            tops[i] = top
            top += heights[i] + _ROW_GAP
    return tops


#: Attributes holding x y pairs (a BoundingBox is two pairs; *3D ones are x y z).
_POINT_ATTRS = ("p", "BoundingBox", "Head3D", "Tail3D", "Center3D")


def _shift(drawn: str, dx: float) -> str:
    """Moves every drawn object ``dx`` points to the right (centres the
    scheme on the slide); sizes are never changed."""
    root = ET.fromstring(drawn)
    page = root.find("page")
    for el in root.iter():
        if el is page:
            continue
        for attr in _POINT_ATTRS:
            value = el.get(attr)
            if value is None:
                continue
            numbers = [float(v) for v in value.split()]
            if attr.endswith("3D"):
                numbers[0] += dx
            else:
                numbers = [v + dx if i % 2 == 0 else v for i, v in enumerate(numbers)]
            el.set(attr, " ".join(_f(v) for v in numbers))
    return ET.tostring(root, encoding="unicode")

