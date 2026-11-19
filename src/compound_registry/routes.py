"""Synthesis routes: walking back from a compound through the stored reactions.

:func:`build_route` is pure: it reads the registry only through
:class:`~compound_registry.route_model.RouteData`, does no I/O and returns the
same :class:`~compound_registry.route_model.Route` for the same data. What it
decides (the rules of ``protocol.py``, "Synthesis routes", in detail):

Which experiments make a compound C
    Every active batch of C whose yield is above zero (isolated, else
    analytical); a batch without one leaves a note and is never asked for its
    reaction. An experiment of such a batch without a stored reaction is listed
    once in ``Route.missing``. A batch is not drawn when C is also one of the
    reactants of that experiment (recovered starting material), with a note.

What a reactant row is
    1. The ELN link (``source_experiment_ref``): the registered product of
       that experiment whose stereo variant is the row's structure; a row
       without a structure takes the linked experiment's product when that
       experiment made exactly one stereo variant. A link that does not match
       leaves a note and the row is read by its structure.
    2. Its structure: a registered stereo variant ("cs:" key), else an
       unregistered structure (RDKit standard InChIKey, "ik:" key).
    3. No readable structure: its name ("nm:" key), or, without a name, the
       row itself ("rw:" key, never merged with anything).

Steps
    The runs of C whose reactants are the same compounds (the same multiset of
    keys) form one step. Each registered reactant that has steps of its own is
    walked back as well, every compound once (a loop is cut with a note), at
    most :data:`MAX_ROUTE_DEPTH` generations and :data:`MAX_ROUTE_STEPS`
    steps. Steps come in synthesis order: the steps making a reactant come
    before the step using it; of several reactants with a history, the one
    with the longest history is made last (directly before the step), so a
    drawing can continue from it.

Texts
    Reagents above the arrow (name, else formula), solvents and temperature
    below it, then one line per run (see ``route_model``, "Run line format").
    The reagent list, the solvent list and the temperature are compared
    separately: one that differs between the runs of a step is not written at
    the arrow but on each run's own line, in parentheses (the other two stay
    at the arrow). A run that has no reagents (or no solvents, or no
    temperature) recorded does not count as different.

Limiting reactant (the "SM" mass)
    The reactant the user chose for the experiment (``RouteData.limiting_choice``)
    when the stored reaction still has a reactant in that row whose name (else
    formula) equals, ignoring case, the one recorded with the choice; a choice
    that no longer applies is ignored with a note. Otherwise the automatic
    choice: among the reactants with 1 equivalent, the one whose amount equals
    the reference amount (0.5 % tolerance; the closest when several do, the
    first row on a tie); else the only reactant with 1 equivalent; else none.
    Actual values are used, the planned value of a field when the actual one
    is missing. Every run carries what was chosen, by whom, what the automatic
    rule gives and every reactant as a candidate (``Run`` fields), so the
    choice can be switched.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from rdkit import Chem, rdBase

from .route_model import (MAX_ROUTE_DEPTH, MAX_ROUTE_STEPS, BatchRef, Candidate,
                          MissingReaction, Participant, Route, RouteData, Run, Step, Text)

__all__ = ["build_route", "read_molfile", "inchikey_of", "is_formula_like", "format_mass",
           "format_yield", "natural_key", "limiting_reactant", "mass_in_grams"]

# --------------------------------------------------------------------------
# Small pure helpers (also used by the tests and the CDXML writer)
# --------------------------------------------------------------------------

_DIGIT_RUNS = re.compile(r"([0-9]+)")


def natural_key(text: str) -> tuple[Any, ...]:
    """Sort key comparing digit runs as numbers (ABC-9 before ABC-10) and the
    rest ignoring case."""
    parts = _DIGIT_RUNS.split(text.casefold())
    return tuple(int(p) if i % 2 else p for i, p in enumerate(parts))


def _batch_order(b: BatchRef) -> tuple[Any, ...]:
    """Natural ELN-number order; batches without an ELN number last, by their
    internal key; then by product number."""
    code = (b.experiment_code or "").strip()
    if code:
        return (0, natural_key(code), code, natural_key(b.experiment_ref), b.experiment_ref,
                b.product_index, b.full_id)
    return (1, natural_key(b.experiment_ref), b.experiment_ref, (), "", b.product_index, b.full_id)


def _exp_name(code: str | None, ref: str) -> str:
    """What an experiment is called in texts: its ELN number, else its key."""
    return code.strip() if isinstance(code, str) and code.strip() else ref


def read_molfile(molfile: str | None) -> Chem.Mol | None:
    """The drawing of a molfile exactly as drawn (not sanitised, explicit H
    atoms kept), or None when it cannot be read. Tolerates CRLF line ends and
    the headers other programs write (NIST, ISIS, CDK, ChemDraw, a SMILES on
    the name line), and a file whose three header lines are missing. Never
    raises."""
    if not isinstance(molfile, str) or not molfile.strip():
        return None
    text = molfile.replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith("$RXN"):
        return None
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
                mol = Chem.MolFromMolBlock(candidate, sanitize=False, removeHs=False,
                                           strictParsing=False)
            except Exception:  # noqa: BLE001 - any parser failure means "unreadable"
                mol = None
            if mol is not None and mol.GetNumAtoms() > 0:
                return mol
    return None


def inchikey_of(molfile: str | None) -> str | None:
    """RDKit's standard InChIKey of a molfile (stereo from its wedges), or None
    when it cannot be read. Tries the sanitised molecule first, then the
    drawing as it is. Never raises."""
    raw = read_molfile(molfile)
    if raw is None:
        return None
    with rdBase.BlockLogs():
        attempts = []
        try:
            text = molfile.replace("\r\n", "\n").replace("\r", "\n")  # type: ignore[union-attr]
            sane = Chem.MolFromMolBlock(text, sanitize=True, removeHs=True, strictParsing=False)
            if sane is not None and sane.GetNumAtoms() > 0:
                attempts.append(sane)
        except Exception:  # noqa: BLE001
            pass
        try:
            copy = Chem.Mol(raw)
            Chem.SanitizeMol(copy)
            Chem.AssignChiralTypesFromBondDirs(copy)
            Chem.AssignStereochemistry(copy, cleanIt=True, force=True)
            attempts.append(copy)
        except Exception:  # noqa: BLE001
            pass
        try:
            copy = Chem.Mol(raw)
            copy.UpdatePropertyCache(strict=False)
            Chem.AssignChiralTypesFromBondDirs(copy)
            attempts.append(copy)
        except Exception:  # noqa: BLE001
            pass
        for mol in attempts:
            if any(a.GetAtomicNum() == 0 for a in mol.GetAtoms()):
                return None
            try:
                key = Chem.MolToInchiKey(mol)
            except Exception:  # noqa: BLE001
                key = ""
            if key:
                return key
    return None


_ELEMENTS = frozenset("""
H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se
Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb
Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm
Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og D T
""".split())
#: Group abbreviations chemists write inside formulas ("Pd(PPh3)4", "Et3N",
#: "Pd2(dba)3", "B2pin2", "Pd(OAc)2").
_CAP_GROUPS = frozenset(["Ph", "Me", "Et", "Pr", "Bu", "Ac", "Bn", "Bz", "Ts", "Tf", "Ms", "Cy",
                         "Boc", "Cbz", "Fmoc", "Tol", "Mes", "Py", "Ns", "Piv", "Tr", "Ar",
                         "Np", "Ad"])
_LOWER_GROUPS = frozenset(["dba", "dppf", "dppe", "dppp", "dppb", "cod", "acac", "pin", "py",
                           "bpy", "bipy", "phen", "t", "n", "i", "s", "o", "m", "p"])
_FORMULA_TOKEN = re.compile(r"[A-Z][a-z]*|[a-z]+|[0-9]+|[()\[\]]|·|\.|(.)")


def is_formula_like(text: str | None) -> bool:
    """True when ``text`` reads as a sum or structural formula ("K2CO3",
    "Pd(PPh3)4", "CH2Cl2", "CsF", "C22H23N"), so its digits may be shown as
    subscripts. A name with a space, hyphen, comma or any word that is not an
    element symbol or a usual group abbreviation is not ("Potassium carbonate",
    "ABC-001", "Cyclohexene"). An all-capital acronym without digits
    ("THF", "DME", "NBS") is not either: it needs a digit or an element symbol
    with a small letter."""
    if not isinstance(text, str):
        return False
    t = text.strip()
    if not t or len(t) > 80:
        return False
    has_digit = has_small = has_symbol = False
    depth = 0
    previous = ""
    for match in _FORMULA_TOKEN.finditer(t):
        token = match.group(0)
        if match.group(1) is not None:
            return False                      # space, hyphen, comma, %, ...
        if token.isdigit():
            has_digit = True
        elif token in "([":
            depth += 1
        elif token in ")]":
            depth -= 1
            if depth < 0:
                return False
        elif token in ("·", "."):
            if previous in ("", "·", "."):
                return False
        elif token[0].isupper():
            if token in _ELEMENTS:
                has_symbol = True
            elif token not in _CAP_GROUPS:
                return False
            if any(c.islower() for c in token):
                has_small = True
        else:
            if token not in _LOWER_GROUPS:
                return False
            has_small = True
        previous = token
    return depth == 0 and has_symbol and (has_digit or has_small)


_LETTER_DIGIT = re.compile(r"[A-Za-z)\]][0-9]")


_MASS_TO_G = {"t": 1e6, "kg": 1e3, "g": 1.0, "mg": 1e-3, "µg": 1e-6, "μg": 1e-6, "ug": 1e-6,
              "mcg": 1e-6, "ng": 1e-9, "pg": 1e-12}
_AMOUNT_TO_MOL = {"kmol": 1e3, "mol": 1.0, "mmol": 1e-3, "µmol": 1e-6, "μmol": 1e-6,
                  "umol": 1e-6, "nmol": 1e-9, "pmol": 1e-12}


def _unit(text: object) -> str:
    return text.strip().casefold() if isinstance(text, str) else ""


def mass_in_grams(value: object, unit: object) -> float | None:
    """A mass converted to grams (units t, kg, g, mg, µg/ug, ng, pg), or None
    for a missing, non-positive or non-finite value or an unknown unit."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    factor = _MASS_TO_G.get(_unit(unit))
    if factor is None or not math.isfinite(number) or number <= 0:
        return None
    return number * factor


