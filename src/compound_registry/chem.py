"""Chemistry of the registry: turning a drawing into permanent lookup keys.

This is the only module that decides what "the same compound" means. Storage,
CLI, native host and extension all depend on :func:`normalize` and never look
at structures themselves.

The rules
---------
1. *Same compound means exactly the same compound*, with the same stereo.
2. *Salts and solvates are different compounds.* Every fragment of the drawing
   is kept with its stoichiometry; nothing is stripped. Free base, HCl salt,
   2 HCl salt and TFA salt each have their own CORE.
3. *Stereoisomers share a CORE and get different STEREO numbers*: enantiomers,
   diastereomers, E/Z isomers and atropisomers.
4. *Stereo descriptors are distinct variants.* "Absolute as drawn", "racemic",
   "relative" and "undefined" are different STEREO entries even when the 2D
   drawing is identical. V3000 enhanced stereo (ABS/AND/OR) is read when
   present; otherwise the drawing is taken as absolute. The caller may
   override the mode.
5. *Isotopically labelled compounds are different compounds* (different CORE).
6. *Drawing differences must not create new IDs*: Kekule vs aromatic, explicit
   vs implicit H, charge-separated vs pentavalent nitro, mobile-H tautomers,
   salt drawn as neutral acid + base or as ion pair. Standard InChI does this
   normalisation.

How the keys implement the rules
--------------------------------
``core_key``
    Standard InChI computed with stereo omitted (InChI option ``SNon``).
    Formula, connectivity, mobile-H, charge and isotope layers remain, so
    rules 2, 5 and 6 hold and all stereoisomers share it (rule 3).

``stereo_key``
    The *set of stereoisomers the drawing denotes*, as canonical JSON::

        [[m, ...], [m, ...], ...]
         \\______/
          one possible substance = a mixture of these members

    The outer list means "exactly one of these substances" and each inner
    list means "a mixture of these stereoisomers". A member ``m`` is the full
    standard InChI of one stereoisomer, plus an ``|atrop=`` suffix when axial
    chirality is drawn (InChI cannot encode atropisomers; see
    :func:`_atrop_axes` for how the suffix is made drawing-independent and
    which cases are refused).

    * absolute as drawn      ``[[I]]``
    * racemate               ``[[I, mirror(I)]]``
    * relative (one of two)  ``[[I], [mirror(I)]]``
    * undefined              ``[[I without the undrawn centres]]``
    * per-centre AND/OR groups: every group is inverted independently; AND
      groups multiply the members of a mixture, OR groups multiply the
      alternatives.

    Because the key is the set itself, it does not depend on which member of
    the set happened to be drawn (a racemate drawn as R or as S is one
    variant), and a "racemate" of an achiral or meso compound collapses to
    the plain compound.

``stereo_mode``
    A label derived from the key (none / abs / rac / rel / undefined /
    partial / mixed). It is stored next to the key (rule 4) but never adds
    information the key does not already have.

Stereo that is drawn but cannot be keyed
----------------------------------------
A wedge that does not end up in the key would let two stereoisomers share an
ID without anyone noticing. Such features are therefore never dropped
silently: they are listed in ``Normalized.ignored_stereo`` and the caller must
refuse to register until the user has accepted each of them. This covers a
wedge at an atom that is not a recognised stereocentre or axis (for example an
allene, a bridgehead nitrogen, a wedge on the wrong bond of a biaryl), stereo
marks in a SMILES that RDKit does not keep, and any case where the InChI
library reads stereo from the molfile that the RDKit route does not have.

Known limits (also listed in the README)
----------------------------------------
* Standard InChI merges only heteroatom mobile-H tautomers. Keto/enol and
  ring-chain tautomers are different COREs.
* Standard InChI disconnects bonds to metals, and does not describe stereo at
  metal centres, planar or helical chirality.
* Not distinguished (reported through ``ignored_stereo`` when a wedge is
  drawn, otherwise invisible): allene and spiro axial chirality, stereogenic
  nitrogen such as in Troeger's base.
* An atropisomeric axis is only recognised when it is drawn with a wedge on a
  ring bond that starts at an axis atom. Drawn flat, the axis is simply not
  part of the key. Axes whose configuration cannot be keyed safely (see
  :func:`_atrop_axes`) are refused with ``atropisomer_unsupported``.
* Abbreviations (atom aliases), polymer and mixture brackets are refused.

Everything here is pure: no I/O, no global state, same input -> same output.
"""

