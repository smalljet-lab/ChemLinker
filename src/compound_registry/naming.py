"""Getting a systematic name for a registered stereo variant, safely.

A wrong name is worse than no name. So a name from the local naming program
(ChemDraw, see ``chemdraw``) is accepted only when two things hold:

1. *ChemDraw read the registered structure.* The standard InChI ChemDraw
   reports for what it read must equal the InChI of the structure sent.
2. *The name says what the registry's stereo variant says.* The registry keys
   a variant by the set of stereoisomers it denotes (``stereo_key``, see
   ``chem``). ChemDraw reads that meaning from the file:

   ===================  ==============================  =====================
   registry variant      what is sent                    the name must
   ===================  ==============================  =====================
   one stereoisomer      wedges as stored, marked        not start with
   (none/abs/partial)    absolute (chiral flag 1)        ``rac-`` / ``rel-``
   stereo not given      wedges removed                  not start with
   (undefined)                                           ``rac-`` / ``rel-``
   racemate              all centres in one AND group    start with ``rac-``
   relative              all centres in one OR group     start with ``rel-``
   ===================  ==============================  =====================

   (Checked against ChemDraw 26.0: the same drawing is named "(S)-...",
   "rac-(R)-..." or "rel-(R)-..." depending on exactly these marks.)

Variants for which this cannot be checked are not named: mixed AND/OR groups
and anything with a chiral axis (InChI does not describe atropisomers, so
check 1 could not catch a wrong axis).

:func:`prepare` builds what is sent, :func:`generate` does the whole thing and
never raises: it returns the name, or the reason there is none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from rdkit import Chem, rdBase

from . import chemdraw
from .chem import _CHIRAL_TAGS, _inchi, _parse_molblock
from .chemdraw import ChemDrawAnswer, ChemDrawError
from .errors import RegistryError

__all__ = ["NameOutcome", "Prepared", "NamingSkipped", "prepare", "check_answer", "generate",
           "generator_info", "MAX_GENERATED_NAME"]

#: Longer names are not stored automatically (same limit as a typed name).
MAX_GENERATED_NAME = 1000

_PASTE_HINT = "You can still type or paste the name."


class NamingSkipped(Exception):
    """This variant is not named automatically; ``message`` says why."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Prepared:
    molfile: str        # what is sent to the naming program
    inchi: str          # standard InChI of exactly that structure
    kind: str           # "single", "rac" or "rel"


@dataclass(frozen=True)
class NameOutcome:
    """``name`` is the accepted name, or None with ``message`` saying why
    (plain words, shown to the user). ``code`` is ``ok``, ``unavailable``
    (no naming program on this PC), ``skipped`` (this kind of variant is not
    named automatically), ``rejected`` (a name came back but failed a check)
    or ``failed`` (the program gave no usable answer)."""

    name: str | None
    message: str | None
    code: str
    tool: str = chemdraw.LABEL


def generator_info() -> dict[str, object]:
    """``{"available", "label"}`` for ``ping`` / ``doctor``."""
    found = chemdraw.available()
    return {"available": bool(found), "label": chemdraw.LABEL if found else None}


def _members(stereo_key: str) -> list[list[str]]:
    try:
        value = json.loads(stereo_key)
    except (TypeError, ValueError) as exc:
        raise NamingSkipped("The stored stereo description could not be read.") from exc
    if (not isinstance(value, list) or not value
            or not all(isinstance(m, list) and m and all(isinstance(x, str) for x in m)
                       for m in value)):
        raise NamingSkipped("The stored stereo description could not be read.")
    return value