def _round_sig(value: float, digits: int = 3) -> tuple[float, int]:
    """``value`` rounded to ``digits`` significant figures and the number of
    decimals that shows them."""
    exponent = math.floor(math.log10(abs(value)))
    decimals = digits - 1 - exponent
    rounded = round(value, decimals)
    if rounded != 0 and math.floor(math.log10(abs(rounded))) != exponent:  # 9.996 -> 10.0
        decimals -= 1
        rounded = round(value, decimals)
    return rounded, max(decimals, 0)


def format_mass(grams: float) -> str:
    """Three significant figures, in g from 1000 mg on, else in mg: "4.01 g",
    "1.90 g", "599 mg", "66.0 mg"."""
    mg, decimals = _round_sig(grams * 1000.0)
    if mg >= 1000:
        g, decimals = _round_sig(grams)
        return f"{g:.{decimals}f} g"
    return f"{mg:.{decimals}f} mg"


def format_yield(percent: float) -> str:
    """At most two decimals, no trailing zeros: "52.4", "17.25", "70"."""
    text = f"{round(float(percent), 2):.2f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _pick(component: dict[str, Any], key: str, unit_key: str | None = None
          ) -> tuple[float | None, str | None]:
    """The actual value of an amount field (with its unit), else the planned
    one."""
    for which in ("actual", "planned"):
        amounts = component.get(which)
        if isinstance(amounts, dict):
            value = amounts.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and math.isfinite(float(value)):
                unit = amounts.get(unit_key) if unit_key else None
                return float(value), unit if isinstance(unit, str) else None
    return None, None