from __future__ import annotations

import itertools
import json
import re

import rdkit
from rdkit import Chem, rdBase
from rdkit.Chem import AllChem, rdinchi

from .errors import ChemistryError
from .models import MODE_OVERRIDES, Normalized

__all__ = ["normalize", "normalize_smiles", "MAX_STEREO_GROUPS", "KEY_ALGORITHM"]

#: Version of the key recipe in this module. Increase it whenever a change could
#: alter ``core_key``, ``stereo_key`` or ``stereo_mode`` for any input. The
#: registry stores it and refuses to issue IDs when it differs, until the owner
#: has re-checked every stored key.
KEY_ALGORITHM = 1

#: More AND/OR groups than this would need > 2**6 InChI computations; refuse.
MAX_STEREO_GROUPS = 6

_ATROP_STEREO = (Chem.BondStereo.STEREOATROPCW, Chem.BondStereo.STEREOATROPCCW)
_CHIRAL_TAGS = (Chem.ChiralType.CHI_TETRAHEDRAL_CW, Chem.ChiralType.CHI_TETRAHEDRAL_CCW)
_FLIP = {"+": "-", "-": "+"}

# InChI notices that only report the normalisation rule 6 asks for (or what the
# stereo mode already says). They are not shown as warnings.
_ROUTINE_INCHI_MESSAGES = frozenset(
    ["Omitted undefined stereo", "Proton(s) added/removed", "Charges neutralized",
     "Charges were rearranged"]
)

# Sgroup types that change what the drawing means (polymers, mixtures,
# formulations, components). They cannot be registered as one compound.
_UNSUPPORTED_SGROUPS = frozenset(
    ["SRU", "MON", "COP", "CRO", "MOD", "GRA", "MER", "ANY", "MIX", "FOR", "COM"]
)

# Atomic numbers treated as metals for the "bond to metal" warning.
_METALS = frozenset(
    [3, 4, 11, 12, 13]
    + list(range(19, 32))
    + list(range(37, 51))
    + list(range(55, 84))
    + list(range(87, 104))
)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def normalize(molfile: str, mode_override: str | None = None) -> Normalized:
    """Turn a molfile (V2000 or V3000) into registry keys.

    Returns a :class:`~compound_registry.models.Normalized`; its first five
    fields are ``(core_key, stereo_key, stereo_mode, inchikey, smiles)``.

    ``mode_override`` (rule 4) replaces whatever stereo groups the file has:

    ``"abs"``        absolute as drawn; AND/OR groups are ignored
    ``"rac"``        one AND group over every drawn centre and axis
    ``"rel"``        one OR group over every drawn centre and axis
    ``"undefined"``  wedges are ignored (double-bond geometry stays as drawn)

    Raises :class:`~compound_registry.errors.ChemistryError`.
    """
    _check_mode(mode_override)
    if not isinstance(molfile, str) or not molfile.strip():
        raise ChemistryError("No structure was given.", code="structure_empty")
    mol = _parse_molblock(molfile)
    return _normalize_mol(mol, mode_override, molblock=molfile, input_format="molfile")


def normalize_smiles(smiles: str, mode_override: str | None = None) -> Normalized:
    """Same as :func:`normalize` for a SMILES or CXSMILES string.

    Enhanced stereo is read from CXSMILES (``|a:..,&1:..,o1:..|``). The
    returned ``molblock`` is generated (2D coordinates computed by RDKit), so
    that a molfile can always be stored.
    """
    _check_mode(mode_override)
    if not isinstance(smiles, str) or not smiles.strip():
        raise ChemistryError("No structure was given.", code="structure_empty")
    with rdBase.BlockLogs():
        try:
            mol = Chem.MolFromSmiles(smiles.strip())
        except Exception:  # RDKit raises on a few malformed inputs
            mol = None
    if mol is None:
        raise ChemistryError(
            "The SMILES could not be parsed.", code="structure_parse_failed"
        )
    _check_atoms(mol)
    return _normalize_mol(
        mol,
        mode_override,
        molblock=_generate_molblock(mol),
        input_format="smiles",
        source_smiles=smiles.strip(),
    )


