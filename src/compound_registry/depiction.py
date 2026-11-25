"""The drawing of a molecule read from a picture ("Capture reaction").

:func:`drawing_molfile` writes the V2000 molfile that the capture page shows
on a card, opens in the structure editor and sends back for ChemDraw. Its
rules:

* **The shape of the picture.** MolScribe says where it saw every atom. Those
  places are used, so the card can be compared with the picture at a glance:
  what is left, right, up and down stays there. Three things are repaired on
  the way:

  - *The squeeze.* MolScribe looks at the picture without its white border
    but scales its coordinates with the shape of the whole picture, so a
    drawing with unequal margins comes out squeezed in x or y (measured: the
    user's first capture was 37 % too tall). The worker reports the rectangle
    MolScribe really looked at (``seen``), and :func:`picture_places` puts
    every atom back at its pixel.
  - *The grid.* MolScribe's coordinates lie on a grid of 64 steps, so bond
    lengths and ring angles wobble. :func:`tidy` evens them out the way
    "Clean Up Structure" does (equal bonds, regular rings, angles on the
    nearest 30 degrees) and turns the drawing by a few degrees at most
    (:data:`MAX_STRAIGHTEN`) so that its bonds lie on the usual directions. An atom never leaves its place by
    more than :data:`MAX_TIDY_MOVE` bond lengths; otherwise the untidied
    places are used.
  - *Groups the SMILES has expanded.* An abbreviation ("Boc") is one
    placeholder atom in MolScribe's drawing and several atoms in its SMILES.
    Its first atom takes the place of the label, RDKit lays out the rest.

  Only when the drawing cannot be matched to the structure, or its places are
  unusable (atoms on top of each other), the molecule gets a computed layout:
  RDKit's own or CoordGen's, whichever has fewer clashes (:func:`fresh_layout`).

* **Wedges that agree with the SMILES.** The wedges are never copied blindly.
  The picture's own wedges are kept when, on the final coordinates, they give
  exactly the stereo of the structure. Otherwise RDKit works the wedges out
  from the structure, and each is moved onto the bond the picture has wedged
  when that can be done truthfully. Every molfile is read back before it is
  returned (:func:`reads_back_as`): it must give the same canonical SMILES,
  stereocentres and double-bond geometry included, or it is not used.

* **One enantiomer.** A SMILES with ``@`` describes one enantiomer, so a
  molfile with a wedge gets the chiral flag. Without it OpenChemLib (the card
  and the editor) reads every stereocentre as a racemic "&1" group.

No picture data is logged here, and nothing raises: any trouble ends in the
computed layout, and ``None`` only when even that cannot be written.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable, Sequence

__all__ = ["BOND", "MAX_TIDY_MOVE", "MAX_STRAIGHTEN", "picture_places", "tidy", "fresh_layout", "clash_score",
           "reads_back_as", "drawing_molfile"]

#: Bond length of the molfiles written here (RDKit's own).
BOND = 1.5
#: :func:`tidy` may move an atom this far (in bond lengths) from where the
#: picture has it; a result beyond that is thrown away.
MAX_TIDY_MOVE = 0.6
#: :func:`tidy` turns a drawing by at most this many degrees to put its bonds
#: on the usual directions (multiples of 30 degrees).
MAX_STRAIGHTEN = 6.0
#: Two atoms nearer than this (in bond lengths) count as lying on each other.
CLASH = 0.35
#: :func:`_straightened` turns a fragment only when its bonds agree this well
#: on one grid of 30 degrees (1 = all bonds exactly on it, 0 = no agreement).
_GRID_AGREEMENT = 0.6
#: The room between two fragments that are set next to each other (bond lengths).
_FRAGMENT_GAP = 1.2
#: Atom matchings of the drawing onto the structure that are tried.
_MAX_MATCHES = 24
_TIDY_ROUNDS = 1000
#: Atoms that are not bonded and nearer than this (bond lengths) after
#: :func:`tidy` make it draw the bond to an end atom among them shorter ...
_KEEP_APART = 0.3
#: ... by this factor (up to twice).
_SHORTER = 0.68

Places = dict[int, tuple[float, float]]


# --------------------------------------------------------------------------- the picture

def picture_places(drawn: Any, box: Sequence[float] | None = None,
                   seen: Sequence[float] | None = None) -> Places | None:
    """Atom of the drawing ``drawn`` (MolScribe's molfile, unsanitised) -> its
    place in the picture, x to the right and y UP like in a molfile.

    With ``box`` (the pixel box of the molecule in the picture) and ``seen``
    (the rectangle inside that box that MolScribe looked at, see the worker)
    the squeeze is undone: MolScribe wrote ``x = fx * 10 * width / height`` and
    ``y = 1 - fy * 10`` with ``fx``, ``fy`` the fractions of ``seen``. Without
    them, or when the molfile does not look like MolScribe's, the coordinates
    are taken as they are. None when the drawing has no coordinates."""
    if drawn is None or drawn.GetNumConformers() == 0:
        return None
    conformer = drawn.GetConformer()
    raw = {}
    for index in range(drawn.GetNumAtoms()):
        point = conformer.GetAtomPosition(index)
        if not (math.isfinite(point.x) and math.isfinite(point.y)):
            return None
        raw[index] = (float(point.x), float(point.y))
    frame = _frame(box, seen)
    if frame is None:
        return raw
    width, height, (x0, y0, x1, y1) = frame
    places = {}
    for index, (x, y) in raw.items():
        fx = x * height / (10.0 * width)
        fy = (1.0 - y) / 10.0
        if not (-0.05 <= fx <= 1.05 and -0.05 <= fy <= 1.05):
            return raw                    # not MolScribe's scale: leave it alone
        places[index] = (x0 + fx * (x1 - x0), -(y0 + fy * (y1 - y0)))
    return places


def _frame(box: Any, seen: Any) -> tuple[float, float, tuple[float, float, float, float]] | None:
    try:
        bx0, by0, bx1, by1 = (float(v) for v in box)
        sx0, sy0, sx1, sy1 = (float(v) for v in seen)
    except (TypeError, ValueError):
        return None
    width, height = bx1 - bx0, by1 - by0
    values = (width, height, sx0, sy0, sx1, sy1)
    if not all(math.isfinite(v) for v in values) or width < 1 or height < 1:
        return None
    if sx1 - sx0 < 1 or sy1 - sy0 < 1 or sx1 - sx0 > width + 12 or sy1 - sy0 > height + 12:
        return None
    return width, height, (sx0, sy0, sx1, sy1)


# --------------------------------------------------------------------------- geometry

def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _bond_pairs(mol: Any) -> list[tuple[int, int]]:
    return [(b.GetBeginAtomIdx(), b.GetEndAtomIdx()) for b in mol.GetBonds()]


def _scaled(mol: Any, places: Places) -> Places | None:
    """``places`` scaled so that the median bond is :data:`BOND`, centred on
    (0, 0). None when no length can be told (all atoms on one spot)."""
    lengths = [d for a, b in _bond_pairs(mol)
               if a in places and b in places and (d := _distance(places[a], places[b])) > 1e-6]
    if lengths:
        scale = BOND / statistics.median(lengths)
    else:
        points = list(places.values())
        nearest = [min((_distance(p, q) for j, q in enumerate(points) if j != i), default=0.0)
                   for i, p in enumerate(points)]
        positive = [d for d in nearest if d > 1e-6]
        if len(points) > 1 and not positive:
            return None
        scale = 2 * BOND / statistics.median(positive) if positive else 1.0
    if not math.isfinite(scale) or scale <= 0:
        return None
    xs = [p[0] * scale for p in places.values()]
    ys = [p[1] * scale for p in places.values()]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return {i: (x * scale - cx, y * scale - cy) for i, (x, y) in places.items()}


def clash_score(mol: Any, places: Places) -> float:
    """How bad a layout is: 0 for none of these faults, 1 for every pair of
    atoms on top of each other (nearer than :data:`CLASH` bond lengths), 1 for
    every pair of bonds that cross, and a little for every bond that is much
    too short or too long."""
    atoms = sorted(places)
    score = 0.0
    limit = CLASH * BOND
    for n, i in enumerate(atoms):
        for j in atoms[n + 1:]:
            if abs(places[i][0] - places[j][0]) < limit and _distance(places[i], places[j]) < limit:
                score += 1.0
    bonds = [(a, b) for a, b in _bond_pairs(mol) if a in places and b in places]
    for a, b in bonds:
        length = _distance(places[a], places[b])
        if length < 0.5 * BOND or length > 1.8 * BOND:
            score += 0.25
    return score + _crossings(mol, places)


def _crossings(mol: Any, places: Places) -> int:
    """Pairs of bonds that cross each other."""
    bonds = [(a, b) for a, b in _bond_pairs(mol) if a in places and b in places]
    count = 0
    for n, (a, b) in enumerate(bonds):
        for c, d in bonds[n + 1:]:
            if len({a, b, c, d}) == 4 and _cross(places[a], places[b], places[c], places[d]):
                count += 1
    return count


def _cross(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float],
           s: tuple[float, float]) -> bool:
    def side(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2, d3, d4 = side(r, s, p), side(r, s, q), side(p, q, r), side(p, q, s)
    return (d1 * d2 < -1e-9) and (d3 * d4 < -1e-9)


# --------------------------------------------------------------------------- tidy

def _snap(angle: float, step: float = 30.0) -> float:
    return round(angle / step) * step


def _gap_targets(mol: Any, centre: int, places: Places, rings: list[frozenset[int]]) \
        -> list[tuple[int, int, float]]:
    """``(neighbour, next neighbour, angle in degrees)`` for the angles around
    ``centre`` that have an ideal value; [] when nothing sure can be said."""
    neighbours = [n.GetIdx() for n in mol.GetAtomWithIdx(centre).GetNeighbors()]
    degree = len(neighbours)
    if degree < 2 or degree > 4:
        return []
    cx, cy = places[centre]
    if any(_distance(places[n], (cx, cy)) < 1e-6 for n in neighbours):
        return []
    angle = {n: math.degrees(math.atan2(places[n][1] - cy, places[n][0] - cx)) for n in neighbours}
    order = sorted(neighbours, key=lambda n: angle[n])
    pairs = [(order[i], order[(i + 1) % degree]) for i in range(degree)]
    gaps = [(angle[b] - angle[a]) % 360.0 for a, b in pairs]
    if degree == 2:
        # Two neighbours have one angle; take the smaller side.
        if gaps[0] > 180.0:
            pairs, gaps = [pairs[1]], [gaps[1]]
        else:
            pairs, gaps = [pairs[0]], [gaps[0]]
    elif min(gaps) < 20.0:
        return []                          # two bonds on top of each other: no order to trust

    def ring_angle(a: int, b: int) -> float | None:
        sizes = [len(ring) for ring in rings if centre in ring and a in ring and b in ring]
        size = min(sizes, default=0)
        return 180.0 - 360.0 / size if 3 <= size <= 8 else None

    fixed = [ring_angle(a, b) for a, b in pairs]
    if degree == 2:
        if fixed[0] is not None:
            return [(pairs[0][0], pairs[0][1], fixed[0])]
        atom = mol.GetAtomWithIdx(centre)
        orders = sorted(b.GetBondTypeAsDouble() for b in atom.GetBonds())
        linear = orders[-1] >= 3.0 or orders == [2.0, 2.0]
        return [(pairs[0][0], pairs[0][1], 180.0 if linear else _chain_angle(gaps[0]))]

    total_fixed = sum(v for v in fixed if v is not None)
    free = [i for i, v in enumerate(fixed) if v is None]
    rest = 360.0 - total_fixed
    if not free:
        if abs(rest) > 20.0:
            return []                      # a bridged or bent ring system: left as drawn
        return [(a, b, v) for (a, b), v in zip(pairs, fixed)]      # type: ignore[misc]
    if rest < 50.0 * len(free):
        return []
    values = _free_angles([gaps[i] for i in free], rest, whole=len(free) == degree)
    targets = list(fixed)
    for i, value in zip(free, values):
        targets[i] = value
    return [(a, b, v) for (a, b), v in zip(pairs, targets) if v is not None and v < 181.0]


def _chain_angle(gap: float) -> float:
    """The angle at an atom with two neighbours: the 120 degrees of a chain,
    unless the picture clearly shows something else."""
    if gap >= 165.0:
        return 180.0
    if gap >= 100.0:
        return 120.0
    return max(_snap(gap), 60.0)


#: How four bonds at one atom are drawn (the angles between neighbours, in
#: turn): a cross, and the fans of a tert-butyl or a sulfonyl group.
_FOUR = ((90.0, 90.0, 90.0, 90.0), (60.0, 60.0, 120.0, 120.0), (60.0, 120.0, 60.0, 120.0),
         (90.0, 90.0, 60.0, 120.0), (90.0, 60.0, 90.0, 120.0))
#: Three bonds: the star, and the T.
_THREE = ((120.0, 120.0, 120.0), (180.0, 90.0, 90.0))


def _free_angles(gaps: list[float], rest: float, whole: bool) -> list[float]:
    """Ideal values for the angles ``gaps`` (in turn) that must add up to
    ``rest``. ``whole``: they are all the angles around the atom (it is in no
    small ring), so the usual pictures of three and four bonds apply. The
    picture's angles are noisy (a grid of 64 steps), so the nearest usual
    picture wins; a drawing that fits none keeps its own proportions."""
    count = len(gaps)
    if whole and count in (3, 4):
        best, best_error = None, 0.0
        for pattern in (_THREE if count == 3 else _FOUR):
            for turn in range(count):
                turned = pattern[turn:] + pattern[:turn]
                for candidate in (turned, tuple(reversed(turned))):
                    error = math.sqrt(sum((g - c) ** 2 for g, c in zip(gaps, candidate)) / count)
                    if best is None or error < best_error - 1e-9:
                        best, best_error = candidate, error
        if best is not None and best_error <= 22.0:
            return list(best)
    elif count == 1:
        return [rest]
    elif count == 2 and abs(gaps[0] - gaps[1]) < 40.0:
        return [rest / 2, rest / 2]        # one substituent on a ring atom: it halves the outside
    elif count == 3 and gaps[1] < min(gaps[0], gaps[2]):
        middle = 60.0 if rest - 60.0 >= 120.0 else rest / 3
        return [(rest - middle) / 2, middle, (rest - middle) / 2]   # two substituents on a ring atom
    snapped = [max(_snap(g), 60.0) for g in gaps]
    if abs(sum(snapped) - rest) < 1.0:
        return snapped
    return [g * rest / sum(gaps) for g in gaps]


def tidy(mol: Any, places: Places) -> Places:
    """``places`` (every atom of ``mol``, median bond :data:`BOND`) evened
    out: bonds of one length, regular rings, angles on the nearest 30
    degrees, the whole turned by a few degrees at most onto the usual bond
    directions. Where two groups would then land on each other (a tert-butyl
    next to a ring: they nearly touch in the picture too) the bond to the end
    atom is drawn shorter, as a chemist does. The drawing keeps its shape:
    the result is returned only when no atom moved further than
    :data:`MAX_TIDY_MOVE` bond lengths, no bonds cross that did not before
    and no atom landed on another; otherwise ``places`` comes back
    unchanged."""
    if len(places) != mol.GetNumAtoms() or mol.GetNumBonds() == 0:
        return places
    try:
        rings = [frozenset(ring) for ring in mol.GetRingInfo().AtomRings()]
    except Exception:  # noqa: BLE001
        rings = []
    bonds = _bond_pairs(mol)
    length = {frozenset(pair): BOND for pair in bonds}
    angles = [(centre, a, b, degrees) for centre in range(mol.GetNumAtoms())
              for a, b, degrees in _gap_targets(mol, centre, places, rings)]
    near = {frozenset((a, b)) for a, b in bonds} | {frozenset((a, b)) for _c, a, b, _d in angles}
    degree = {atom.GetIdx(): atom.GetDegree() for atom in mol.GetAtoms()}
    atoms = sorted(places)

    def touching(pos: Places) -> list[tuple[int, int]]:
        limit = _KEEP_APART * BOND
        return [(i, j) for n, i in enumerate(atoms) for j in atoms[n + 1:]
                if abs(pos[i][0] - pos[j][0]) < limit and _distance(pos[i], pos[j]) < limit
                and frozenset((i, j)) not in near]

    result = places
    for attempt in range(3):
        springs = [(a, b, length[frozenset((a, b))], 1.0) for a, b in bonds]
        for centre, a, b, degrees in angles:
            # An angle is held by the distance between its two outer atoms.
            la, lb = length[frozenset((centre, a))], length[frozenset((centre, b))]
            across = la * la + lb * lb - 2.0 * la * lb * math.cos(math.radians(degrees))
            springs.append((a, b, math.sqrt(max(across, 0.0)), 0.6))
        result = _relaxed(places, springs)
        pairs = touching(result)
        ends = {i for pair in pairs for i in pair if degree[i] == 1}
        if not ends or attempt == 2:
            break
        for a, b in bonds:
            if a in ends or b in ends:
                length[frozenset((a, b))] *= _SHORTER
    result = _straightened(mol, _rigid_fit(result, places))
    if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in result.values()):
        return places
    fitted = _rigid_fit(result, places, turn=False)
    if max(_distance(fitted[i], places[i]) for i in places) > MAX_TIDY_MOVE * BOND + _turn_room(places):
        return places
    # Two groups that nearly touch in the picture may nearly touch here; what
    # must not happen is a new crossing of bonds or an atom landing on another.
    if _crossings(mol, result) > _crossings(mol, places):
        return places
    if _atoms_on_each_other(result) and not _atoms_on_each_other(places):
        return places
    return result


def _relaxed(places: Places, springs: list[tuple[int, int, float, float]]) -> Places:
    """``places`` moved until every spring ``(atom, atom, wanted distance,
    weight)`` has its length, as far as they can all be had."""
    pos = {i: [x, y] for i, (x, y) in places.items()}
    for round_number in range(_TIDY_ROUNDS):
        # A weak pull back to the picture keeps the drawing from drifting while
        # it is still rough; it fades out, or it would bend the parts that
        # turn easily (a group on a single bond) towards the grid again.
        pull = 0.02 * max(0.0, 1.0 - round_number / (0.3 * _TIDY_ROUNDS))
        worst = 0.0
        for a, b, wanted, weight in springs:
            dx, dy = pos[b][0] - pos[a][0], pos[b][1] - pos[a][1]
            distance = math.hypot(dx, dy)
            if distance < 1e-9:
                continue
            worst = max(worst, abs(distance - wanted))
            push = 0.5 * weight * (distance - wanted) / distance
            pos[a][0] += push * dx
            pos[a][1] += push * dy
            pos[b][0] -= push * dx
            pos[b][1] -= push * dy
        if pull > 0:
            for index, (x, y) in places.items():
                pos[index][0] += pull * (x - pos[index][0])
                pos[index][1] += pull * (y - pos[index][1])
        elif worst < 1e-4 * BOND:
            break
    return {i: (p[0], p[1]) for i, p in pos.items()}


def _turn_room(places: Places) -> float:
    """How far the outermost atom moves when the drawing is turned by the
    angle :func:`_straightened` may turn it."""
    xs = [p[0] for p in places.values()]
    ys = [p[1] for p in places.values()]
    radius = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / 2
    return 2 * radius * math.sin(math.radians(MAX_STRAIGHTEN / 2))


def _rigid_fit(moved: Places, reference: Places, turn: bool = True) -> Places:
    """``moved`` shifted (and, with ``turn``, rotated) onto ``reference`` in
    the least-squares sense: undoes any drift of the whole drawing."""
    keys = list(reference)
    n = len(keys)
    mx = sum(moved[k][0] for k in keys) / n
    my = sum(moved[k][1] for k in keys) / n
    rx = sum(reference[k][0] for k in keys) / n
    ry = sum(reference[k][1] for k in keys) / n
    cos_t, sin_t = 1.0, 0.0
    if turn:
        dot = cross = 0.0
        for k in keys:
            ax, ay = moved[k][0] - mx, moved[k][1] - my
            bx, by = reference[k][0] - rx, reference[k][1] - ry
            dot += ax * bx + ay * by
            cross += ax * by - ay * bx
        norm = math.hypot(dot, cross)
        if norm > 1e-9:
            cos_t, sin_t = dot / norm, cross / norm
    out = {}
    for k, (x, y) in moved.items():
        ax, ay = x - mx, y - my
        out[k] = (rx + ax * cos_t - ay * sin_t, ry + ax * sin_t + ay * cos_t)
    return out


def _straightened(mol: Any, places: Places) -> Places:
    """Each fragment of ``places`` turned about its middle by the angle that
    brings its bond directions closest to multiples of 30 degrees, when that
    is a small correction (at most :data:`MAX_STRAIGHTEN` degrees: the noise
    of the grid, a scan that is a little askew). A fragment whose bonds do
    not share such directions (a five-membered ring with a chain), or that
    is drawn at a real angle, is left as the picture has it."""
    from rdkit import Chem

    try:
        fragments = [set(f) for f in Chem.GetMolFrags(mol)]
    except Exception:  # noqa: BLE001
        fragments = [set(places)]
    out = dict(places)
    for fragment in fragments:
        sin_sum = cos_sum = 0.0
        count = 0
        for a, b in _bond_pairs(mol):
            if a not in fragment:
                continue
            dx, dy = places[b][0] - places[a][0], places[b][1] - places[a][1]
            if math.hypot(dx, dy) < 1e-6:
                continue
            angle = math.atan2(dy, dx) * 12.0       # 30 degrees become a full turn
            sin_sum += math.sin(angle)
            cos_sum += math.cos(angle)
            count += 1
        if count == 0 or math.hypot(sin_sum, cos_sum) < _GRID_AGREEMENT * count:
            continue
        off = math.atan2(sin_sum, cos_sum) / 12.0
        if abs(off) > math.radians(MAX_STRAIGHTEN):
            continue                      # really drawn at an angle: it stays as the picture has it
        cos_t, sin_t = math.cos(-off), math.sin(-off)
        cx = sum(places[i][0] for i in fragment) / len(fragment)
        cy = sum(places[i][1] for i in fragment) / len(fragment)
        for i in fragment:
            x, y = places[i][0] - cx, places[i][1] - cy
            out[i] = (cx + x * cos_t - y * sin_t, cy + x * sin_t + y * cos_t)
    return out


# --------------------------------------------------------------------------- layouts

def _places_of(mol: Any) -> Places:
    conformer = mol.GetConformer()
    return {i: (conformer.GetAtomPosition(i).x, conformer.GetAtomPosition(i).y)
            for i in range(mol.GetNumAtoms())}


def fresh_layout(mol: Any) -> Places | None:
    """A computed layout of ``mol``: RDKit's own (with its ring templates) or
    CoordGen's, whichever has the lower :func:`clash_score` (RDKit's on a
    tie), scaled like everything else. Fragments (the ions of a salt) are set
    side by side in one row."""
    from rdkit import Chem
    from rdkit.Chem import rdDepictor

    candidates: list[Places] = []
    work = Chem.Mol(mol)
    try:
        work.RemoveAllConformers()
        try:
            rdDepictor.Compute2DCoords(work, useRingTemplates=True)
        except TypeError:                 # an RDKit without ring templates
            rdDepictor.Compute2DCoords(work)
        candidates.append(_places_of(work))
    except Exception:  # noqa: BLE001
        pass
    if not candidates or clash_score(mol, candidates[0]) > 0:
        try:
            from rdkit.Chem import rdCoordGen

            other = Chem.Mol(mol)
            other.RemoveAllConformers()
            rdCoordGen.AddCoords(other)
            candidates.append(_places_of(other))
        except Exception:  # noqa: BLE001 - CoordGen is optional
            pass
    best = None
    for places in candidates:
        scaled = _scaled(mol, places)
        if scaled is None:
            continue
        scaled = _in_a_row(mol, scaled)
        if best is None or clash_score(mol, scaled) < clash_score(mol, best):
            best = scaled
    return best


def _in_a_row(mol: Any, places: Places) -> Places:
    """The fragments (the ions of a salt) set in one row from left to right in
    the order of the SMILES, each centred on the row, one bond length apart."""
    from rdkit import Chem

    try:
        fragments = sorted((sorted(f) for f in Chem.GetMolFrags(mol)), key=lambda f: f[0])
    except Exception:  # noqa: BLE001
        return places
    if len(fragments) < 2:
        return places
    out = {}
    x = 0.0
    for fragment in fragments:
        xs = [places[i][0] for i in fragment]
        ys = [places[i][1] for i in fragment]
        middle = (min(ys) + max(ys)) / 2
        for i in fragment:
            out[i] = (places[i][0] - min(xs) + x, places[i][1] - middle)
        x += max(xs) - min(xs) + _FRAGMENT_GAP * BOND
    return _scaled(mol, out) or out


def _side_by_side(mol: Any, places: Places, only_touching: bool = False) -> Places:
    """Fragments whose boxes overlap are moved apart to the right, in their
    order from left to right (a salt: ion next to ion). ``only_touching``
    (a drawing from the picture): nothing is moved unless an atom of one
    fragment lies on, in or right next to another fragment, as happens when
    an abbreviation is drawn out where the counter-ion stands."""
    from rdkit import Chem

    try:
        fragments = [list(f) for f in Chem.GetMolFrags(mol)]
    except Exception:  # noqa: BLE001
        return places
    if len(fragments) < 2:
        return places
    if only_touching and not _fragments_touch(mol, places, fragments):
        return places
    out = dict(places)

    def box(atoms: Iterable[int]) -> tuple[float, float, float, float]:
        xs = [out[i][0] for i in atoms]
        ys = [out[i][1] for i in atoms]
        return min(xs), min(ys), max(xs), max(ys)

    gap = _FRAGMENT_GAP * BOND
    placed: list[list[int]] = []
    for fragment in sorted(fragments, key=lambda f: (box(f)[0], f[0])):
        for _ in range(len(placed) + 1):
            mine = box(fragment)
            hits = [other for other in placed
                    if mine[0] < (o := box(other))[2] + gap and o[0] < mine[2] + gap
                    and mine[1] < o[3] + gap and o[1] < mine[3] + gap]
            if not hits:
                break
            shift = max(box(other)[2] for other in hits) + gap - mine[0]
            for i in fragment:
                out[i] = (out[i][0] + shift, out[i][1])
        placed.append(fragment)
    return out if only_touching else (_scaled(mol, out) or out)


def _fragments_touch(mol: Any, places: Places, fragments: list[list[int]]) -> bool:
    owner = {i: n for n, fragment in enumerate(fragments) for i in fragment}
    atoms = sorted(places)
    limit = 0.8 * BOND
    for n, i in enumerate(atoms):
        for j in atoms[n + 1:]:
            if owner[i] != owner[j] and abs(places[i][0] - places[j][0]) < limit \
                    and _distance(places[i], places[j]) < limit:
                return True
    bonds = _bond_pairs(mol)
    for n, (a, b) in enumerate(bonds):
        for c, d in bonds[n + 1:]:
            if owner[a] != owner[c] and _cross(places[a], places[b], places[c], places[d]):
                return True
    for n, fragment in enumerate(fragments):
        if len(fragment) < 3:
            continue
        xs = [places[i][0] for i in fragment]
        ys = [places[i][1] for i in fragment]
        for i in atoms:
            if owner[i] != n and min(xs) < places[i][0] < max(xs) \
                    and min(ys) < places[i][1] < max(ys):
                return True
    return False


# --------------------------------------------------------------------------- matching

def _any_bond_query(drawn: Any, keep: list[int]) -> Any:
    """``drawn`` (its atoms ``keep``) as a pattern of elements and
    connections only: no bond orders, charges or aromaticity, and a
    placeholder matches any atom."""
    from rdkit import Chem

    index = {i: n for n, i in enumerate(keep)}
    query = Chem.RWMol()
    for i in keep:
        number = drawn.GetAtomWithIdx(i).GetAtomicNum()
        query.AddAtom(Chem.AtomFromSmarts(f"[#{number}]" if number else "*"))
    template = Chem.MolFromSmarts("*~*")
    any_bond = template.GetBondWithIdx(0)
    for bond in drawn.GetBonds():
        a, b = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if a in index and b in index:
            query.AddBond(index[a], index[b], Chem.BondType.SINGLE)
            query.ReplaceBond(query.GetBondBetweenAtoms(index[a], index[b]).GetIdx(), any_bond)
    return query.GetMol()


def _is_plain_hydrogen(atom: Any) -> bool:
    return atom.GetAtomicNum() == 1 and atom.GetIsotope() == 0 and atom.GetDegree() == 1


def _exact_query(drawn: Any, keep: list[int]) -> Any:
    """``drawn`` (its atoms ``keep``) as a pattern with its bond orders,
    charges and stereo, or None when it cannot be made (it does not
    sanitise). Atom ``n`` of the pattern is ``keep[n]``."""
    from rdkit import Chem

    work = Chem.RWMol(drawn)
    kept = set(keep)
    for index in sorted((a.GetIdx() for a in work.GetAtoms() if a.GetIdx() not in kept),
                        reverse=True):
        work.RemoveAtom(index)
    for atom in work.GetAtoms():
        if atom.GetAtomicNum() == 0:
            work.ReplaceAtom(atom.GetIdx(), Chem.AtomFromSmarts("*"), preserveProps=False)
    try:
        if Chem.SanitizeMol(work, catchErrors=True) != Chem.SanitizeFlags.SANITIZE_NONE:
            return None
        Chem.AssignChiralTypesFromBondDirs(work)
        Chem.DetectBondStereochemistry(work)
        Chem.AssignStereochemistry(work, cleanIt=True, force=True)
    except Exception:  # noqa: BLE001
        return None
    return work.GetMol()


#: Heavy atoms of the groups chemists abbreviate most, by label (lower case,
#: letters and digits only). Only used to tell several labels on one atom
#: apart ("Me" and "Ph" on a phosphorus); a label that is not here is no
#: problem.
_LABEL_ATOMS = {
    "me": 1, "ch3": 1, "et": 2, "pr": 3, "npr": 3, "ipr": 3, "bu": 4, "nbu": 4, "ibu": 4,
    "sbu": 4, "tbu": 4, "ph": 6, "bn": 7, "cy": 6, "vinyl": 2, "allyl": 3, "ac": 3, "bz": 8,
    "ome": 2, "meo": 2, "oet": 3, "eto": 3, "oac": 4, "aco": 4, "obn": 8, "bno": 8, "otf": 8,
    "tfo": 8, "ots": 11, "tso": 11, "oms": 5, "mso": 5, "otbs": 8, "tbso": 8, "oh": 1, "ho": 1,
    "nh2": 1, "h2n": 1, "no2": 3, "o2n": 3, "cn": 2, "nc": 2, "cf3": 4, "f3c": 4, "ccl3": 4,
    "cho": 2, "ohc": 2, "cooh": 3, "co2h": 3, "hooc": 3, "ho2c": 3, "co2me": 4, "meo2c": 4,
    "coome": 4, "co2et": 5, "eto2c": 5, "cooet": 5, "boc": 7, "cbz": 10, "fmoc": 17, "ts": 10,
    "tos": 10, "ms": 4, "tf": 7, "tms": 4, "tes": 7, "tbs": 7, "tbdms": 7, "tips": 10,
    "tbdps": 17, "pmb": 9, "bpin": 9, "pinb": 9, "nme2": 3, "me2n": 3, "net2": 5, "et2n": 5,
    "so3h": 4, "ho3s": 4, "sme": 2, "mes": 2, "sph": 7, "phs": 7, "oph": 7, "pho": 7,
}


def _label_score(mol: Any, drawn: Any, mapping: dict[int, int]) -> int:
    """How many labelled placeholders of the drawing got a group of the size
    their label says ("Me": one atom, "Ph": six)."""
    used = set(mapping.values())
    score = 0
    for d, m in mapping.items():
        source = drawn.GetAtomWithIdx(d)
        if source.GetAtomicNum() != 0 or not source.HasProp("molFileAlias"):
            continue
        label = "".join(ch for ch in source.GetProp("molFileAlias").lower() if ch.isalnum())
        size = _LABEL_ATOMS.get(label)
        if size is None:
            continue
        group, todo = {m}, [m]
        while todo and len(group) <= size + 1:
            for neighbour in mol.GetAtomWithIdx(todo.pop()).GetNeighbors():
                index = neighbour.GetIdx()
                if index not in used and index not in group:
                    group.add(index)
                    todo.append(index)
        if len(group) == size:
            score += 1
    return score


def _matchings(mol: Any, drawn: Any) -> list[dict[int, int]]:
    """Ways to lay the drawing onto ``mol``: each ``{atom of drawn: atom of
    mol}``, the most trustworthy first. Explicit hydrogens of the drawing are
    left out (``mol`` has none).

    Three kinds of pattern are tried, the strictest first: the drawing with
    its stereo, with its bond orders only, and as elements and connections
    only (the two answers of MolScribe may differ in a bond order or a
    charge; the SMILES is the structure, the drawing gives the places). Each
    first with the placeholders kept (a placeholder then stands where the
    first atom of its group goes), then without them. Among the matchings of
    one kind, those that give every label a group of its size come first."""
    heavy = [a.GetIdx() for a in drawn.GetAtoms() if not _is_plain_hydrogen(a)]
    real = [i for i in heavy if drawn.GetAtomWithIdx(i).GetAtomicNum() != 0]
    out: list[dict[int, int]] = []
    seen: set[tuple[tuple[int, int], ...]] = set()

    def collect(query: Any, keep: list[int], chirality: bool) -> None:
        if query is None or len(out) >= _MAX_MATCHES:
            return
        try:
            found = mol.GetSubstructMatches(query, uniquify=False, useChirality=chirality,
                                            maxMatches=_MAX_MATCHES)
        except Exception:  # noqa: BLE001
            return
        fresh = []
        for match in found:
            mapping = dict(zip(keep, match))
            key = tuple(sorted(mapping.items()))
            if key not in seen:
                seen.add(key)
                fresh.append(mapping)
        fresh.sort(key=lambda mapping: -_label_score(mol, drawn, mapping))
        out.extend(fresh[:_MAX_MATCHES - len(out)])

    for keep in (heavy, real) if len(real) != len(heavy) else (heavy,):
        if len(keep) < 1 or len(keep) > mol.GetNumAtoms():
            continue
        try:
            exact = _exact_query(drawn, keep)
        except Exception:  # noqa: BLE001
            exact = None
        collect(exact, keep, True)
        collect(exact, keep, False)
        try:
            collect(_any_bond_query(drawn, keep), keep, False)
        except Exception:  # noqa: BLE001
            pass
        if out:
            break
    return out


# --------------------------------------------------------------------------- the molfile

def _canonical(mol: Any) -> str:
    from rdkit import Chem

    return Chem.MolToSmiles(mol)


def _read_back(block: str) -> Any:
    from rdkit import Chem

    try:
        return Chem.MolFromMolBlock(block, sanitize=True, removeHs=True, strictParsing=False)
    except Exception:  # noqa: BLE001
        return None


def reads_back_as(block: str, mol: Any) -> bool:
    """True when the molfile ``block``, read again, is the structure ``mol``:
    the same canonical SMILES, stereocentres and double-bond geometry
    included."""
    back = _read_back(block)
    if back is None:
        return False
    try:
        return _canonical(back) == _canonical(mol)
    except Exception:  # noqa: BLE001
        return False


class _Block:
    """The lines of a V2000 molfile, with its bond lines open to change."""

    def __init__(self, text: str):
        self.lines = text.split("\n")
        counts = self.lines[3]
        if not counts.rstrip().endswith("V2000"):
            raise ValueError("not V2000")
        self.atoms = int(counts[0:3])
        self.bonds = int(counts[3:6])
        self.first = 4 + self.atoms

    def bond(self, n: int) -> tuple[int, int, int, int]:
        line = self.lines[self.first + n]
        stereo = int(line[9:12]) if line[9:12].strip() else 0
        return int(line[0:3]) - 1, int(line[3:6]) - 1, int(line[6:9]), stereo

    def set_bond(self, n: int, begin: int, end: int, stereo: int) -> None:
        line = self.lines[self.first + n]
        order = int(line[6:9])
        self.lines[self.first + n] = f"{begin + 1:3d}{end + 1:3d}{order:3d}{stereo:3d}" + line[12:]

    def find(self, a: int, b: int) -> int | None:
        for n in range(self.bonds):
            begin, end, _order, _stereo = self.bond(n)
            if {begin, end} == {a, b}:
                return n
        return None

    def wedges(self) -> dict[int, int]:
        """Bond number -> 1 (wedge) or 6 (hash)."""
        return {n: s for n in range(self.bonds)
                if (s := self.bond(n)[3]) in (1, 6) and self.bond(n)[2] == 1}

    def copy(self) -> "_Block":
        other = _Block.__new__(_Block)
        other.lines = list(self.lines)
        other.atoms, other.bonds, other.first = self.atoms, self.bonds, self.first
        return other

    def text(self) -> str:
        lines = list(self.lines)
        if self.wedges():
            # One enantiomer: the chiral flag (columns 13 to 15 of the counts line).
            lines[3] = lines[3][:12] + "  1" + lines[3][15:]
        return "\n".join(lines)


def _write(mol: Any, places: Places) -> str | None:
    """RDKit's molfile of ``mol`` on ``places`` (its own choice of wedges)."""
    from rdkit import Chem
    from rdkit.Geometry import Point3D

    work = Chem.Mol(mol)
    work.RemoveAllConformers()
    conformer = Chem.Conformer(work.GetNumAtoms())
    conformer.Set3D(False)
    for index, (x, y) in places.items():
        conformer.SetAtomPosition(index, Point3D(round(x, 4), round(y, 4), 0.0))
    work.AddConformer(conformer, assignId=True)
    for bond in work.GetBonds():
        for prop in ("_MolFileBondStereo", "_MolFileBondCfg"):
            if bond.HasProp(prop):
                bond.ClearProp(prop)
    try:
        return Chem.MolToMolBlock(work, forceV3000=False)
    except Exception:  # noqa: BLE001
        return None