def prepare(molfile: str, stereo_key: str) -> Prepared:
    """The file to send for the variant with this stored molfile and stereo
    key. Raises :class:`NamingSkipped` when the variant cannot be named safely.
    """
    members = _members(stereo_key)
    if any("|atrop=" in m for group in members for m in group):
        raise NamingSkipped("This compound has a chiral axis (atropisomer); its name cannot be "
                            f"checked automatically. {_PASTE_HINT}")
    if len(members) == 1 and len(members[0]) == 1:
        kind = "single"
    elif len(members) == 1 and len(members[0]) == 2:
        kind = "rac"
    elif len(members) == 2 and all(len(group) == 1 for group in members):
        kind = "rel"
    else:
        raise NamingSkipped("This compound has mixed stereo groups (AND/OR); its name cannot be "
                            f"checked automatically. {_PASTE_HINT}")
    wanted = {m for group in members for m in group}

    try:
        mol = _parse_molblock(molfile)
        drawn = _inchi(mol)[0]
    except RegistryError as exc:
        raise NamingSkipped(f"The stored structure could not be read: {exc.message}") from exc
    centres = [a.GetIdx() for a in mol.GetAtoms() if a.GetChiralTag() in _CHIRAL_TAGS]
    work = Chem.RWMol(mol)
    work.SetStereoGroups([])

    try:
        if kind == "single":
            if drawn not in wanted:
                # The variant was registered as "stereo not given": the wedges do not count.
                for idx in centres:
                    work.GetAtomWithIdx(idx).SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
                drawn = _inchi(work)[0]
                if drawn not in wanted:
                    raise NamingSkipped("The stored drawing does not reproduce the registered "
                                        f"stereo variant. {_PASTE_HINT}")
            work.SetIntProp("_MolFileChiralFlag", 1)
            with rdBase.BlockLogs():
                text = Chem.MolToMolBlock(work)
        else:
            mirror = Chem.RWMol(mol)
            for idx in centres:
                mirror.GetAtomWithIdx(idx).InvertChirality()
            if not centres or {drawn, _inchi(mirror)[0]} != wanted or len(wanted) != 2:
                # Two stereoisomers that are not mirror images: a group over some centres only.
                raise NamingSkipped("This compound has mixed stereo groups (AND/OR); its name "
                                    f"cannot be checked automatically. {_PASTE_HINT}")
            group_type = (Chem.StereoGroupType.STEREO_AND if kind == "rac"
                          else Chem.StereoGroupType.STEREO_OR)
            work.SetStereoGroups([Chem.CreateStereoGroup(group_type, work, centres)])
            work.SetIntProp("_MolFileChiralFlag", 0)
            with rdBase.BlockLogs():
                text = Chem.MolToV3KMolBlock(work)
    except RegistryError as exc:
        raise NamingSkipped(f"The stored structure could not be prepared: {exc.message}") from exc
    return Prepared(molfile=text, inchi=drawn, kind=kind)


def check_answer(prepared: Prepared, answer: ChemDrawAnswer) -> NameOutcome:
    """Accept or reject what the naming program answered for ``prepared``."""
    tool = chemdraw.LABEL
    name = " ".join(answer.name.split())  # a wrapped name becomes one line
    if not name:
        return NameOutcome(None, f"{tool} has no name for this structure. {_PASTE_HINT}", "failed")
    if len(name) > MAX_GENERATED_NAME or chemdraw.has_control(name):
        return NameOutcome(None, f"{tool} returned a name that cannot be stored (too long or "
                                 f"with control characters). {_PASTE_HINT}", "rejected")
    if answer.inchi != prepared.inchi:
        return NameOutcome(None, f"{tool} read the structure differently from the registry, so "
                                 f"its name was not stored. {_PASTE_HINT}", "rejected")
    lowered = name.lower()
    has_prefix = {"rac": lowered.startswith("rac-"), "rel": lowered.startswith("rel-")}
    expected = {"single": (False, False), "rac": (True, False), "rel": (False, True)}[prepared.kind]
    if (has_prefix["rac"], has_prefix["rel"]) != expected:
        return NameOutcome(None, f"{tool}'s name does not say the same about the stereo as the "
                                 f"registry does, so it was not stored. {_PASTE_HINT}", "rejected")
    return NameOutcome(name, None, "ok")


def generate(molfile: str, stereo_key: str, *,
             ask: Callable[[str], ChemDrawAnswer] | None = None) -> NameOutcome:
    """Name for the variant, or the reason there is none. Never raises.

    ``ask`` hands a molfile to the naming program (default: the installed
    ChemDraw); tests replace it.
    """
    tool = chemdraw.LABEL
    try:
        prepared = prepare(molfile, stereo_key)
    except NamingSkipped as exc:
        return NameOutcome(None, exc.message, "skipped")
    except Exception as exc:  # noqa: BLE001 - a name must never break the caller
        return NameOutcome(None, f"The structure could not be prepared for {tool} "
                                 f"({type(exc).__name__}). {_PASTE_HINT}", "failed")
    try:
        answer = (ask or chemdraw.name_molfile)(prepared.molfile)
    except ChemDrawError as exc:
        code = "unavailable" if exc.code == "unavailable" else "failed"
        return NameOutcome(None, f"{exc.message} {_PASTE_HINT}", code)
    except Exception as exc:  # noqa: BLE001
        return NameOutcome(None, f"{tool} could not be used ({type(exc).__name__}). "
                                 f"{_PASTE_HINT}", "failed")
    try:
        return check_answer(prepared, answer)
    except Exception as exc:  # noqa: BLE001 - e.g. an answer of the wrong shape
        return NameOutcome(None, f"{tool}'s answer could not be read ({type(exc).__name__}). "
                                 f"{_PASTE_HINT}", "failed")