# --------------------------------------------------------------------------
# Parsing and validation
# --------------------------------------------------------------------------

def _check_mode(mode_override: str | None) -> None:
    if mode_override is not None and mode_override not in MODE_OVERRIDES:
        raise ChemistryError(
            f"Unknown stereo mode {mode_override!r}; use one of {', '.join(MODE_OVERRIDES)}.",
            code="invalid_mode",
        )


def _parse_molblock(molfile: str) -> Chem.Mol:
    """Parse and sanitise a molfile, keeping every fragment (rule 2).

    Sanitisation is what makes rule 6 work on the RDKit side: it converts
    pentavalent nitro groups to the charge-separated form and perceives
    aromaticity, so Kekule and aromatic drawings become the same molecule.
    Explicit hydrogens are folded into their heavy atom unless they carry
    information (isotope labels such as D/T are kept: rule 5).
    """
    text = molfile.replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith("$RXN"):
        raise ChemistryError(
            "This is a reaction file. Give the molfile of one product.",
            code="structure_parse_failed",
        )
    candidates = [text]
    lines = text.split("\n")
    # Some tools drop the three header lines; put the counts line back on line 4.
    for i, line in enumerate(lines[:4]):
        if line.rstrip().endswith(("V2000", "V3000")) and i != 3:
            candidates.append("\n".join([""] * (3 - i) + lines) if i < 3 else text)
            break
    mol = None
    with rdBase.BlockLogs():
        for candidate in candidates:
            try:
                mol = Chem.MolFromMolBlock(candidate, sanitize=True, removeHs=True, strictParsing=False)
            except Exception:
                mol = None
            if mol is not None:
                break
    if mol is None:
        raise ChemistryError(
            "The structure could not be parsed (invalid molfile or impossible valence).",
            code="structure_parse_failed",
        )
    after_end = text.split("$$$$", 1)[1] if "$$$$" in text else ""
    if after_end.strip():
        raise ChemistryError(
            "The file contains several structures. Give one molfile at a time.",
            code="structure_parse_failed",
        )
    _check_atoms(mol)
    return mol


def _check_atoms(mol: Chem.Mol) -> None:
    """Refuse drawings that do not describe one fully specified compound."""
    if mol.GetNumAtoms() == 0:
        raise ChemistryError("The structure is empty.", code="structure_empty")
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() == 0 or atom.HasQuery():
            raise ChemistryError(
                "The structure contains a placeholder (R group, '*', abbreviation or "
                "query atom). Draw the full structure.",
                code="structure_has_placeholders",
            )
        if atom.HasProp("molFileAlias") and atom.GetProp("molFileAlias").strip():
            # The file shows a label (e.g. "Boc") on top of a different atom.
            raise ChemistryError(
                f"Atom {atom.GetIdx() + 1} carries the label "
                f"{atom.GetProp('molFileAlias').strip()!r} instead of a full structure. "
                "Expand the abbreviation.",
                code="structure_has_placeholders",
            )
    for sgroup in Chem.GetMolSubstanceGroups(mol):
        kind = sgroup.GetProp("TYPE") if sgroup.HasProp("TYPE") else ""
        if kind in _UNSUPPORTED_SGROUPS:
            raise ChemistryError(
                f"The structure contains a {kind} bracket (polymer or mixture notation), "
                "which cannot be registered as one compound.",
                code="structure_unsupported",
            )


def _generate_molblock(mol: Chem.Mol) -> str:
    """A molfile for a molecule that came in as SMILES."""
    copy = Chem.Mol(mol)
    with rdBase.BlockLogs():
        AllChem.Compute2DCoords(copy)
        has_groups = len(copy.GetStereoGroups()) > 0
        return Chem.MolToMolBlock(copy, forceV3000=has_groups)


# --------------------------------------------------------------------------
# InChI helpers
# --------------------------------------------------------------------------

def _inchi(mol: Chem.Mol, options: str = "") -> tuple[str, str, str]:
    """(inchi, message, auxinfo) from the InChI library; raises if it fails."""
    with rdBase.BlockLogs():
        try:
            inchi, _retcode, message, _log, aux = rdinchi.MolToInchi(mol, options=options)
        except Exception as exc:
            raise ChemistryError(f"InChI generation failed: {exc}", code="inchi_failed") from exc
    if not inchi:
        raise ChemistryError(
            f"InChI generation failed: {message or 'no InChI returned'}", code="inchi_failed"
        )
    return inchi, message or "", aux or ""