def _same_amount(value: float, unit: str | None, ref: float, ref_unit: str | None) -> float | None:
    """Relative difference of two amounts of substance, or None when they
    cannot be compared (units neither equal nor both convertible)."""
    if _unit(unit) == _unit(ref_unit):
        a, b = value, ref
    else:
        fa, fb = _AMOUNT_TO_MOL.get(_unit(unit)), _AMOUNT_TO_MOL.get(_unit(ref_unit))
        if fa is None or fb is None:
            return None
        a, b = value * fa, ref * fb
    if b == 0:
        return 0.0 if a == 0 else None
    return abs(a - b) / abs(b)


def _one_eq_indexes(reaction: dict[str, Any]) -> list[int]:
    """Indexes (in ``reaction["reactants"]``) of the reactants with 1
    equivalent (1e-6 tolerance)."""
    ones = []
    for i, component in enumerate(reaction.get("reactants") or []):
        if not isinstance(component, dict):
            continue
        eq, _ = _pick(component, "eq")
        if eq is not None and abs(eq - 1.0) <= 1e-6:
            ones.append(i)
    return ones


def limiting_reactant(reaction: dict[str, Any]) -> int | None:
    """Index (in ``reaction["reactants"]``) of the limiting reactant chosen
    automatically, or None.

    Among the reactants with 1 equivalent (1e-6 tolerance), the one whose
    amount of substance equals the reference amount (0.5 % relative
    tolerance, same unit or both convertible; the closest one when several
    do, the first of those in the grid on a tie); else the only reactant with
    1 equivalent; else None."""
    reactants = reaction.get("reactants") or []
    ones = _one_eq_indexes(reaction)
    ref = reaction.get("reference_amount")
    if isinstance(ref, dict) and isinstance(ref.get("value"), (int, float)) \
            and not isinstance(ref.get("value"), bool):
        best: tuple[float, int] | None = None
        for i in ones:
            n, n_unit = _pick(reactants[i], "n", "n_unit")
            if n is None:
                continue
            diff = _same_amount(n, n_unit, float(ref["value"]), ref.get("unit"))
            if diff is not None and diff <= 0.005 and (best is None or diff < best[0]):
                best = (diff, i)
        if best is not None:
            return best[1]
    return ones[0] if len(ones) == 1 else None