def _with_wedges(shown: Any, places: Places, wanted: list[tuple[int, int, int]],
                 mol: Any = None) -> tuple[str, bool] | None:
    """The molfile of ``shown`` on ``places`` and whether it reads back as
    ``mol`` (``shown`` itself, or the same structure without the hydrogens
    that are drawn as atoms). ``wanted``: the picture's wedges as ``(from
    atom, to atom, 1 or 6)`` in atoms of ``shown``; they are used as far as
    they tell the truth."""
    mol = shown if mol is None else mol
    text = _write(shown, places)
    if text is None:
        return None
    try:
        base = _Block(text)
    except (ValueError, IndexError):
        return text, reads_back_as(text, mol)           # V3000: RDKit's wedges as they are
    if not reads_back_as(base.text(), mol):
        return base.text(), False
    if not wanted:
        return base.text(), True

    # 1. The picture's wedges, all of them and nothing else.
    exact = base.copy()
    for n in exact.wedges():
        begin, end, _order, _stereo = exact.bond(n)
        exact.set_bond(n, begin, end, 0)
    usable = True
    for begin, end, stereo in wanted:
        n = exact.find(begin, end)
        if n is None or exact.bond(n)[2] != 1:
            usable = False
            break
        exact.set_bond(n, begin, end, stereo)
    if usable and reads_back_as(exact.text(), mol):
        return exact.text(), True

    # 2. RDKit's wedges, each moved onto the bond the picture has wedged at
    #    that atom when the molfile still tells the truth then.
    current = base
    for n, _stereo in sorted(base.wedges().items()):
        centre = base.bond(n)[0]
        for begin, end, stereo in wanted:
            if begin != centre:
                continue
            target = current.find(begin, end)
            if target is None or target == n or current.bond(target)[2] != 1 \
                    or current.bond(target)[3] != 0:
                continue
            done = False
            for value in (stereo, 7 - stereo):
                trial = current.copy()
                old_begin, old_end, _o, _s = trial.bond(n)
                trial.set_bond(n, old_begin, old_end, 0)
                trial.set_bond(target, begin, end, value)
                if reads_back_as(trial.text(), mol):
                    current, done = trial, True
                    break
            if done:
                break
    return current.text(), True