def _stereo_flags(inchi: str) -> tuple[bool, bool]:
    """(has_defined, has_undefined) from the /t and /b layers of an InChI.

    Entries end in ``+`` or ``-`` when defined and in ``?`` when undefined,
    e.g. ``/t3-,4?`` or ``/b3-2+``.
    """
    defined = undefined = False
    for layer in inchi.split("/")[1:]:
        if not layer or layer[0] not in "tb":
            continue
        for entry in re.split(r"[,;]", layer[1:]):
            entry = entry.strip()
            if not entry:
                continue
            if entry.endswith("?"):
                undefined = True
            elif entry[-1] in "+-":
                defined = True
    return defined, undefined


def _atom_ranks(fragment: Chem.Mol) -> tuple[str, dict[int, tuple[int, int]] | None]:
    """(core InChI, {atom index: (component, rank)}) for one drawn fragment.

    ``rank`` is the smallest InChI canonical number in the atom's class of
    constitutionally equivalent atoms (AuxInfo layers ``/N:`` and ``/E:`` of
    the stereo-free InChI). Canonical numbers of equivalent atoms depend on
    the order in which the atoms were drawn; the class minimum does not, and
    it is the same for every tautomer InChI merges. Returns ``None`` for the
    ranks when the AuxInfo cannot be interpreted.
    """
    inchi, _message, aux = _inchi(fragment, "/SNon")
    numbers = re.search(r"/N:([0-9,;]+)", aux)
    if not numbers:
        return inchi, None
    equivalence = re.search(r"/E:([^/]*)", aux)
    eq_components = equivalence.group(1).split(";") if equivalence else []
    if any("*" in comp for comp in eq_components):
        return inchi, None  # abbreviated layer (repeated components inside one fragment)
    ranks: dict[int, tuple[int, int]] = {}
    for comp_no, comp in enumerate(numbers.group(1).split(";"), start=1):
        class_min: dict[int, int] = {}
        if comp_no <= len(eq_components):
            for cls in re.findall(r"\(([0-9,]+)\)", eq_components[comp_no - 1]):
                members = [int(x) for x in cls.split(",") if x]
                for member in members:
                    class_min[member] = min(members)
        for canon_no, original in enumerate(comp.split(","), start=1):
            if original:
                ranks[int(original) - 1] = (comp_no, class_min.get(canon_no, canon_no))
    return inchi, ranks


# --------------------------------------------------------------------------
# Atropisomers (rule 3; not representable in InChI)
# --------------------------------------------------------------------------

def _unsupported_axis(reason: str) -> ChemistryError:
    return ChemistryError(
        f"This drawing has a chiral axis that the registry cannot identify safely: {reason} "
        "Remove the wedge from the axis to register the compound without axial stereo, "
        "or ask for this case to be supported.",
        code="atropisomer_unsupported",
    )