def _row_no(component: dict[str, Any], index: int) -> int:
    """The reactant's row number (its ``row``, else its 1-based position)."""
    row = component.get("row")
    return row if isinstance(row, int) and not isinstance(row, bool) else index + 1


def _shown_name(component: dict[str, Any]) -> str | None:
    """A reactant's name as stored, else its formula, else None (stripped)."""
    for key in ("name", "formula"):
        value = component.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _same_name(a: str | None, b: str | None) -> bool:
    """Equal names ignoring case and surrounding blanks (no name = no name)."""
    def fold(text: str | None) -> str | None:
        return text.strip().casefold() if isinstance(text, str) and text.strip() else None
    return fold(a) == fold(b)


def _grams_of(component: dict[str, Any]) -> float | None:
    m, m_unit = _pick(component, "m", "m_unit")
    return mass_in_grams(m, m_unit) if m is not None else None


def _candidates(reaction: dict[str, Any]) -> tuple[Candidate, ...]:
    """Every reactant of a reaction, by row number."""
    out = []
    for i, component in enumerate(reaction.get("reactants") or []):
        if not isinstance(component, dict):
            continue
        eq, _ = _pick(component, "eq")
        grams = _grams_of(component)
        out.append(Candidate(row=_row_no(component, i), name=_shown_name(component), eq=eq,
                             mass=format_mass(grams) if grams is not None else None))
    return tuple(sorted(out, key=lambda c: c.row))


@dataclass(frozen=True)
class _Limiting:
    """The starting material of one run."""

    index: int | None                       # in reaction["reactants"]
    row: int | None
    by: Literal["user", "auto"] | None
    auto_row: int | None
    ambiguous: bool
    grams: float | None


_PLAIN_NUMBER = re.compile(r"^[+\-−]?\d+(?:[.,]\d+)?$")


def _temperature(text: object) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.strip()
    return f"{t} °C" if _PLAIN_NUMBER.match(t) else t