def _drawn_wedges(drawn: Any) -> list[tuple[int, int, int]]:
    """The wedges of the drawing: ``(from atom, to atom, 1 or 6)``."""
    from rdkit import Chem

    out = []
    for bond in drawn.GetBonds():
        direction = bond.GetBondDir()
        stereo = 1 if direction == Chem.BondDir.BEGINWEDGE else \
            6 if direction == Chem.BondDir.BEGINDASH else 0
        if not stereo and bond.HasProp("_MolFileBondStereo"):
            try:
                stereo = int(bond.GetProp("_MolFileBondStereo"))
            except ValueError:
                stereo = 0
        if stereo in (1, 6) and bond.GetBondType() == Chem.BondType.SINGLE:
            out.append((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx(), stereo))
    return out


def _carry_labels(mol: Any, drawn: Any, mapping: dict[int, int]) -> Any:
    """``mol`` with the label of a placeholder of the drawing ("R1", "Ar")
    on the placeholder it was matched to, so it is still shown."""
    from rdkit import Chem

    out = None
    for d, m in mapping.items():
        source = drawn.GetAtomWithIdx(d)
        if source.GetAtomicNum() != 0 or mol.GetAtomWithIdx(m).GetAtomicNum() != 0:
            continue
        for prop in ("molFileAlias", "dummyLabel"):
            if source.HasProp(prop) and source.GetProp(prop).strip():
                out = Chem.Mol(mol) if out is None else out
                out.GetAtomWithIdx(m).SetProp(prop, source.GetProp(prop))
    return mol if out is None else out