def _atrop_axes(mol: Chem.Mol, centres: frozenset) -> tuple[dict[int, tuple[str, str]], list[str]]:
    """Axes drawn with a wedge: ({bond index: (axis id, '+' or '-')}, warnings).

    RDKit perceives the axis from a wedge on a bond that starts at an axis
    atom and stores the sense of twist relative to the lowest-numbered
    neighbour at each end. That depends on the atom numbering of the drawing,
    so it is re-expressed here in terms that do not:

    * the reference neighbour at each end is the one with the lowest InChI
      rank (see :func:`_atom_ranks`), so the sign is the same for every
      numbering, orientation and tautomer of one atropisomer and opposite for
      its mirror image;
    * the axis is named by the ranks of its two atoms and by its fragment,
      where fragments are numbered by their sorted core InChI.

    Cases that cannot be keyed without risking a wrong ID are refused
    (``atropisomer_unsupported``) rather than guessed:

    * the two neighbours at one end have the same rank (identical by
      constitution) while the drawing has stereocentres or other axes: the
      axis could be pseudo-asymmetric;
    * several axes share the same name (symmetry-equivalent axes or identical
      fragments), unless there are exactly two of them and no stereocentre:
      then the unordered pair of signs describes the compound completely
      (``+,+`` / ``-,-`` / ``+,-``).

    A wedged axis whose end is symmetric in an otherwise stereo-free drawing
    is not stereogenic and is ignored with a warning.
    """
    bonds = [b for b in mol.GetBonds() if b.GetStereo() in _ATROP_STEREO]
    if not bonds:
        return {}, []

    mapping: list[tuple[int, ...]] = []
    fragments = Chem.GetMolFrags(mol, asMols=True, sanitizeFrags=False, fragsMolAtomMapping=mapping)
    located = {orig: (f_no, local) for f_no, atoms in enumerate(mapping) for local, orig in enumerate(atoms)}
    info = [_atom_ranks(fragment) for fragment in fragments]
    signature_no = {sig: n for n, sig in enumerate(sorted({sig for sig, _ in info}), start=1)}

    def fmt(rank: tuple[int, int]) -> str:
        return str(rank[1]) if rank[0] == 1 else f"{rank[0]}.{rank[1]}"

    axes: dict[int, tuple[str, str]] = {}
    warnings: list[str] = []
    for bond in bonds:
        begin, end = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        f_no = located[begin][0]
        signature, ranks = info[f_no]
        if ranks is None:
            raise _unsupported_axis("InChI gave no usable atom numbering for this fragment.")
        sign = 1 if bond.GetStereo() == Chem.BondStereo.STEREOATROPCCW else -1
        symmetric = False
        usable = True
        for atom, other in ((begin, end), (end, begin)):
            neighbours = [n.GetIdx() for n in mol.GetAtomWithIdx(atom).GetNeighbors() if n.GetIdx() != other]
            if not neighbours:
                usable = False
                break
            ranked = []
            for n in neighbours:
                rank = ranks.get(located[n][1])
                if rank is None:
                    raise _unsupported_axis("a neighbour of the axis has no InChI atom number.")
                ranked.append((rank, n))
            ranked.sort()
            if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
                symmetric = True
            if ranked[0][1] != min(neighbours):  # RDKit's reference is the lowest index
                sign = -sign
        if not usable or symmetric:
            if usable and (centres or len(bonds) > 1):
                raise _unsupported_axis(
                    "the two groups next to the axis on one ring are identical by constitution, "
                    "and the molecule has other stereo elements."
                )
            warnings.append(
                "A wedge is drawn at a single bond that is not a stereogenic axis; it was ignored."
            )
            continue
        rank_a, rank_b = ranks.get(located[begin][1]), ranks.get(located[end][1])
        if rank_a is None or rank_b is None:
            raise _unsupported_axis("an axis atom has no InChI atom number.")
        lo, hi = sorted((rank_a, rank_b))
        axes[bond.GetIdx()] = (f"{signature_no[signature]}:{fmt(lo)}-{fmt(hi)}", "+" if sign > 0 else "-")

    names = [name for name, _sign in axes.values()]
    if len(set(names)) != len(names) and not (len(names) == 2 and not centres):
        raise _unsupported_axis(
            "several axes are equivalent by symmetry (or sit in identical fragments) and their "
            "relative arrangement cannot be described."
        )
    return axes, warnings


# --------------------------------------------------------------------------
# The isomer set
# --------------------------------------------------------------------------

class _Drawing:
    """A parsed structure plus what is needed to enumerate its stereoisomers."""

    def __init__(self, mol: Chem.Mol):
        self.mol = mol
        self.centres = frozenset(
            a.GetIdx() for a in mol.GetAtoms() if a.GetChiralTag() in _CHIRAL_TAGS
        )
        self.axes, self.warnings = _atrop_axes(mol, self.centres)
        self._cache: dict[tuple[frozenset, frozenset, bool], tuple[str, str]] = {}

    def member(
        self,
        invert_atoms: frozenset = frozenset(),
        invert_axes: frozenset = frozenset(),
        drop_wedges: bool = False,
    ) -> tuple[str, str]:
        """(member key, InChI message) of one stereoisomer.

        ``invert_atoms`` / ``invert_axes`` are mirrored relative to the
        drawing. ``drop_wedges`` removes every wedge-defined element.
        """
        cache_key = (invert_atoms, invert_axes, drop_wedges)
        if cache_key in self._cache:
            return self._cache[cache_key]
        mol = Chem.Mol(self.mol)
        if drop_wedges:
            for atom in mol.GetAtoms():
                atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
        else:
            for idx in invert_atoms & self.centres:
                mol.GetAtomWithIdx(idx).InvertChirality()
        inchi, message, _aux = _inchi(mol)
        key = inchi
        if self.axes and not drop_wedges:
            parts = sorted(
                f"{axis}{_FLIP[label] if bond_idx in invert_axes else label}"
                for bond_idx, (axis, label) in self.axes.items()
            )
            key += "|atrop=" + ",".join(parts)
        self._cache[cache_key] = (key, message)
        return self._cache[cache_key]

    def mirror(self) -> str:
        """Member key of the complete mirror image of the drawing."""
        return self.member(self.centres, frozenset(self.axes))[0]