def _reagent_texts(reaction: dict[str, Any]) -> list[Text]:
    """One text per reagent row: its name, else its formula, else nothing."""
    out = []
    for component in reaction.get("reagents") or []:
        if not isinstance(component, dict):
            continue
        name = component.get("name")
        formula = component.get("formula")
        if isinstance(name, str) and name.strip():
            out.append(Text(name.strip(), is_formula_like(name)))
        elif isinstance(formula, str) and formula.strip():
            out.append(Text(formula.strip(), True))
    return out


def _solvent_parts(reaction: dict[str, Any]) -> tuple[list[str], str | None]:
    names = [s["name"].strip() for s in reaction.get("solvents") or []
             if isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"].strip()]
    return names, _temperature(reaction.get("temperature"))


def _solvent_text(names: list[str], temperature: str | None) -> Text | None:
    """Solvents joined ", ", the temperature appended. Formula-like when a
    solvent is a formula and nothing else in the line has a digit after a
    letter (which would wrongly become a subscript)."""
    parts = names + ([temperature] if temperature else [])
    if not parts:
        return None
    formula = any(is_formula_like(n) for n in names) and not any(
        _LETTER_DIGIT.search(p) for p in parts if not is_formula_like(p))
    return Text(", ".join(parts), formula)


# --------------------------------------------------------------------------
# The builder
# --------------------------------------------------------------------------

@dataclass
class _RunInfo:
    batch: BatchRef
    reaction: dict[str, Any]
    reactants: list[Participant]
    yield_percent: float
    yield_kind: Literal["isolated", "analytical"]


@dataclass
class _Group:
    product: str                 # CORE_STEREO made
    runs: list[_RunInfo]


class _Builder:
    def __init__(self, data: RouteData):
        self.data = data
        self.notes: list[str] = []
        self.missing: dict[str, MissingReaction] = {}
        self._reactions: dict[str, dict[str, Any] | None] = {}
        self._resolved: dict[str, list[Participant]] = {}
        self._molfiles: dict[str, str | None] = {}
        self.groups: dict[str, list[_Group]] = {}
        self.history: dict[str, int] = {}
        self._visiting: set[str] = set()
        self.group_count = 0
        self._cut_depth = self._cut_steps = False

    # ---------------------------------------------------------------- notes
    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    # ---------------------------------------------------------------- data access (never raise)
    def reaction(self, ref: str) -> dict[str, Any] | None:
        if ref not in self._reactions:
            try:
                value = self.data.reaction(ref)
            except Exception:  # noqa: BLE001 - a broken record counts as not stored
                value = None
            self._reactions[ref] = value if isinstance(value, dict) else None
        return self._reactions[ref]

    def identify(self, molfile: str | None) -> str | None:
        if not isinstance(molfile, str) or not molfile.strip():
            return None
        try:
            cs = self.data.identify(molfile)
        except Exception:  # noqa: BLE001
            return None
        return cs if isinstance(cs, str) and cs else None

    def stereo_molfile(self, cs: str) -> str | None:
        if cs not in self._molfiles:
            try:
                value = self.data.stereo_molfile(cs)
            except Exception:  # noqa: BLE001
                value = None
            self._molfiles[cs] = value if isinstance(value, str) and value.strip() else None
        return self._molfiles[cs]

    def limiting_choice(self, ref: str) -> tuple[int, str | None] | None:
        """The user's starting-material choice for an experiment, or None
        (also when the data cannot tell, or answers nonsense)."""
        ask = getattr(self.data, "limiting_choice", None)
        if ask is None:
            return None
        try:
            found = ask(ref)
        except Exception:  # noqa: BLE001 - an unreadable choice means "automatic"
            return None
        if isinstance(found, (tuple, list)) and len(found) == 2 \
                and isinstance(found[0], int) and not isinstance(found[0], bool) \
                and (found[1] is None or isinstance(found[1], str)):
            return found[0], found[1]
        return None

    def limiting(self, batch: BatchRef, reaction: dict[str, Any]) -> _Limiting:
        """The starting material of a run: the user's choice when it still
        applies (else a note), else the automatic one."""
        reactants = reaction.get("reactants") or []
        auto = limiting_reactant(reaction)
        auto_row = _row_no(reactants[auto], auto) if auto is not None else None
        ambiguous = len(_one_eq_indexes(reaction)) >= 2
        index, by = (auto, "auto") if auto is not None else (None, None)
        choice = self.limiting_choice(batch.experiment_ref)
        if choice is not None:
            row, name = choice
            match = next((i for i, c in enumerate(reactants)
                          if isinstance(c, dict) and _row_no(c, i) == row), None)
            if match is not None and _same_name(_shown_name(reactants[match]), name):
                index, by = match, "user"
            else:
                where = _exp_name(batch.experiment_code, batch.experiment_ref)
                shown = f"row {row}, {name.strip()}" if name and name.strip() else f"row {row}"
                self.note(f"{where}: the starting material chosen earlier ({shown}) is no "
                          "longer in the reaction; the automatic choice is used.")
        if index is None:
            return _Limiting(None, None, None, auto_row, ambiguous, None)
        return _Limiting(index, _row_no(reactants[index], index), by, auto_row, ambiguous,
                         _grams_of(reactants[index]))

    def label(self, cs: str) -> str:
        try:
            text = self.data.stereo_label(cs)
        except Exception:  # noqa: BLE001
            text = None
        return text if isinstance(text, str) and text else cs

    def batches_of(self, cs: str) -> list[BatchRef]:
        try:
            return sorted(self.data.batches_of(cs), key=_batch_order)
        except Exception:  # noqa: BLE001
            return []

    def batches_in(self, ref: str) -> list[BatchRef]:
        try:
            return list(self.data.batches_in(ref))
        except Exception:  # noqa: BLE001
            return []

    # ---------------------------------------------------------------- participants
    def registered(self, cs: str, row_molfile: str | None, name: str | None) -> Participant:
        return Participant(key=f"cs:{cs}", molfile=self.stereo_molfile(cs) or row_molfile,
                           name=name, label=self.label(cs), core_stereo_id=cs)

    def product(self, cs: str) -> Participant:
        label = self.label(cs)
        return Participant(key=f"cs:{cs}", molfile=self.stereo_molfile(cs), name=label,
                           label=label, core_stereo_id=cs)

    def resolve(self, batch: BatchRef, reaction: dict[str, Any]) -> list[Participant]:
        """The reactants of one experiment, in grid order (cached)."""
        ref = batch.experiment_ref
        if ref in self._resolved:
            return self._resolved[ref]
        where = _exp_name(batch.experiment_code, ref)
        out = []
        for i, row in enumerate(reaction.get("reactants") or []):
            if isinstance(row, dict):
                out.append(self.resolve_row(where, ref, i, row))
        self._resolved[ref] = out
        return out

    def resolve_row(self, where: str, ref: str, index: int, row: dict[str, Any]) -> Participant:
        name = row.get("name") if isinstance(row.get("name"), str) and row["name"].strip() else None
        molfile = row.get("molfile") if isinstance(row.get("molfile"), str) \
            and row["molfile"].strip() else None
        row_no = row.get("row") if isinstance(row.get("row"), int) else index + 1
        shown = f"reactant {row_no}" + (f" ({name})" if name else "")
        cs_of_row: str | None = None
        identified = False
        source = row.get("source_experiment_ref")
        if isinstance(source, str) and source.strip():
            source = source.strip()
            source_name = _exp_name(row.get("source_experiment_code"), source)
            candidates = [b for b in self.batches_in(source) if b.core_stereo_id]
            if candidates:
                distinct = sorted({b.core_stereo_id for b in candidates})
                if molfile is not None:
                    cs_of_row, identified = self.identify(molfile), True
                    if cs_of_row in distinct:
                        return self.registered(cs_of_row, molfile, name)
                    self.note(f"{where}: {shown} is taken from {source_name} in the ELN, but no "
                              f"registered product of {source_name} has its structure; it was "
                              "identified by its structure instead.")
                elif len(distinct) == 1:
                    return self.registered(distinct[0], molfile, name)
                else:
                    self.note(f"{where}: {shown} is taken from {source_name} in the ELN, which "
                              f"has several registered products ({', '.join(distinct)}), and "
                              "the row has no structure to tell which one it is.")
        if molfile is not None:
            cs = cs_of_row if identified else self.identify(molfile)
            if cs is not None:
                return self.registered(cs, molfile, name)
            key = inchikey_of(molfile)
            if key is not None:
                return Participant(key=f"ik:{key}", molfile=molfile, name=name, label=None)
        formula = row.get("formula") if isinstance(row.get("formula"), str) \
            and row["formula"].strip() else None
        if name is not None:
            return Participant(key=f"nm:{name.casefold()}", molfile=None, name=name, label=None)
        return Participant(key=f"rw:{ref}:{row_no}", molfile=None, name=formula, label=None)

    # ---------------------------------------------------------------- walking back
    def runs_of(self, cs: str) -> list[_RunInfo]:
        runs = []
        for batch in self.batches_of(cs):
            ref = batch.experiment_ref
            where = _exp_name(batch.experiment_code, ref)
            # The yield first: a run without a yield above 0 is never drawn,
            # so its reaction is not asked for either.
            value, kind = _yield_of(batch)
            if value is None or kind is None:
                self.note(f"{where} not drawn for {self.label(cs)}: no yield above 0 is recorded.")
                continue
            reaction = self.reaction(ref)
            if reaction is None:
                if ref not in self.missing:
                    self.missing[ref] = MissingReaction(ref, batch.experiment_code,
                                                        batch.experiment_url, cs)
                continue
            reactants = self.resolve(batch, reaction)
            if any(p.key == f"cs:{cs}" for p in reactants):
                self.note(f"{where} not drawn for {self.label(cs)}: {self.label(cs)} is also one "
                          "of its reactants (recovered starting material).")
                continue
            runs.append(_RunInfo(batch, reaction, reactants, value, kind))
        return runs

    def expand(self, cs: str, depth: int) -> int:
        """Collect the steps making ``cs`` and, recursively, its reactants.
        Returns the length of its history (0: no step)."""
        if cs in self.history:
            return self.history[cs]
        if cs in self._visiting:
            self.note(f"The route loops back to {self.label(cs)}; it is not followed further "
                      "there.")
            return 0
        if depth > MAX_ROUTE_DEPTH:
            if not self._cut_depth:
                self._cut_depth = True
                self.note(f"The route was cut after {MAX_ROUTE_DEPTH} generations.")
            return 0
        self._visiting.add(cs)
        groups: list[_Group] = []
        by_key: dict[tuple[str, ...], _Group] = {}
        for run in self.runs_of(cs):
            key = tuple(sorted(p.key for p in run.reactants))
            group = by_key.get(key)
            if group is None:
                if self.group_count >= MAX_ROUTE_STEPS:
                    if not self._cut_steps:
                        self._cut_steps = True
                        self.note(f"The route was cut after {MAX_ROUTE_STEPS} steps.")
                    continue
                self.group_count += 1
                group = by_key[key] = _Group(cs, [])
                groups.append(group)
            group.runs.append(run)
        longest = 0
        for group in groups:
            for p in group.runs[0].reactants:
                if p.core_stereo_id is not None:
                    longest = max(longest, self.expand(p.core_stereo_id, depth + 1))
        self._visiting.discard(cs)
        self.groups[cs] = groups
        self.history[cs] = (longest + 1) if groups else 0
        return self.history[cs]

    # ---------------------------------------------------------------- steps
    def ordered_reactants(self, group: _Group) -> list[Participant]:
        """Grid order of the first run, the registered reactant with the
        longest own history moved to the front."""
        reactants = list(group.runs[0].reactants)
        best: int | None = None
        for i, p in enumerate(reactants):
            h = self.history.get(p.core_stereo_id, 0) if p.core_stereo_id else 0
            if h > 0 and (best is None or h > self.history[reactants[best].core_stereo_id]):
                best = i
        if best is not None:
            reactants.insert(0, reactants.pop(best))
        return reactants

    def make_step(self, group: _Group) -> Step:
        runs = sorted(group.runs, key=lambda r: _batch_order(r.batch))
        reagents = [_reagent_texts(r.reaction) for r in runs]
        solvents = [_solvent_parts(r.reaction) for r in runs]

        # Reagents, solvents and the temperature are compared separately, each
        # ignoring the runs that have none recorded.
        reagent_keys = {tuple(sorted(t.text.casefold() for t in texts))
                        for texts in reagents if texts}
        solvent_keys = {tuple(sorted(n.casefold() for n in names)) for names, _ in solvents
                        if names}
        temperature_keys = {temp.casefold() for _, temp in solvents if temp}
        reagents_agree = len(reagent_keys) <= 1
        solvents_agree = len(solvent_keys) <= 1
        temperatures_agree = len(temperature_keys) <= 1

        above: tuple[Text, ...] = ()
        if reagents_agree:
            above = tuple(next((texts for texts in reagents if texts), []))
        below: list[Text] = []
        shared_names = next((n for n, _ in solvents if n), []) if solvents_agree else []
        shared_temp = next((t for _, t in solvents if t), None) if temperatures_agree else None
        line = _solvent_text(shared_names, shared_temp)
        if line is not None:
            below.append(line)

        out_runs = []
        for run, texts, (names, temp) in zip(runs, reagents, solvents):
            own = []
            if not reagents_agree and texts:
                own.append(", ".join(t.text for t in texts))
            conditions = (names if not solvents_agree else []) \
                + ([temp] if temp and not temperatures_agree else [])
            if conditions:
                own.append(", ".join(conditions))
            b = run.batch
            chosen = self.limiting(b, run.reaction)
            sm = chosen.grams
            line = _exp_name(b.experiment_code, b.experiment_ref)
            if own:
                line += f" ({'; '.join(own)})"
            if sm is not None:
                line += f", SM = {format_mass(sm)}"
            line += f", {format_yield(run.yield_percent)} %"
            out_runs.append(Run(b.experiment_ref, b.experiment_code, b.experiment_url,
                                run.yield_percent, run.yield_kind, sm, line,
                                compound=group.product, limiting_row=chosen.row,
                                limiting_by=chosen.by, auto_row=chosen.auto_row,
                                ambiguous=chosen.ambiguous,
                                candidates=_candidates(run.reaction)))
            below.append(Text(line))
        return Step(product=self.product(group.product),
                    reactants=tuple(self.ordered_reactants(group)),
                    above=above, below=tuple(below), runs=tuple(out_runs))

    def emit(self, cs: str, done: set[str], out: list[Step]) -> None:
        """Post-order: the steps making the reactants, then the step itself."""
        if cs in done:
            return
        done.add(cs)
        for group in self.groups.get(cs, []):
            step = self.make_step(group)
            registered = [p for p in step.reactants if p.core_stereo_id is not None]
            # The longest history (first) is made last, directly before this step.
            for p in registered[1:] + registered[:1]:
                if self.groups.get(p.core_stereo_id):
                    self.emit(p.core_stereo_id, done, out)
            out.append(step)


def _yield_of(batch: BatchRef) -> tuple[float | None, Literal["isolated", "analytical"] | None]:
    """The isolated yield when above 0, else the analytical one when above 0."""
    for kind, value in (("isolated", batch.isolated), ("analytical", batch.analytical)):
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and math.isfinite(float(value)) and value > 0:
            return float(value), kind  # type: ignore[return-value]
    return None, None


def build_route(target_core_stereo_id: str, data: RouteData) -> Route:
    """Every step that led to ``target_core_stereo_id`` (see the module
    docstring for the rules), in synthesis order, the step making the target
    last. Pure and deterministic; never raises for odd data (a failing
    ``RouteData`` call counts as "nothing known")."""
    builder = _Builder(data)
    target = target_core_stereo_id.strip()
    builder.expand(target, 0)
    steps: list[Step] = []
    builder.emit(target, set(), steps)
    return Route(target=builder.product(target), steps=tuple(steps),
                 missing=tuple(builder.missing.values()), notes=tuple(builder.notes))