def _with_drawn_hydrogens(mol: Any, drawn: Any,
                          mapping: dict[int, int]) -> tuple[Any, dict[int, int]]:
    """``mol`` with the hydrogens that the picture draws as atoms (the "H" at
    a ring fusion, usually on a wedge) added as atoms too, and the matching
    extended to them, so that the wedge can stay on the bond to that H. Only
    where the drawing shows all hydrogens of the atom."""
    from rdkit import Chem

    drawn_on: dict[int, list[int]] = {}
    for atom in drawn.GetAtoms():
        if _is_plain_hydrogen(atom):
            neighbour = atom.GetNeighbors()[0].GetIdx()
            if neighbour in mapping:
                drawn_on.setdefault(mapping[neighbour], []).append(atom.GetIdx())
    keep = {m: hs for m, hs in drawn_on.items()
            if mol.GetAtomWithIdx(m).GetTotalNumHs() == len(hs)
            and mol.GetAtomWithIdx(m).GetAtomicNum() != 0}
    if not keep:
        return mol, mapping
    shown = Chem.AddHs(mol, onlyOnAtoms=sorted(keep))
    extended = dict(mapping)
    for m, hydrogens in keep.items():
        added = sorted(n.GetIdx() for n in shown.GetAtomWithIdx(m).GetNeighbors()
                       if n.GetIdx() >= mol.GetNumAtoms())
        if len(added) != len(hydrogens):
            return mol, mapping
        extended.update(zip(hydrogens, added))
    return shown, extended


