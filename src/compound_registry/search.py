"""Matching for the compound list (``RegistryService.compounds``).

The rules are specified in :mod:`compound_registry.protocol` under "Compound
search"; this module implements them over a :class:`Snapshot` of the registry
(every CORE with its stereo variants, its batches without structures, the
stored names). It decides nothing about chemistry: "same constitution" is the
registry's own core key, reached through the service's normalisation and the
repository's ``lookup``; the substructure test is RDKit's ``HasSubstructMatch``
without chirality.

A text or query structure that cannot be used as asked is never an error: it
adds a plain-words entry to ``notes`` and matches nothing by that route.

Each match is recorded as a :class:`Hit` per CORE: the reasons (values of
``protocol.MATCH_REASONS``) and the stereo numbers of the variants that
matched THEMSELVES (their own ID, InChIKey, name, stored structure or
experiment). A match of the CORE as a whole (its number, its formula, its
constitution) adds a reason but no variant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from rdkit import Chem, rdBase
from rdkit.Chem import rdDepictor

from .errors import ChemistryError, NotFoundError
from .idformat import IdFormat
from .models import CoreRecord, LookupResult, NameRecord, Normalized, StereoRecord
from .properties import compute_properties, formula_counts, parse_formula, read_molblock

__all__ = ["CoreEntry", "Hit", "Snapshot", "match_text", "match_drawn", "read_query",
           "variant_matched"]

_INCHIKEY_RE = re.compile(r"[A-Z]{14}-[A-Z]{10}-[A-Z]")
_INCHIKEY_BLOCK_RE = re.compile(r"[A-Z]{14}")

#: ``normalize(molfile=..., smiles=...)`` -> Normalized (raises ChemistryError).
Normalize = Callable[..., Normalized]
#: ``lookup(norm)`` -> LookupResult (read-only).
Lookup = Callable[[Normalized], LookupResult]
#: ``molfiles(core_nos)`` -> {core_stereo_id: stored molfile}.
Molfiles = Callable[[list[int]], dict[str, str]]


@dataclass
class CoreEntry:
    core: CoreRecord
    variants: list[StereoRecord] = field(default_factory=list)       # by stereo number
    batches: list[dict[str, Any]] = field(default_factory=list)      # oldest first


@dataclass
class Snapshot:
    """What the search looks at. ``entries`` is ordered by CORE number."""

    entries: dict[int, CoreEntry]
    names: dict[str, NameRecord]
    id_format: IdFormat
    normalize: Normalize
    lookup: Lookup
    molfiles: Molfiles
    # core_stereo_id -> stored molfile, filled on demand (shared with the caller)
    molfile_cache: dict[str, str] = field(default_factory=dict)

    def load_molfiles(self, core_nos: list[int]) -> None:
        missing = [no for no in core_nos
                   if any(v.core_stereo_id not in self.molfile_cache
                          for v in self.entries[no].variants)]
        if missing:
            self.molfile_cache.update(self.molfiles(missing))


@dataclass
class Hit:
    reasons: set[str] = field(default_factory=set)
    variants: set[int] = field(default_factory=set)


Hits = dict[int, Hit]


def _add(hits: Hits, core_no: int, reason: str, stereo_nos: tuple[int, ...] | set[int] = ()) -> None:
    hit = hits.setdefault(core_no, Hit())
    hit.reasons.add(reason)
    hit.variants.update(stereo_nos)


def variant_matched(stereo_no: int, components: list[Hit]) -> bool:
    """``variants[].matched``: this variant matched itself in at least one part
    of the search, and no part of the search (text, drawn structure) matched
    only OTHER variants of the CORE. Without a search: always true."""
    if not components:
        return True
    own = [stereo_no in hit.variants for hit in components]
    return any(own) and all(o or not hit.variants for o, hit in zip(own, components))


# --------------------------------------------------------------------------- text

def match_text(text: str, snap: Snapshot, notes: list[str]) -> Hits:
    """CORE number -> Hit for every CORE the text matches (any reason)."""
    hits: Hits = {}
    _match_id(text, snap, hits, notes)
    _match_inchikey(text, snap, hits, notes)
    smiles_read = _match_structure_text(text, snap, hits, notes)
    _match_name(text, snap, hits)
    _match_formula(text, snap, hits, notes, smiles_read)
    _match_experiment(text, snap, hits)
    return hits


def _match_id(text: str, snap: Snapshot, hits: Hits, notes: list[str]) -> None:
    try:
        core_no, stereo_no, batch_no = snap.id_format.parse_partial(text)
    except NotFoundError:
        return
    entry = snap.entries.get(core_no)
    found = False
    if entry is not None:
        if stereo_no is None:
            _add(hits, core_no, "id")
            found = True
        elif any(v.stereo_no == stereo_no for v in entry.variants):
            if batch_no is None or any(b["stereo_no"] == stereo_no and b["batch_no"] == batch_no
                                       for b in entry.batches):
                _add(hits, core_no, "id", (stereo_no,))
                found = True
    if found:
        notes.append(f"Read as an ID of CORE {snap.id_format.format_core(core_no)}.")
    else:
        notes.append("Read as an ID, but nothing with this ID is registered.")


def _match_inchikey(text: str, snap: Snapshot, hits: Hits, notes: list[str]) -> None:
    upper = text.upper()
    if _INCHIKEY_RE.fullmatch(upper):
        def same(key: str) -> bool:
            return key == upper
        notes.append("Read as an InChIKey.")
    elif _INCHIKEY_BLOCK_RE.fullmatch(text):     # a first block only when typed in capitals
        def same(key: str) -> bool:
            return key[:14] == text
        notes.append("Read as the first block of an InChIKey (the constitution; stereo is "
                     "ignored).")
    else:
        return
    for core_no, entry in snap.entries.items():
        own = {v.stereo_no for v in entry.variants if same(v.inchikey)}
        if own or same(entry.core.inchikey):
            _add(hits, core_no, "inchikey", own)


def _match_structure_text(text: str, snap: Snapshot, hits: Hits, notes: list[str]) -> bool:
    """InChI or SMILES with the same constitution. Returns True when the text
    was read as SMILES (or as an InChI)."""
    if text.startswith("InChI="):
        molblock = _inchi_to_molblock(text)
        if molblock is None:
            notes.append("The text starts with 'InChI=' but could not be read as an InChI.")
            return False
        try:
            norm = snap.normalize(molfile=molblock, smiles=None)
        except ChemistryError as exc:
            notes.append(f"Read as an InChI, but it cannot be compared: {exc.message}")
            return True
        notes.append("Read as an InChI: compounds with the same constitution are listed "
                     "(stereo is ignored).")
    else:
        if not _parses_as_smiles(text):
            return False
        try:
            norm = snap.normalize(molfile=None, smiles=text)
        except ChemistryError as exc:
            notes.append(f"Read as SMILES, but it cannot be compared: {exc.message}")
            return True
        notes.append(f"Read as SMILES ({norm.smiles}): compounds with the same constitution "
                     "are listed (stereo is ignored).")
    _add_lookup(snap, norm, "structure", hits)
    return True


def _add_lookup(snap: Snapshot, norm: Normalized, reason: str, hits: Hits) -> None:
    found = snap.lookup(norm)
    if found.core is not None and found.core.core_no in snap.entries:
        own = (found.stereo.stereo_no,) if found.stereo is not None else ()
        _add(hits, found.core.core_no, reason, own)


def _inchi_to_molblock(text: str) -> str | None:
    """A 2D molfile for an InChI (through RDKit), or None."""
    try:
        with rdBase.BlockLogs():
            mol = Chem.MolFromInchi(text, sanitize=True, removeHs=True, logLevel=None,
                                    treatWarningAsError=False)
            if mol is None or mol.GetNumAtoms() == 0:
                return None
            rdDepictor.Compute2DCoords(mol)
            return Chem.MolToMolBlock(mol)
    except Exception:  # noqa: BLE001 - an unreadable search text is not an error
        return None


def _parses_as_smiles(text: str) -> bool:
    try:
        with rdBase.BlockLogs():
            mol = Chem.MolFromSmiles(text)
    except Exception:  # noqa: BLE001
        return False
    return mol is not None and mol.GetNumAtoms() > 0


def _match_name(text: str, snap: Snapshot, hits: Hits) -> None:
    folded = text.casefold()
    owners = _variant_owners(snap)
    for core_stereo_id, record in snap.names.items():
        owner = owners.get(core_stereo_id)
        if owner is not None and folded in record.text.casefold():
            _add(hits, owner[0], "name", (owner[1],))


def _variant_owners(snap: Snapshot) -> dict[str, tuple[int, int]]:
    return {v.core_stereo_id: (core_no, v.stereo_no)
            for core_no, entry in snap.entries.items() for v in entry.variants}


def _match_formula(text: str, snap: Snapshot, hits: Hits, notes: list[str],
                   smiles_read: bool) -> None:
    """The stored (per-fragment) formula as written, or the sum formula of
    ``properties`` compared by element counts and net charge."""
    parsed = parse_formula(text)
    if parsed is None:
        return
    compact = "".join(text.split())
    counts, charge = parsed
    heavy = {el: n for el, n in counts.items() if el != "H"}
    candidates: list[int] = []
    for core_no, entry in snap.entries.items():
        if entry.core.formula == compact:
            _add(hits, core_no, "formula")
            continue
        # Cheap preselection on the heavy atoms of the stored formula (InChI
        # never changes those), then the exact sum formula of the structure.
        stored = formula_counts(entry.core.formula)
        if stored is not None and {el: n for el, n in stored.items() if el != "H"} == heavy:
            candidates.append(core_no)
    if candidates:
        snap.load_molfiles(candidates)
        for core_no in candidates:
            first = snap.entries[core_no].variants[0]
            props = compute_properties(snap.molfile_cache.get(first.core_stereo_id, ""))
            if props is not None and parse_formula(props["sum_formula"]) == (counts, charge):
                _add(hits, core_no, "formula")
    if not smiles_read:
        notes.append("Read as a formula.")


def _match_experiment(text: str, snap: Snapshot, hits: Hits) -> None:
    folded = text.casefold()
    for core_no, entry in snap.entries.items():
        own = {b["stereo_no"] for b in entry.batches
               if b["experiment_ref"] == text
               or (b["experiment_code"] and folded in b["experiment_code"].casefold())}
        if own:
            _add(hits, core_no, "experiment", own)


# --------------------------------------------------------------------------- drawn structure

def read_query(molfile: str | None, smiles: str | None) -> Chem.Mol | None:
    """The query structure with its explicit hydrogens removed, or None when
    it cannot be read. Query atoms and bonds of a molfile are kept. Quiet."""
    try:
        with rdBase.BlockLogs():
            if molfile is not None:
                mol = read_molblock(molfile, remove_hs=False)
                if mol is None:
                    text = molfile.replace("\r\n", "\n").replace("\r", "\n")
                    mol = Chem.MolFromMolBlock(text, sanitize=False, removeHs=False,
                                               strictParsing=False)
                    if mol is not None:
                        mol.UpdatePropertyCache(strict=False)
                        Chem.FastFindRings(mol)
            else:
                mol = Chem.MolFromSmiles(smiles or "")
                if mol is None:
                    mol = Chem.MolFromSmiles(smiles or "", sanitize=False)
                    if mol is not None:
                        mol.UpdatePropertyCache(strict=False)
                        Chem.FastFindRings(mol)
            if mol is None:
                return None
            try:
                mol = Chem.RemoveHs(mol, sanitize=False)
            except Exception:  # noqa: BLE001 - keep the hydrogens rather than fail
                pass
            return mol
    except Exception:  # noqa: BLE001
        return None


def match_drawn(molfile: str | None, smiles: str | None, match: str, snap: Snapshot,
                core_nos: list[int], notes: list[str]) -> Hits:
    """CORE number -> Hit (reason ``drawn``) among ``core_nos``."""
    hits: Hits = {}
    query = read_query(molfile, smiles)
    if query is None:
        notes.append("The drawn structure could not be read, so nothing matches it.")
        return hits
    if query.GetNumAtoms() == 0:
        notes.append("The drawn structure is empty, so nothing matches it.")
        return hits
    if match == "exact":
        try:
            norm = snap.normalize(molfile=molfile, smiles=smiles)
        except ChemistryError as exc:
            notes.append(f"The drawn structure cannot be used for an exact search, so nothing "
                         f"matches it: {exc.message}")
            return hits
        notes.append("Exact search: compounds with the same constitution as the drawn "
                     "structure are listed (stereo is ignored).")
        wanted = set(core_nos)
        found: Hits = {}
        _add_lookup(snap, norm, "drawn", found)
        return {no: hit for no, hit in found.items() if no in wanted}

    notes.append("Substructure search: compounds that contain the drawn structure are listed "
                 "(stereo and drawn hydrogens are ignored).")
    snap.load_molfiles(core_nos)
    with rdBase.BlockLogs():
        for core_no in core_nos:
            own = set()
            for v in snap.entries[core_no].variants:
                target = read_molblock(snap.molfile_cache.get(v.core_stereo_id, ""))
                try:
                    if target is not None and target.HasSubstructMatch(query, useChirality=False):
                        own.add(v.stereo_no)
                except Exception:  # noqa: BLE001 - an odd target never breaks the search
                    continue
            if own:
                _add(hits, core_no, "drawn", own)
    return hits