def _groups_for(drawing: _Drawing, mode_override: str | None):
    """The AND and OR groups that apply: [(atoms, axis bonds)], [(atoms, axis bonds)]."""
    all_axes = frozenset(drawing.axes)
    if mode_override in ("abs", "undefined"):
        return [], []
    if mode_override == "rac":
        return [(drawing.centres, all_axes)], []
    if mode_override == "rel":
        return [], [(drawing.centres, all_axes)]
    and_groups, or_groups = [], []
    seen_atoms: set[int] = set()
    seen_bonds: set[int] = set()
    for group in drawing.mol.GetStereoGroups():
        atoms = frozenset(a.GetIdx() for a in group.GetAtoms())
        all_bonds = frozenset(b.GetIdx() for b in group.GetBonds())
        if atoms & seen_atoms or all_bonds & seen_bonds:
            raise ChemistryError(
                "A stereocentre is listed in more than one stereo group (ABS/AND/OR); "
                "the annotation contradicts itself.",
                code="invalid_stereo_groups",
            )
        seen_atoms |= atoms
        seen_bonds |= all_bonds
        bonds = all_bonds & all_axes
        kind = group.GetGroupType()
        if kind == Chem.StereoGroupType.STEREO_AND:
            and_groups.append((atoms, bonds))
        elif kind == Chem.StereoGroupType.STEREO_OR:
            or_groups.append((atoms, bonds))
        # STEREO_ABSOLUTE: as drawn, nothing to enumerate
    return and_groups, or_groups


def _isomer_set(drawing: _Drawing, mode_override: str | None) -> tuple[list[list[str]], str, str]:
    """(nested key structure, stereo mode, InChI message of the drawn member)."""
    drop = mode_override == "undefined"
    drawn_key, message = drawing.member(drop_wedges=drop)
    and_groups, or_groups = _groups_for(drawing, mode_override)
    if len(and_groups) + len(or_groups) > MAX_STEREO_GROUPS:
        raise ChemistryError(
            f"The structure has more than {MAX_STEREO_GROUPS} AND/OR stereo groups; "
            "simplify the stereo annotation.",
            code="too_many_stereo_groups",
        )

    def combine(groups, mask):
        atoms: frozenset = frozenset()
        axes: frozenset = frozenset()
        for (g_atoms, g_axes), selected in zip(groups, mask):
            if selected:
                atoms ^= g_atoms
                axes ^= g_axes
        return atoms, axes

    substances = set()
    for or_mask in itertools.product((False, True), repeat=len(or_groups)):
        or_atoms, or_axes = combine(or_groups, or_mask)
        mixture = set()
        for and_mask in itertools.product((False, True), repeat=len(and_groups)):
            and_atoms, and_axes = combine(and_groups, and_mask)
            mixture.add(drawing.member(or_atoms ^ and_atoms, or_axes ^ and_axes, drop)[0])
        substances.add(tuple(sorted(mixture)))
    structure = [list(mixture) for mixture in sorted(substances)]
    return structure, _mode_label(structure, drawing, drawn_key, message, drop), message


def _mode_label(structure, drawing: _Drawing, drawn_key: str, message: str, dropped: bool) -> str:
    """Derive the stereo mode from the isomer set (see module docstring)."""
    inchi = drawn_key.split("|atrop=")[0]
    defined, undefined = _stereo_flags(inchi)
    if "Omitted undefined stereo" in message:
        undefined = True
    if drawing.axes and not dropped:
        defined = True
    if len(structure) == 1 and len(structure[0]) == 1:
        if defined and undefined:
            return "partial"
        if defined:
            return "abs"
        if undefined:
            return "undefined"
        return "none"
    mirror = drawing.mirror()
    pair = sorted((drawn_key, mirror))
    if not undefined and drawn_key != mirror:
        if structure == [pair]:
            return "rac"
        if structure == [[pair[0]], [pair[1]]]:
            return "rel"
    return "mixed"