def _on_the_picture(mol: Any, drawn: Any, picture: Places, mapping: dict[int, int]) -> Places | None:
    """Every atom of ``mol`` placed: the matched ones where the picture has
    them, the others (an expanded abbreviation) by RDKit."""
    from rdkit import Chem
    from rdkit.Chem import rdDepictor
    from rdkit.Geometry import Point2D

    given = {m: picture[d] for d, m in mapping.items() if d in picture}
    if len(given) < 2 and mol.GetNumAtoms() > 1:
        return None
    scaled = _scaled(_Sub(mol, set(given)), given)
    if scaled is None:
        return None
    if len(scaled) == mol.GetNumAtoms():
        return scaled
    work = Chem.Mol(mol)
    work.RemoveAllConformers()
    try:
        rdDepictor.Compute2DCoords(
            work, coordMap={i: Point2D(x, y) for i, (x, y) in scaled.items()},
            canonOrient=False, clearConfs=True)
    except Exception:  # noqa: BLE001
        return None
    return _places_of(work)


class _Sub:
    """The bonds of a molecule between some of its atoms (for :func:`_scaled`)."""

    def __init__(self, mol: Any, atoms: set[int]):
        self._mol, self._atoms = mol, atoms

    def GetBonds(self) -> list[Any]:  # noqa: N802 - RDKit's name
        return [b for b in self._mol.GetBonds()
                if b.GetBeginAtomIdx() in self._atoms and b.GetEndAtomIdx() in self._atoms]


