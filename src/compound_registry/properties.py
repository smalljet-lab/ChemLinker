"""Display-only values computed from a structure: sum formula, masses, composition.

Nothing here is part of any registry key, and nothing here may ever be used to
decide whether two structures are the same compound (that is ``chem``). The
values are what a chemist expects next to a drawing:

``sum_formula``  Hill formula of everything drawn, all fragments together
                 (a salt counts with its counter-ion), as RDKit's
                 ``CalcMolFormula`` writes it: a net charge is appended
                 (``C4H12N+``, ``Fe+3``); isotopes count as their element.
``mol_weight``   average molar mass in g/mol, 3 decimals.
``exact_mass``   monoisotopic mass, 5 decimals (an isotope label uses the
                 mass of that isotope).
``composition``  mass percent per element, 2 decimals, in the element order of
                 ``sum_formula``. Implicit hydrogens count; an isotope label
                 counts with its own mass under its element.

:func:`compute_properties` never raises and never writes to the RDKit log: a
structure it cannot read gives ``None``. :func:`read_molblock` is the quiet
molfile reader it uses; the compound search reuses it for stored molfiles.
"""

from __future__ import annotations

import re
from typing import Any

from rdkit import Chem, rdBase
from rdkit.Chem import Descriptors, rdMolDescriptors

__all__ = ["compute_properties", "read_molblock", "parse_formula", "formula_counts"]

_ELEMENT_RE = re.compile(r"([A-Z][a-z]?)(\d*)")
_FRAGMENT_RE = re.compile(r"^(\d*)((?:[A-Z][a-z]?\d*)+)$")
# "+", "-", "+2" (as CalcMolFormula writes it) or "++". ("2+" is not accepted:
# in "SO42-" the digits could belong to the oxygen.)
_CHARGE_RE = re.compile(r"(?:([+-])(\d*)|([+-]{2,}))$")


def read_molblock(molblock: str, *, remove_hs: bool = True) -> Chem.Mol | None:
    """Sanitised molecule from a V2000/V3000 molfile, or None. Quiet, never raises.

    Like ``chem``, a file whose three header lines were dropped is tried again
    with the counts line put back on line 4.
    """
    try:
        if not isinstance(molblock, str) or not molblock.strip():
            return None
        text = molblock.replace("\r\n", "\n").replace("\r", "\n")
        candidates = [text]
        lines = text.split("\n")
        for i, line in enumerate(lines[:4]):
            if line.rstrip().endswith(("V2000", "V3000")) and i != 3:
                if i < 3:
                    candidates.append("\n".join([""] * (3 - i) + lines))
                break
        with rdBase.BlockLogs():
            for candidate in candidates:
                try:
                    mol = Chem.MolFromMolBlock(candidate, sanitize=True, removeHs=remove_hs,
                                               strictParsing=False)
                except Exception:  # noqa: BLE001
                    mol = None
                if mol is not None:
                    return mol
    except Exception:  # noqa: BLE001 - a display value must never break the caller
        return None
    return None


def compute_properties(molblock: str) -> dict[str, Any] | None:
    """``{"sum_formula", "mol_weight", "exact_mass", "composition"}`` of every
    fragment of ``molblock`` together, or None when it cannot be computed (an
    unreadable file, an empty structure, a placeholder atom)."""
    try:
        mol = read_molblock(molblock, remove_hs=False)
        if mol is None or mol.GetNumAtoms() == 0:
            return None
        with rdBase.BlockLogs():
            if any(a.GetAtomicNum() == 0 or a.HasQuery() for a in mol.GetAtoms()):
                return None
            formula = rdMolDescriptors.CalcMolFormula(mol)
            mol_weight = Descriptors.MolWt(mol)
            exact_mass = Descriptors.ExactMolWt(mol)
            h_mass = Chem.GetPeriodicTable().GetAtomicWeight(1)
            masses: dict[str, float] = {}
            for atom in mol.GetAtoms():
                symbol = atom.GetSymbol()
                masses[symbol] = masses.get(symbol, 0.0) + atom.GetMass()
                implicit = atom.GetTotalNumHs(includeNeighbors=False)
                if implicit:
                    masses["H"] = masses.get("H", 0.0) + implicit * h_mass
        total = sum(masses.values())
        if not formula or total <= 0:
            return None
        order = list(dict.fromkeys(symbol for symbol, _ in _ELEMENT_RE.findall(formula)))
        order += [symbol for symbol in masses if symbol not in order]  # (never expected)
        return {
            "sum_formula": formula,
            "mol_weight": round(mol_weight, 3),
            "exact_mass": round(exact_mass, 5),
            "composition": [{"element": symbol, "percent": round(100.0 * masses[symbol] / total, 2)}
                            for symbol in order if symbol in masses],
        }
    except Exception:  # noqa: BLE001 - display only: never break a lookup
        return None


# --------------------------------------------------------------------------- formula text

_ELEMENTS: frozenset[str] | None = None


def _elements() -> frozenset[str]:
    """Symbols of the elements 1..118."""
    global _ELEMENTS
    if _ELEMENTS is None:
        table = Chem.GetPeriodicTable()
        _ELEMENTS = frozenset(table.GetElementSymbol(z) for z in range(1, 119))
    return _ELEMENTS


def parse_formula(text: str) -> tuple[dict[str, int], int] | None:
    """``(element counts, net charge)`` of a formula written by a person or by
    ``CalcMolFormula``, or None when ``text`` is not formula-like.

    Accepted: element symbols with counts in any order (``C2H6O``,
    ``CH3CH2OH``), fragments joined by ``.`` with an optional leading
    multiplier (``C2H7N.ClH``, ``C10H12N2.2ClH``), and one trailing charge
    (``+``, ``-``, ``+2``, ``++``). Spaces are ignored. Every symbol must be
    an element of the periodic table (so ``ABC-008`` is not a formula).
    """
    if not isinstance(text, str):
        return None
    compact = "".join(text.split())
    if not compact:
        return None
    charge = 0
    m = _CHARGE_RE.search(compact)
    if m is not None and m.start() > 0:
        if m.group(1):
            charge = (1 if m.group(1) == "+" else -1) * int(m.group(2) or 1)
        else:
            signs = m.group(3)
            if len(set(signs)) != 1:
                return None
            charge = (1 if signs[0] == "+" else -1) * len(signs)
        compact = compact[: m.start()]
    counts: dict[str, int] = {}
    for fragment in compact.split("."):
        fm = _FRAGMENT_RE.match(fragment)
        if fm is None:
            return None
        times = int(fm.group(1)) if fm.group(1) else 1
        if times == 0:
            return None
        for symbol, number in _ELEMENT_RE.findall(fm.group(2)):
            if symbol not in _elements():
                return None
            counts[symbol] = counts.get(symbol, 0) + times * (int(number) if number else 1)
    if not counts or any(v == 0 for v in counts.values()):
        return None
    return counts, charge


def formula_counts(formula: str) -> dict[str, int] | None:
    """Element counts of a formula without charge (e.g. the per-fragment
    formula stored for a CORE, ``C2H7N.ClH``), or None."""
    parsed = parse_formula(formula)
    return parsed[0] if parsed is not None else None