# --------------------------------------------------------------------------
# Drawn stereo that is not in the key
# --------------------------------------------------------------------------

def _strip_stereo_layers(inchi: str) -> str:
    return "/".join(layer for layer in inchi.split("/") if not layer or layer[0] not in "tbms")


def _align_atoms(with_h: Chem.Mol, without_h: Chem.Mol) -> dict[int, int] | None:
    """Map atom indices of the molfile as written (explicit H kept) to the
    working molecule (explicit H folded in). Removing hydrogens keeps the
    order of the remaining atoms, so one pass over both lists is enough."""
    if not with_h.GetNumConformers() or not without_h.GetNumConformers():
        return None
    pos_h, pos = with_h.GetConformer(), without_h.GetConformer()
    mapping: dict[int, int] = {}
    j, total = 0, without_h.GetNumAtoms()
    for atom in with_h.GetAtoms():
        if j >= total:
            break
        other = without_h.GetAtomWithIdx(j)
        a, b = pos_h.GetAtomPosition(atom.GetIdx()), pos.GetAtomPosition(j)
        if (
            atom.GetAtomicNum() == other.GetAtomicNum()
            and atom.GetIsotope() == other.GetIsotope()
            and abs(a.x - b.x) < 1e-4
            and abs(a.y - b.y) < 1e-4
            and abs(a.z - b.z) < 1e-4
        ):
            mapping[atom.GetIdx()] = j
            j += 1
    return mapping if j == total else None


def _wedge_kind(bond: Chem.Bond) -> str:
    """'wedge', 'hashed' or '' from the molfile's own bond annotation."""
    if bond.HasProp("_MolFileBondStereo"):
        return {1: "wedge", 6: "hashed"}.get(bond.GetIntProp("_MolFileBondStereo"), "")
    if bond.HasProp("_MolFileBondCfg"):
        return {1: "wedge", 3: "hashed"}.get(bond.GetIntProp("_MolFileBondCfg"), "")
    return ""


def _unused_wedges(molblock: str, drawing: "_Drawing") -> list[str]:
    """Wedge/hash bonds whose narrow end is not at a keyed centre or axis."""
    text = molblock.replace("\r\n", "\n").replace("\r", "\n")
    with rdBase.BlockLogs():
        try:
            as_written = Chem.MolFromMolBlock(text, sanitize=True, removeHs=False, strictParsing=False)
        except Exception:
            as_written = None
    mapping = _align_atoms(as_written, drawing.mol) if as_written is not None else None
    if mapping is None:  # fall back to the bonds that survived hydrogen removal
        as_written, mapping = drawing.mol, {i: i for i in range(drawing.mol.GetNumAtoms())}
    keyed = set(drawing.centres)
    for bond_idx in drawing.axes:
        bond = drawing.mol.GetBondWithIdx(bond_idx)
        keyed.update((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()))
    found = []
    for bond in as_written.GetBonds():
        kind = _wedge_kind(bond)
        if not kind:
            continue
        begin = bond.GetBeginAtom()
        if mapping.get(begin.GetIdx()) in keyed:
            continue
        found.append(
            f"The {kind} bond starting at atom {begin.GetIdx() + 1} ({begin.GetSymbol()}) does not "
            "define a stereocentre or axis that the registry can record."
        )
    return found


def _ignored_stereo(
    drawing: "_Drawing", drawn_inchi: str, molblock: str, input_format: str, source_smiles: str | None
) -> tuple[list[str], list[str]]:
    """(ignored stereo features, plain warnings). See the module docstring."""
    ignored: list[str] = []
    warnings: list[str] = []
    if input_format == "molfile":
        ignored.extend(_unused_wedges(molblock, drawing))
        native = _native_inchi(molblock)
        if native and native != drawn_inchi:
            if _strip_stereo_layers(native) == _strip_stereo_layers(drawn_inchi):
                ignored.append(
                    "The InChI library reads stereo from this drawing that differs from what is "
                    f"recorded (InChI: {_stereo_text(native)}; recorded: {_stereo_text(drawn_inchi)})."
                )
            else:
                warnings.append(
                    "RDKit and the InChI library read this drawing as different structures. "
                    "Check the thumbnail before registering."
                )
    elif source_smiles:
        core = source_smiles.split()[0].split("|")[0]
        marks = len(re.findall(r"\[[^\]]*@[^\]]*\]", core))
        if marks > len(drawing.centres):
            ignored.append(
                f"{marks - len(drawing.centres)} stereo mark(s) (@) in the SMILES are not at a "
                "stereocentre that the registry can record."
            )
    return list(dict.fromkeys(ignored)), warnings