def drawing_molfile(mol: Any, drawn: Any = None, box: Sequence[float] | None = None,
                    seen: Sequence[float] | None = None) -> str | None:
    """The molfile to show for the structure ``mol`` (sanitised): on the
    places of the drawing ``drawn`` (MolScribe's molfile read without
    sanitising; ``box`` and ``seen`` as in :func:`picture_places`) when they
    can be used, else on a computed layout; with wedges that agree with
    ``mol``. See the module text. None when no molfile can be written."""
    from rdkit import rdBase

    with rdBase.BlockLogs():
        try:
            answer = _from_the_picture(mol, drawn, box, seen) if drawn is not None else None
        except Exception:  # noqa: BLE001 - any trouble: the computed layout
            answer = None
        if answer is not None:
            return answer
        try:
            places = fresh_layout(mol)
            written = _with_wedges(mol, places, []) if places is not None else None
            if written is not None:
                return written[0]
        except Exception:  # noqa: BLE001
            pass
        try:
            from rdkit import Chem
            from rdkit.Chem import rdDepictor

            work = Chem.Mol(mol)
            work.RemoveAllConformers()
            rdDepictor.Compute2DCoords(work)
            return Chem.MolToMolBlock(work, forceV3000=False)
        except Exception:  # noqa: BLE001
            return None


