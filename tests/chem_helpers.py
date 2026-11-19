"""Helpers for the chemistry normalisation tests.

They only BUILD inputs (molfiles, SMILES variants) with RDKit and edit V2000
text the way another drawing program would have written it. Nothing here
computes registry keys: the tests compare keys produced by
``compound_registry.chem`` with each other.
"""

from __future__ import annotations

import math
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import AllChem, rdDepictor

DATA = Path(__file__).parent / "data"


def read_fixture(name: str) -> str:
    """Molfile from tests/data, verbatim (LF line endings as stored)."""
    return (DATA / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# molfile generation with RDKit
# ---------------------------------------------------------------------------

def mol_2d(smiles: str) -> Chem.Mol:
    """RDKit molecule (CXSMILES extensions honoured) with 2D coordinates."""
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None, f"test input does not parse: {smiles!r}"
    rdDepictor.Compute2DCoords(mol)
    return mol


def molblock(smiles: str, *, v3000: bool = False, aromatic_bonds: bool = False,
             explicit_h: bool = False, renumber_seed: int | None = None) -> str:
    """Molfile of ``smiles`` drawn in 2D.

    v3000           write a V3000 file (needed for enhanced stereo collections)
    aromatic_bonds  write aromatic bonds as bond type 4 instead of Kekule
    explicit_h      draw every hydrogen as an atom
    renumber_seed   shuffle the atom order (deterministically) before writing
    """
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None, f"test input does not parse: {smiles!r}"
    if explicit_h:
        mol = Chem.AddHs(mol)
    if renumber_seed is not None:
        mol = renumbered(mol, renumber_seed)
    rdDepictor.Compute2DCoords(mol)
    if v3000:
        return Chem.MolToV3KMolBlock(mol, kekulize=not aromatic_bonds)
    return Chem.MolToMolBlock(mol, kekulize=not aromatic_bonds)


def kekule_molblock(kekule_smiles: str) -> str:
    """Molfile that keeps exactly the single/double bond pattern written in
    ``kekule_smiles`` (no aromatisation / re-kekulisation by RDKit)."""
    mol = Chem.MolFromSmiles(kekule_smiles, sanitize=False)
    mol.UpdatePropertyCache(strict=False)
    Chem.FastFindRings(mol)
    rdDepictor.Compute2DCoords(mol)
    return Chem.MolToMolBlock(mol, kekulize=False)


def molblock_3d(smiles: str, seed: int = 0xC0FFEE) -> str:
    """3D molfile (explicit H, embedded conformer, no wedges)."""
    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
    assert AllChem.EmbedMolecule(mol, randomSeed=seed) == 0
    return Chem.MolToMolBlock(mol)


def renumbered(mol: Chem.Mol, seed: int) -> Chem.Mol:
    """Same molecule, atoms in a deterministic pseudo-random order."""
    n = mol.GetNumAtoms()
    order = list(range(n))
    # small LCG so the permutation does not depend on Python's random module
    state = seed or 1
    for i in range(n - 1, 0, -1):
        state = (state * 1103515245 + 12345) % (2 ** 31)
        j = state % (i + 1)
        order[i], order[j] = order[j], order[i]
    if order == list(range(n)) and n > 1:
        order = order[1:] + order[:1]
    return Chem.RenumberAtoms(mol, order)


# ---------------------------------------------------------------------------
# V2000 text editing (keeps every other byte of the file untouched)
# ---------------------------------------------------------------------------

def _split_v2000(mb: str):
    lines = mb.splitlines()
    counts = lines[3]
    assert counts.rstrip().endswith("V2000"), "not a V2000 molfile"
    n_atoms, n_bonds = int(counts[0:3]), int(counts[3:6])
    return lines, n_atoms, n_bonds


def _join(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def set_chiral_flag(mb: str, flag: int) -> str:
    lines, _, _ = _split_v2000(mb)
    c = lines[3]
    lines[3] = c[:12] + f"{flag:3d}" + c[15:]
    return _join(lines)


def set_bond_stereo(mb: str, begin: int, end: int, flag: int) -> str:
    """Set the stereo flag of bond {begin, end} (1-based) and make ``begin``
    its first atom (a wedge starts at its first atom).
    flag: 0 plain, 1 wedge, 4 wavy (either), 6 hash; for double bonds 3 = crossed."""
    lines, n_atoms, n_bonds = _split_v2000(mb)
    for i in range(4 + n_atoms, 4 + n_atoms + n_bonds):
        l = lines[i]
        a, b, t = int(l[0:3]), int(l[3:6]), int(l[6:9])
        if {a, b} == {begin, end}:
            lines[i] = f"{begin:3d}{end:3d}{t:3d}{flag:3d}" + l[12:]
            return _join(lines)
    raise KeyError(f"no bond {begin}-{end}")


def strip_wedges(mb: str) -> str:
    """Same drawing with every single-bond wedge/hash/wavy removed (flat)."""
    lines, n_atoms, n_bonds = _split_v2000(mb)
    for i in range(4 + n_atoms, 4 + n_atoms + n_bonds):
        l = lines[i]
        if int(l[6:9]) == 1 and int(l[9:12]) in (1, 4, 6):
            lines[i] = l[:9] + "  0" + l[12:]
    return _join(lines)


def transform_coords(mb: str, fn, *, swap_wedges: bool = False) -> str:
    """Apply ``fn(x, y, z) -> (x, y, z)`` to every atom. With ``swap_wedges``
    every wedge becomes a hash and vice versa (what a mirror in the drawing
    plane needs to keep the same molecule)."""
    lines, n_atoms, n_bonds = _split_v2000(mb)
    for i in range(4, 4 + n_atoms):
        l = lines[i]
        x, y, z = float(l[0:10]), float(l[10:20]), float(l[20:30])
        x, y, z = fn(x, y, z)
        lines[i] = f"{x:10.4f}{y:10.4f}{z:10.4f}" + l[30:]
    if swap_wedges:
        for i in range(4 + n_atoms, 4 + n_atoms + n_bonds):
            l = lines[i]
            if int(l[6:9]) == 1 and int(l[9:12]) in (1, 6):
                new = 6 if int(l[9:12]) == 1 else 1
                lines[i] = l[:9] + f"{new:3d}" + l[12:]
    return _join(lines)


def rotate(angle_deg: float, dx: float = 0.0, dy: float = 0.0):
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return lambda x, y, z: (c * x - s * y + dx, s * x + c * y + dy, z)


def mirror_x(x, y, z):
    return (-x, y, z)


def renumber_v2000(mb: str, new_order: list[int]) -> str:
    """Rewrite a V2000 molfile with its atoms in another order.

    ``new_order[k]`` is the 1-based OLD index of the atom that becomes atom k+1.
    Bond lines keep their begin/end orientation (so wedges still start at the
    same atom) and their stereo flags; property lines (CHG, ISO, RGP) are
    remapped. Only the property lines this suite uses are supported.
    """
    lines, n_atoms, n_bonds = _split_v2000(mb)
    assert sorted(new_order) == list(range(1, n_atoms + 1))
    old_to_new = {old: new for new, old in enumerate(new_order, start=1)}
    atom_lines = lines[4:4 + n_atoms]
    out = lines[:4] + [atom_lines[old - 1] for old in new_order]
    for i in range(4 + n_atoms, 4 + n_atoms + n_bonds):
        l = lines[i]
        a, b = int(l[0:3]), int(l[3:6])
        out.append(f"{old_to_new[a]:3d}{old_to_new[b]:3d}" + l[6:])
    for l in lines[4 + n_atoms + n_bonds:]:
        if l.startswith(("M  CHG", "M  ISO", "M  RGP")):
            n = int(l[6:9])
            fields = l[9:].split()
            pairs = [(old_to_new[int(fields[2 * k])], int(fields[2 * k + 1])) for k in range(n)]
            l = l[:6] + f"{n:3d}" + "".join(f" {a:3d} {v:3d}" for a, v in pairs)
        elif l.startswith("M  ") and not l.startswith("M  END"):
            raise NotImplementedError(l)
        out.append(l)
    return _join(out)


def replace_atom_symbol(mb: str, atom: int, symbol: str, extra_lines: tuple[str, ...] = ()) -> str:
    """Change the element symbol of atom ``atom`` (1-based); ``extra_lines``
    are inserted just before ``M  END``."""
    lines, n_atoms, _ = _split_v2000(mb)
    l = lines[3 + atom]
    lines[3 + atom] = l[:31] + f"{symbol:<3}" + l[34:]
    end = lines.index("M  END")
    lines[end:end] = list(extra_lines)
    return _join(lines)


# ---------------------------------------------------------------------------
# key comparison
# ---------------------------------------------------------------------------

def keys(n) -> tuple[str, str, str]:
    """(core_key, stereo_key, stereo_mode): equal tuples == same compound."""
    return (n.core_key, n.stereo_key, n.stereo_mode)


def stereo_part(n) -> tuple[str, str]:
    return (n.stereo_key, n.stereo_mode)