def _stereo_text(inchi: str) -> str:
    layers = [layer for layer in inchi.split("/")[1:] if layer and layer[0] in "tbm"]
    return "/" + "/".join(layers) if layers else "none"


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def _normalize_mol(
    mol: Chem.Mol,
    mode_override: str | None,
    *,
    molblock: str,
    input_format: str,
    source_smiles: str | None = None,
) -> Normalized:
    warnings: list[str] = []

    # CORE: standard InChI without any stereo layer (rules 2, 3, 5, 6).
    core_key, _core_msg, _core_aux = _inchi(mol, "/SNon")
    if not core_key.startswith("InChI=1S/"):
        raise ChemistryError(
            "InChI did not return a standard InChI for this structure.", code="inchi_failed"
        )

    drawing = _Drawing(mol)
    warnings.extend(drawing.warnings)

    structure, mode, message = _isomer_set(drawing, mode_override)
    if mode_override is None:
        detected = mode
    else:
        detected = _isomer_set(drawing, None)[1]

    # As drawn (no override): for display, search and the consistency check.
    drawn_inchi = drawing.member()[0].split("|atrop=")[0]
    with rdBase.BlockLogs():
        inchikey = rdinchi.InchiToInchiKey(drawn_inchi)
        core_inchikey = rdinchi.InchiToInchiKey(core_key)

    for part in (p.strip() for p in message.split(";")):
        if part and part not in _ROUTINE_INCHI_MESSAGES:
            warnings.append(f"InChI: {part}")

    if any(a.GetAtomicNum() in _METALS and a.GetDegree() > 0 for a in mol.GetAtoms()):
        warnings.append(
            "The structure has bonds to a metal. InChI disconnects them, so coordination "
            "isomers and stereo at the metal are not distinguished."
        )

    # Drawn stereo that is not in the key must never be dropped silently. With
    # the "undefined" override the user has already said that no wedge counts.
    ignored: list[str] = []
    if mode_override != "undefined":
        ignored, extra = _ignored_stereo(drawing, drawn_inchi, molblock, input_format, source_smiles)
        warnings.extend(extra)

    return Normalized(
        core_key=core_key,
        stereo_key=json.dumps(structure, separators=(",", ":"), ensure_ascii=True),
        stereo_mode=mode,
        inchikey=inchikey,
        smiles=_display_smiles(mol),
        inchi=drawn_inchi,
        core_inchikey=core_inchikey,
        formula=core_key.split("/")[1] if "/" in core_key else "",
        detected_mode=detected,
        mode_overridden=mode_override is not None,
        molblock=molblock,
        input_format=input_format,
        warnings=tuple(dict.fromkeys(warnings)),
        ignored_stereo=tuple(ignored),
        rdkit_version=rdkit.__version__,
        inchi_version=_inchi_version(),
    )


def _native_inchi(molblock: str) -> str:
    """InChI computed by the InChI library reading the molfile itself.

    Used only as a second opinion on stereo perception. Empty when InChI
    cannot read the file (for example some V3000 features).
    """
    text = molblock.replace("\r\n", "\n").replace("\r", "\n")
    with rdBase.BlockLogs():
        try:
            return rdinchi.MolBlockToInchi(text, options="")[0] or ""
        except Exception:
            return ""


def _display_smiles(mol: Chem.Mol) -> str:
    """Canonical isomeric CXSMILES without coordinates. Display only."""
    copy = Chem.Mol(mol)
    copy.RemoveAllConformers()
    with rdBase.BlockLogs():
        try:
            return Chem.MolToCXSmiles(copy)
        except Exception:
            return Chem.MolToSmiles(copy)


def _inchi_version() -> str:
    try:
        return str(Chem.GetInchiVersion())
    except Exception:
        return ""