def _from_the_picture(mol: Any, drawn: Any, box: Any, seen: Any) -> str | None:
    picture = picture_places(drawn, box, seen)
    if picture is None:
        return None
    wedges = _drawn_wedges(drawn)
    for heavy in _matchings(mol, drawn):
        shown, mapping = _with_drawn_hydrogens(mol, drawn, heavy)
        raw = _on_the_picture(shown, drawn, picture, mapping)
        if raw is None or len(raw) != shown.GetNumAtoms():
            continue
        if _atoms_on_each_other(raw):
            continue                      # no drawing to keep: two atoms on one spot
        shown = _carry_labels(shown, drawn, mapping)
        wanted = [(mapping[a], mapping[b], s) for a, b, s in wedges if a in mapping and b in mapping]
        neat = tidy(shown, raw)
        for places in ((neat, raw) if neat is not raw else (raw,)):
            places = _side_by_side(shown, places, only_touching=True)
            written = _with_wedges(shown, places, wanted, mol)
            if written is not None and written[1]:
                return written[0]
    return None


def _atoms_on_each_other(places: Places) -> bool:
    atoms = sorted(places)
    limit = 0.12 * BOND
    return any(_distance(places[i], places[j]) < limit
               for n, i in enumerate(atoms) for j in atoms[n + 1:])
