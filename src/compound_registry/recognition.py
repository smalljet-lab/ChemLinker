"""Capture reaction: the host side of reading a reaction scheme from a picture.

The four operations of ``protocol.CAPTURE_OPS`` end here. None of them needs a
registry.

Parts
-----
* **Where it is installed.** The optional recognition part lives in its own
  folder, the "recognition home": ``<per-user folder>/recognition`` with
  ``env/`` (a Python environment of its own: torch, RxnScribe, MolScribe,
  EasyOCR) and ``models/`` inside. The per-user folder is the one the browser
  registration uses on Windows (``%LOCALAPPDATA%\\CompoundRegistry``);
  ``$COMPOUND_REGISTRY_RECOGNITION_HOME`` names another place. It is installed
  by ``compound-registry install-recognition`` (``recognition_install.py``),
  never by the host. The registry's own environment gets no new package.
* **The worker.** :class:`Worker` runs ``recognition_worker.py`` with the
  Python of that environment and talks to it in JSON lines. The models are
  loaded once (20 to 35 seconds, minutes on the very first run) and the worker
  stays alive. :class:`Recognition` starts it when asked, restarts it once when
  it died during a request, stops it after :data:`IDLE_STOP_S` without use and
  when the host ends. The worker ends by itself as soon as its stdin closes,
  so none is left behind when the host is killed.
* **The result.** The worker returns raw model output. :func:`build_result`
  turns it into the protocol result with the registry's own RDKit, so a
  SMILES here is canonical exactly like everywhere else in the product, adds
  the warnings (:func:`structure_warnings`) and has every molecule drawn
  (``depiction.py``: the shape of the picture, wedges that agree with the
  SMILES).
* **ChemDraw.** :func:`reaction_to_chemdraw` writes the (edited) reaction as a
  ChemDraw document into the user's temporary folder and opens it the way the
  route feature does.

SMILES or molfile? (measured, Phase 0 test set, 2026-11-24)
-----------------------------------------------------------
MolScribe answers every molecule twice: as a SMILES and as a molfile. Its
molfile keeps an abbreviation the chemist drew ("Boc", "OTf", "Me") as an
``R`` atom with an alias line, while its SMILES has the abbreviation expanded.
Compared with the truth by InChIKey (``tools/recognition-spike``,
``results/raw/base-*.json`` against ``testset/truth.json``):

* single molecules (35): the SMILES was right for 34, the molfile for 25; the
  two differed for 9, and in all 9 the SMILES was the right one;
* reaction pictures (89 truth molecules drawn): 79 found from the SMILES, 64
  from the molfile. Of the 157 molecule boxes, 48 have a molfile that gives
  no InChIKey at all (it holds an ``R`` atom), and 62 have a molfile that is
  not the structure of the SMILES by the comparison used here
  (:func:`_identity`; an R group written in another way counts too). All 62
  hold a placeholder. Where the truth is known the SMILES was a truth
  molecule in 15 of them, the molfile in none.

So the SMILES is the source of ``smiles``, and the molfile that is returned
is always written again for the structure of the SMILES (``depiction.py``):
its atoms stand where MolScribe saw them in the picture (an expanded
abbreviation is laid out by RDKit from the place of its label), and its
wedges are worked out from the structure, never copied blindly, so that the
drawing the user compares with the picture, edits and sends to ChemDraw is
the structure of the SMILES and looks like the picture. MolScribe's own
molfile is not passed on: its coordinates are squeezed whenever the white
margins of the picture are unequal (see ``depiction.picture_places``; the
worker reports what that needs as ``seen``), and its wedges may disagree
with its SMILES. Only when the SMILES cannot be read is the molfile used as
the source.

The warning ``smiles_molfile_differ`` is kept for a real disagreement. A
molfile that is the SMILES structure with a group left as a placeholder
(:func:`_is_abbreviated_drawing_of`) is no disagreement: that is how MolScribe
writes every abbreviation, and warning about 62 of 157 boxes (39 %) would
teach the user to ignore it. With that rule 6 of the 157 boxes get the
warning, each one a difference in stereo: three in the Phase 0 picture where
the two answers gave opposite wedges, three where the SMILES lost a
double-bond geometry that the drawing has.

Confidence: with the same data, 5 of the 9 molecules below 0.8 were wrong and
7 of the 116 at or above it; hence :data:`LOW_CONFIDENCE`.

Privacy
-------
Nothing read from a picture is logged or put into an error message: no image
data, no structure, no text. The worker reports the NAME of an exception
class only. The picture travels to the worker through its stdin; no file is
written.
"""

from __future__ import annotations

import atexit
import base64
import binascii
import collections
import json
import logging
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from . import protocol
from .errors import RegistryError

__all__ = ["HOME_ENV_VAR", "INSTALL_COMMAND", "how_to_run_the_installer", "COMMON_ELEMENTS", "LOW_CONFIDENCE", "IDLE_STOP_S",
           "LOAD_TIMEOUT_S", "RXN_CHECKPOINT", "MOL_CHECKPOINT", "OCR_FILES", "MARKER_NAME",
           "recognition_home", "env_python", "models_dir", "worker_script", "install_state",
           "check_png", "structure_warnings", "describe_structures", "build_result",
           "Worker", "WorkerError", "Recognition", "default", "shutdown",
           "reaction_to_chemdraw", "op_recognition_status", "op_recognize",
           "op_describe_structures", "op_reaction_to_chemdraw", "OPERATIONS"]

#: Names another recognition home (tests, a second installation).
HOME_ENV_VAR = "COMPOUND_REGISTRY_RECOGNITION_HOME"
#: What the user runs to install the optional part.
INSTALL_COMMAND = "compound-registry install-recognition"

#: The model files inside ``models/`` (see recognition_install.py for where
#: they come from and their checksums).
RXN_CHECKPOINT = "pix2seq_reaction_full.ckpt"
MOL_CHECKPOINT = "swin_base_char_aux_1m680k.pth"
OCR_FILES = ("easyocr/craft_mlt_25k.pth", "easyocr/english_g2.pth")
#: Written into the recognition home as the LAST step of a successful
#: installation; without it the part counts as not installed.
MARKER_NAME = "installed.json"

#: The worker is stopped after this many seconds without a request.
IDLE_STOP_S = 600.0
#: Loading the models may take this long before the worker counts as failed
#: (the first run after an installation took up to 200 seconds).
LOAD_TIMEOUT_S = 600.0
#: How long a worker gets to end by itself before it is killed.
STOP_GRACE_S = 3.0
#: A longer line from the worker means the stream is broken.
MAX_WORKER_LINE = 8_000_000
#: Lines of the worker's stderr kept in memory (never logged, never sent).
STDERR_TAIL_LINES = 40

#: A molecule below this confidence gets ``low_confidence`` (see the module
#: text for the measurement).
LOW_CONFIDENCE = 0.8

#: Elements that are common in the molecules of organic synthesis. Anything
#: else gets ``unusual_element``: in Phase 0 a wrong reading often showed as an
#: odd element (Pb, Pa, Rf, V, Mg, Y). Deliberately short: a real palladium
#: complex is flagged too, and the user confirms it.
COMMON_ELEMENTS = frozenset({"H", "B", "C", "N", "O", "F", "Si", "P", "S", "Cl", "Br", "I",
                             "Li", "Na", "K"})

#: A structure with more atoms is answered as ``unparsable``. RDKit writes a
#: SMILES recursively: a chain of about 3000 atoms overflows the C stack and
#: ends the whole host process, and nothing can catch that. The rest of the
#: product never gets that far because InChI stops at 1023 atoms.
MAX_STRUCTURE_ATOMS = 1000
#: A longer SMILES is not even parsed (no SMILES of at most
#: :data:`MAX_STRUCTURE_ATOMS` atoms comes near this length).
MAX_SMILES_CHARS = 20_000

#: The answer of ``recognize`` is kept this far below the frame limit.
_RESULT_BYTES = protocol.MAX_RESPONSE_BYTES - 20_000
_MAX_TEXT_BLOCKS = 20
_ERROR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,80}\Z")
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_LOGGER = "compound_registry.host"

NOT_INSTALLED = ("Reading reactions from pictures is an optional part of ChemLinker that is not "
                 f"installed on this PC. To install it, run '{INSTALL_COMMAND}' (about 2.8 GB; "
                 "it needs the internet once, afterwards everything runs on this PC).")


def how_to_run_the_installer() -> str:
    """Where and how to type :data:`INSTALL_COMMAND` on this PC: the command
    lives inside the environment this program runs from, not on the PATH."""
    windows = os.name == "nt"
    launcher = Path(sys.prefix) / ("Scripts" if windows else "bin") / (
        "compound-registry.exe" if windows else "compound-registry")
    if not launcher.is_file():
        return ""
    relative = launcher.relative_to(Path(sys.prefix).parent)
    return (f" Open a terminal in the folder {Path(sys.prefix).parent} and type: "
            f"{relative} install-recognition")


def _log() -> logging.Logger:
    return logging.getLogger(_LOGGER)


def _failed(message: str) -> RegistryError:
    return RegistryError(message, code="recognition_failed")


# --------------------------------------------------------------------------- where it is

def per_user_folder(environ: Any = None, platform: str | None = None) -> Path:
    """The per-user program data folder of this product: on Windows the one
    that also holds the browser registration (``install_host.default_target``),
    ``%LOCALAPPDATA%\\CompoundRegistry``."""
    environ = os.environ if environ is None else environ
    platform = platform or sys.platform
    if platform.startswith("win"):
        local = str(environ.get("LOCALAPPDATA", "")).strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
    elif platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        data = str(environ.get("XDG_DATA_HOME", "")).strip()
        base = Path(data) if data and os.path.isabs(data) else Path.home() / ".local" / "share"
    return base / "CompoundRegistry"


def recognition_home(environ: Any = None, platform: str | None = None) -> Path:
    """``$COMPOUND_REGISTRY_RECOGNITION_HOME``, else ``<per-user folder>/recognition``."""
    environ = os.environ if environ is None else environ
    override = str(environ.get(HOME_ENV_VAR, "")).strip()
    if override:
        return Path(override).expanduser()
    return per_user_folder(environ, platform) / "recognition"


def env_python(home: Path, platform: str | None = None) -> Path:
    """The Python of the recognition environment."""
    if (platform or sys.platform).startswith("win"):
        return Path(home) / "env" / "Scripts" / "python.exe"
    return Path(home) / "env" / "bin" / "python"


def models_dir(home: Path) -> Path:
    return Path(home) / "models"


def model_files(home: Path) -> list[Path]:
    base = models_dir(home)
    return [base / RXN_CHECKPOINT, base / MOL_CHECKPOINT] + [base / name for name in OCR_FILES]


def worker_script() -> Path:
    return Path(__file__).with_name("recognition_worker.py")


def install_state(home: Path | None = None) -> dict[str, Any]:
    """``{"installed", "home", "problem"}``: whether the recognition part is in
    place in ``home`` (default: :func:`recognition_home`) AND was installed
    for the programs and models this version of ChemLinker expects. Looks at
    files only (no model checksum, nothing is started), so it is cheap. Never
    raises."""
    try:
        home = Path(home) if home is not None else recognition_home()
        problem = None
        if not env_python(home).is_file():
            problem = "its Python environment is missing"
        elif any(not path.is_file() for path in model_files(home)):
            problem = "a model file is missing"
        elif not (home / MARKER_NAME).is_file():
            problem = "its installation was not finished"
        elif not worker_script().is_file():
            problem = "the worker program is missing from this installation"
        else:
            problem = _record_problem(home)
        return {"installed": problem is None, "home": str(home), "problem": problem}
    except Exception as exc:  # noqa: BLE001 - a status question never fails
        return {"installed": False, "home": str(home) if home is not None else None,
                "problem": f"it could not be checked ({type(exc).__name__})"}


OUTDATED = ("it was installed for another version of ChemLinker (the recognition programs "
            "or the models have changed since)")


def read_record(home: Path) -> dict[str, Any] | None:
    """What ``installed.json`` in ``home`` says, or None when there is none
    that can be read."""
    try:
        record = json.loads((Path(home) / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _record_problem(home: Path) -> str | None:
    """None when the installation record fits what this version ships.

    The worker comes from this package and is written for exactly the pinned
    libraries and model files. After an update of ChemLinker alone the old
    environment would still be there: the new worker would run on libraries it
    was never tested with, or fail with nothing but the name of an exception.
    So an installation made for other pins counts as not installed, and
    ``install-recognition`` brings it up to date."""
    from . import recognition_install

    record = read_record(home)
    if record is None:
        return "its installation record cannot be read"
    return None if recognition_install.record_is_current(record) else OUTDATED


# --------------------------------------------------------------------------- the picture

def _image_invalid(message: str) -> RegistryError:
    return RegistryError(message, code="image_invalid")


def check_png(image: str) -> tuple[int, int]:
    """``(width, height)`` of the PNG that ``image`` (base64) holds. Raises
    ``image_invalid`` when it is not base64, not a PNG, or of unusable size.
    Reads the header only; the picture itself is decoded by the worker."""
    if not isinstance(image, str) or not image or len(image) % 4 or not _BASE64.match(image):
        raise _image_invalid("The picture is not valid base64 text.")
    try:
        data = base64.b64decode(image, validate=True)
    except (binascii.Error, ValueError):
        raise _image_invalid("The picture is not valid base64 text.") from None
    if len(data) < 45 or data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR" \
            or data[8:12] != b"\x00\x00\x00\x0d":
        raise _image_invalid("The picture is not a PNG image.")
    width, height = struct.unpack(">II", data[16:24])
    low, high = protocol.MIN_IMAGE_SIDE, protocol.MAX_IMAGE_SIDE
    if not (low <= width <= high and low <= height <= high):
        raise _image_invalid(f"The picture is {width} x {height} pixels; each side must be "
                             f"between {low} and {high} pixels.")
    return width, height


# --------------------------------------------------------------------------- chemistry

def _mol_from_smiles(smiles: Any):
    """A sanitised molecule, or None. Placeholders (``*``) are allowed."""
    from rdkit import Chem, rdBase

    if not isinstance(smiles, str) or not smiles.strip() or len(smiles) > MAX_SMILES_CHARS:
        return None
    with rdBase.BlockLogs():
        try:
            mol = Chem.MolFromSmiles(smiles.strip())
        except Exception:  # noqa: BLE001 - RDKit raises on a few malformed inputs
            mol = None
    return mol if mol is not None and 0 < mol.GetNumAtoms() <= MAX_STRUCTURE_ATOMS else None


def _mol_from_molfile(molfile: Any):
    """A sanitised molecule read like ``chem`` reads a molfile (explicit H
    folded in, header lines tolerated), or None. Unlike ``chem.normalize`` it
    accepts placeholders: here they are reported, not refused. Like it, it
    refuses what is not ONE structure: a file with a second record, a polymer
    or mixture bracket. Too large a structure is refused as well
    (:data:`MAX_STRUCTURE_ATOMS`)."""
    from rdkit import Chem, rdBase

    from . import chem

    if not isinstance(molfile, str) or not molfile.strip():
        return None
    text = molfile.replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith("$RXN"):
        return None
    if "$$$$" in text and text.split("$$$$", 1)[1].strip():
        return None                       # an SD file with several structures
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
                mol = Chem.MolFromMolBlock(candidate, sanitize=True, removeHs=True,
                                           strictParsing=False)
            except Exception:  # noqa: BLE001
                mol = None
            if mol is not None and mol.GetNumAtoms() > 0:
                break
        else:
            return None
        if mol.GetNumAtoms() > MAX_STRUCTURE_ATOMS:
            return None
        for group in Chem.GetMolSubstanceGroups(mol):
            if group.HasProp("TYPE") and group.GetProp("TYPE") in chem._UNSUPPORTED_SGROUPS:
                return None
    return mol


def _smiles_of(mol: Any) -> str:
    """The canonical SMILES the rest of the product shows (``chem``'s display
    SMILES) without the CXSMILES extension, so that it can be joined into a
    reaction SMILES."""
    from . import chem

    return chem._display_smiles(mol).split(" |", 1)[0].strip()


def _has_placeholder(mol: Any) -> bool:
    """True for anything that stands for "something else": an R group, ``*``,
    an abbreviation label, a query atom, or a query bond (the molfile bond
    types "any", "single or double", ...; their SMILES is written with ``~``
    and is no real SMILES)."""
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() == 0 or atom.HasQuery():
            return True
        if atom.HasProp("molFileAlias") and atom.GetProp("molFileAlias").strip():
            return True
    return any(bond.HasQuery() for bond in mol.GetBonds())


def _identity(mol: Any) -> str:
    """What makes MolScribe's two answers "the same structure": the standard
    InChIKey, or the canonical SMILES when InChI cannot describe it
    (placeholders). Not used for ``duplicate`` (see :func:`_assemble`)."""
    from rdkit import Chem, rdBase

    if not _has_placeholder(mol):
        with rdBase.BlockLogs():
            try:
                key = Chem.MolToInchiKey(mol)
            except Exception:  # noqa: BLE001
                key = ""
        if key:
            return "ik:" + key
    return "smi:" + _smiles_of(mol)


def _has_double_bond_geometry(mol: Any) -> bool:
    """True when a double bond has E/Z geometry or COULD have it. Like a
    stereocentre without a wedge, a double bond whose geometry was not read is
    to be checked: a dropped E/Z is a known failure of the recognition. A
    double bond to an end atom (``C=NH``) is left out: nobody draws its
    geometry."""
    from rdkit import Chem

    none = (Chem.BondStereo.STEREONONE, Chem.BondStereo.STEREOANY)
    if any(bond.GetStereo() not in none for bond in mol.GetBonds()):
        return True
    try:
        possible = Chem.FindPotentialStereo(mol)
    except Exception:  # noqa: BLE001
        return False
    for item in possible:
        if item.type != Chem.StereoType.Bond_Double:
            continue
        bond = mol.GetBondWithIdx(item.centeredOn)
        if bond.GetBeginAtom().GetDegree() > 1 and bond.GetEndAtom().GetDegree() > 1:
            return True
    return False


def structure_warnings(mol: Any) -> list[str]:
    """The warnings that can be told from one structure alone, in the order of
    ``protocol.STRUCTURE_WARNING_CODES`` (without ``unparsable``)."""
    from rdkit import Chem, rdBase

    found = set()
    if _has_placeholder(mol):
        found.add("placeholder")
    charged = []
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() == 0:
            continue
        if atom.GetIsotope():
            found.add("isotope")
        if atom.GetSymbol() not in COMMON_ELEMENTS:
            found.add("unusual_element")
        if atom.GetFormalCharge():
            charged.append(atom)
    # A nitro group, an N-oxide or an ylide is written with two opposite
    # charges on neighbouring atoms; that is no salt.
    for atom in charged:
        sign = atom.GetFormalCharge() > 0
        if not any(n.GetFormalCharge() and (n.GetFormalCharge() > 0) != sign
                   for n in atom.GetNeighbors()):
            found.add("charged_or_salt")
            break
    with rdBase.BlockLogs():
        try:
            if len(Chem.GetMolFrags(mol)) > 1:
                found.add("charged_or_salt")
        except Exception:  # noqa: BLE001
            pass
        try:
            centres = Chem.FindMolChiralCenters(mol, includeUnassigned=True,
                                                useLegacyImplementation=False)
        except Exception:  # noqa: BLE001
            centres = []
        if centres or _has_double_bond_geometry(mol):
            found.add("stereo")
    return [code for code in protocol.STRUCTURE_WARNING_CODES if code in found]


def describe_structures(structures: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """``describe_structures``: canonical SMILES and warnings of each
    structure, in order. An unreadable one is ``{"smiles": None, "warnings":
    ["unparsable"]}``."""
    out = []
    for item in structures:
        molfile, smiles = item.get("molfile"), item.get("smiles")
        mol = _mol_from_molfile(molfile) if molfile is not None else _mol_from_smiles(smiles)
        if mol is None:
            out.append({"smiles": None, "warnings": ["unparsable"]})
        else:
            out.append({"smiles": _smiles_of(mol), "warnings": structure_warnings(mol)})
    return {"structures": out}


def _molfile_of(mol: Any, like: Any = None, box: Any = None, seen: Any = None) -> str | None:
    """The V2000 molfile shown for ``mol``: on the places of ``like`` (the
    drawing MolScribe gave, unsanitised; ``box`` and ``seen`` say where in the
    picture it is) so that it looks like the picture, with wedges that agree
    with ``mol``. See :mod:`compound_registry.depiction`."""
    from . import depiction

    return depiction.drawing_molfile(mol, like, box, seen)


def _is_abbreviated_drawing_of(drawn: Any, full: Any) -> bool:
    """True when ``drawn`` (MolScribe's molfile) is the structure ``full`` (its
    SMILES) with groups left as placeholders: an abbreviation such as "Boc"
    that the SMILES has expanded, or an R group written in another way. Every
    real atom of the drawing must be found in ``full`` with the same
    neighbours, charge, isotope and stereo (wedges and E/Z included), and
    ``full`` must have nothing else besides what hangs on a placeholder. That
    is no disagreement between the two answers, so it gets no warning."""
    from rdkit import Chem

    placeholders = [a.GetIdx() for a in drawn.GetAtoms() if a.GetAtomicNum() == 0]
    if not placeholders or any(bond.HasQuery() for bond in drawn.GetBonds()):
        return False
    try:
        query = Chem.RWMol(drawn)
        for index in placeholders:
            query.ReplaceAtom(index, Chem.AtomFromSmarts("*"), preserveProps=False)
        matches = full.GetSubstructMatches(query.GetMol(), useChirality=True, uniquify=True,
                                           maxMatches=64)
    except Exception:  # noqa: BLE001 - then it counts as a difference
        return False
    unset = Chem.ChiralType.CHI_UNSPECIFIED
    no_geometry = (Chem.BondStereo.STEREONONE, Chem.BondStereo.STEREOANY)
    wild = set(placeholders)
    for match in matches:
        matched = set(match)
        same = True
        for atom in drawn.GetAtoms():
            if atom.GetIdx() in wild:
                continue
            other = full.GetAtomWithIdx(match[atom.GetIdx()])
            if (atom.GetDegree() != other.GetDegree()
                    or atom.GetFormalCharge() != other.GetFormalCharge()
                    or atom.GetIsotope() != other.GetIsotope()
                    or (atom.GetChiralTag() == unset) != (other.GetChiralTag() == unset)):
                same = False
                break
        for bond in drawn.GetBonds() if same else ():
            other = full.GetBondBetweenAtoms(match[bond.GetBeginAtomIdx()],
                                             match[bond.GetEndAtomIdx()])
            if other is None or (bond.GetStereo() in no_geometry) != (
                    other.GetStereo() in no_geometry):
                same = False
                break
        # Nothing of ``full`` may stand apart from the drawing (a counter-ion).
        if same and all(matched & set(fragment) for fragment in Chem.GetMolFrags(full)):
            return True
    return False


def _read_molecule(smiles: Any, molfile: Any, box: Any = None, seen: Any = None) -> dict[str, Any]:
    """One MolScribe answer -> ``{"smiles", "molfile", "warnings", "identity"}``
    (see "SMILES or molfile?" in the module text). ``box`` and ``seen``: where
    the molecule is in the picture (``depiction.picture_places``).

    The molfile that is returned is always written again: on the places the
    picture has, with wedges worked out from the structure that ``smiles``
    names (see :mod:`compound_registry.depiction`). MolScribe's own molfile is
    passed on unchanged only when nothing could be read from either answer."""
    from .routes import read_molfile

    from_smiles = _mol_from_smiles(smiles)
    from_molfile = _mol_from_molfile(molfile)
    given = molfile if isinstance(molfile, str) and molfile.strip() else None
    if from_smiles is None and from_molfile is None:
        # Kept when it can at least be drawn, so the user can repair it.
        drawable = given if given is not None and read_molfile(given) is not None else None
        return {"smiles": None, "molfile": drawable, "warnings": ["unparsable"], "identity": None}
    differ = False
    drawn = read_molfile(given) if given is not None else None
    if from_smiles is not None:
        mol = from_smiles
        if not (from_molfile is not None and _identity(from_molfile) == _identity(mol)):
            differ = given is not None and not (
                from_molfile is not None and _is_abbreviated_drawing_of(from_molfile, mol))
        out_molfile = _molfile_of(mol, drawn, box, seen)
    else:
        mol = from_molfile
        out_molfile = _molfile_of(mol, drawn, box, seen) or given
        differ = isinstance(smiles, str) and bool(smiles.strip())
    warnings = structure_warnings(mol)
    if differ:
        warnings.append("smiles_molfile_differ")
    return {"smiles": _smiles_of(mol), "molfile": out_molfile, "warnings": warnings,
            "identity": _identity(mol)}


def _box(value: Any, width: int, height: int) -> list[int] | None:
    """A valid pixel box inside the picture, or None."""
    try:
        x0, y0, x1, y1 = (int(round(float(v))) for v in value)
    except (TypeError, ValueError):
        return None
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, width), min(y1, height)
    return [x0, y0, x1, y1] if x0 < x1 and y0 < y1 else None


def _clean_lines(lines: Any) -> list[str]:
    out = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, str):
            continue
        text = " ".join("".join(ch if ch.isprintable() else " " for ch in line).split())
        if text:
            out.append(text[:protocol.MAX_REACTION_TEXT])
        if len(out) >= protocol.MAX_CHEMDRAW_CONDITION_LINES:
            break
    return out


def reaction_smiles(reactants: Sequence[str | None], agents: Sequence[str | None],
                    products: Sequence[str | None]) -> str | None:
    """``reactants>agents>products``; None when a reactant or product has no
    SMILES or there is neither. Agents without SMILES are left out."""
    if not reactants and not products:
        return None
    if any(not s for s in list(reactants) + list(products)):
        return None
    return ">".join(".".join(part) for part in
                    (reactants, [s for s in agents if s], products))


def _ordered(codes: set[str], order: Sequence[str]) -> list[str]:
    return [code for code in order if code in codes]


def build_result(raw: Any, mode: str, seconds: float, *,
                 max_bytes: int = _RESULT_BYTES) -> dict[str, Any]:
    """The protocol result of ``recognize`` from the worker's raw answer.
    Raises ``recognition_failed`` when the answer has not the agreed shape."""
    if not isinstance(raw, dict) or not isinstance(raw.get("molecules"), list) \
            or not isinstance(raw.get("reactions"), list):
        raise _failed("The recognition gave an answer that could not be understood.")
    try:
        width, height = int(raw["width"]), int(raw["height"])
    except (KeyError, TypeError, ValueError):
        raise _failed("The recognition gave an answer that could not be understood.") from None

    read: dict[int, dict[str, Any]] = {}          # worker index -> molecule (without "n")
    for index, item in enumerate(raw["molecules"]):
        box = _box(item.get("box"), width, height) if isinstance(item, dict) else None
        if box is None:
            continue
        # ``box`` as the worker gave it: the picture MolScribe was handed.
        molecule = _read_molecule(item.get("smiles"), item.get("molfile"), item.get("box"),
                                  item.get("seen"))
        confidence = item.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                or not 0.0 <= confidence <= 1.0:
            confidence = None
        else:
            confidence = round(float(confidence), 4)
        if confidence is not None and confidence < LOW_CONFIDENCE:
            molecule["warnings"].append("low_confidence")
        molecule.update(box=box, confidence=confidence)
        read[index] = molecule

    limit = min(len(read), protocol.MAX_RECOGNITION_MOLECULES)
    while True:
        result = _assemble(read, raw["reactions"], mode, width, height, seconds, limit)
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size <= max_bytes or limit == 0:
            return result
        limit = limit * 3 // 4


def _assemble(read: dict[int, dict[str, Any]], raw_reactions: list[Any], mode: str, width: int,
              height: int, seconds: float, limit: int) -> dict[str, Any]:
    kept = list(read)[:limit]
    number = {index: n for n, index in enumerate(kept, start=1)}
    # "The same structure" is the same SMILES: the rule the capture page
    # applies after an edit (protocol: it finds duplicates by equal SMILES).
    # An InChIKey would also merge tautomers and the ionic and the covalent
    # drawing of a metal salt, which are different drawings to check.
    seen: dict[str, int] = {}
    for index in kept:
        text = read[index]["smiles"]
        if text is not None:
            seen[text] = seen.get(text, 0) + 1
    molecules = []
    for index in kept:
        m = read[index]
        codes = set(m["warnings"])
        if m["smiles"] is not None and seen[m["smiles"]] > 1:
            codes.add("duplicate")
        molecules.append({"n": number[index], "box": m["box"], "smiles": m["smiles"],
                          "molfile": m["molfile"], "confidence": m["confidence"],
                          "warnings": _ordered(codes, protocol.MOLECULE_WARNING_CODES)})
    smiles = {m["n"]: m["smiles"] for m in molecules}

    reactions = []
    used: set[int] = set()
    for item in raw_reactions if mode == "reaction" else []:
        if not isinstance(item, dict):
            continue
        roles: dict[str, list[int]] = {}
        for role in ("reactants", "agents", "products"):
            ns: list[int] = []
            for index in item.get(role) if isinstance(item.get(role), list) else []:
                n = number.get(index) if isinstance(index, int) else None
                if n is not None and n not in ns:
                    ns.append(n)
            roles[role] = ns
        if not any(roles.values()):
            continue
        conditions = []
        for text in item.get("texts") if isinstance(item.get("texts"), list) else []:
            box = _box(text.get("box"), width, height) if isinstance(text, dict) else None
            lines = _clean_lines(text.get("lines")) if box is not None else []
            if lines and len(conditions) < _MAX_TEXT_BLOCKS:
                conditions.append({"box": box, "lines": lines})
        used.update(n for ns in roles.values() for n in ns)
        reactions.append({**roles, "conditions": conditions,
                          "reaction_smiles": reaction_smiles(
                              [smiles[n] for n in roles["reactants"]],
                              [smiles[n] for n in roles["agents"]],
                              [smiles[n] for n in roles["products"]])})

    unassigned = [m["n"] for m in molecules if m["n"] not in used] if mode == "reaction" else []
    codes = set()
    if not molecules:
        codes.add("no_molecules")
    elif mode == "reaction" and not reactions:
        codes.add("no_reaction")
    elif mode == "reaction" and unassigned:
        codes.add("unassigned_molecules")
    if limit < len(read):
        codes.add("truncated")
    return {"width": width, "height": height, "molecules": molecules, "reactions": reactions,
            "unassigned": unassigned, "seconds": round(float(seconds), 2),
            "warnings": _ordered(codes, protocol.RECOGNITION_WARNING_CODES)}


# --------------------------------------------------------------------------- the worker

class WorkerError(Exception):
    """The worker gave no usable answer. ``kind``: ``start`` (could not be
    started), ``load`` (ended or failed while loading), ``loading`` (still
    loading when the time was up; it keeps loading), ``timeout`` (no answer in
    time; it was stopped), ``died`` (ended during the request), ``model`` (it
    reported an error; ``stage`` and ``error`` name it)."""

    def __init__(self, kind: str, *, stage: str | None = None, error: str | None = None):
        super().__init__(kind)
        self.kind = kind
        self.stage = stage
        self.error = error


def _safe_name(value: Any) -> str | None:
    return value if isinstance(value, str) and _ERROR_NAME.match(value) else None


class _Running:
    """One worker process and what its reader threads learned about it."""

    def __init__(self, proc: Any, started: float):
        self.proc = proc
        self.started = started
        self.state = "loading"            # loading | ready | ended
        self.load_seconds: float | None = None
        self.error: str | None = None     # name of the exception a failed load reported
        self.replies: dict[Any, dict[str, Any]] = {}
        self.garbage = 0
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)


class Worker:
    """Starts, talks to and stops ONE recognition worker process.

    ``command`` is the whole command line. Thread-safe: requests are served
    one at a time. Nothing the worker writes is logged."""

    def __init__(self, command: Sequence[str], *, cwd: str | os.PathLike | None = None,
                 load_timeout: float = LOAD_TIMEOUT_S,
                 popen: Callable[..., Any] = subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic):
        self.command = [str(part) for part in command]
        self.cwd = str(cwd) if cwd is not None else None
        self.load_timeout = load_timeout
        self._popen = popen
        self._clock = clock
        self._cond = threading.Condition()
        self._request_lock = threading.Lock()
        self._running: _Running | None = None
        self._next_id = 0
        #: Why there is no usable worker: None, or a WorkerError kind.
        self.failure: str | None = None
        self.failure_error: str | None = None
        self.starts = 0

    # ---- state

    @property
    def state(self) -> str:
        """``stopped``, ``loading``, ``ready`` or ``failed``."""
        with self._cond:
            self._check_load_time()
            run = self._running
            if run is not None and run.state in ("loading", "ready"):
                return run.state
            return "failed" if self.failure in ("start", "load") else "stopped"

    @property
    def load_seconds(self) -> float | None:
        with self._cond:
            run = self._running
            if run is None or run.state == "ended":
                return None
            if run.state == "ready":
                return run.load_seconds
            return round(self._clock() - run.started, 1)

    @property
    def pid(self) -> int | None:
        with self._cond:
            run = self._running
            return run.proc.pid if run is not None and run.state != "ended" else None

    @property
    def stderr_tail(self) -> list[str]:
        with self._cond:
            return list(self._running.stderr_tail) if self._running is not None else []

    @property
    def garbage_lines(self) -> int:
        with self._cond:
            return self._running.garbage if self._running is not None else 0

    def _check_load_time(self) -> None:
        run = self._running
        if run is not None and run.state == "loading" \
                and self._clock() - run.started > self.load_timeout:
            self._end(run, "load")
            self.failure_error = "LoadTimeout"

    # ---- start / stop

    def start(self) -> None:
        """Start the worker unless one is loading or ready. Returns at once;
        the models load in the background. Raises ``WorkerError("start")``."""
        with self._cond:
            self._check_load_time()
            run = self._running
            if run is not None and run.state in ("loading", "ready"):
                return
            self.failure = self.failure_error = None
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                proc = self._popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=self.cwd, creationflags=flags,
                                   close_fds=True)
            except (OSError, ValueError) as exc:
                self.failure, self.failure_error = "start", type(exc).__name__
                raise WorkerError("start", error=type(exc).__name__) from None
            run = _Running(proc, self._clock())
            self._running = run
            self.starts += 1
            threading.Thread(target=self._read_stdout, args=(run,), daemon=True,
                             name="recognition-out").start()
            threading.Thread(target=self._read_stderr, args=(run,), daemon=True,
                             name="recognition-err").start()

    def stop(self) -> None:
        """Ask the worker to end, wait a moment, then kill it. Never raises."""
        with self._cond:
            run = self._running
            if run is None or run.state == "ended":
                self._running = None
                return
            run.state = "ended"
            self._running = None
            self._cond.notify_all()
        self._terminate(run.proc, polite=True)

    def _end(self, run: _Running, failure: str | None) -> None:
        """Kill ``run`` (called with the condition held)."""
        if run.state != "ended":
            run.state = "ended"
            if self._running is run:
                self.failure = failure
            self._cond.notify_all()
            threading.Thread(target=self._terminate, args=(run.proc, False), daemon=True).start()

    @staticmethod
    def _terminate(proc: Any, polite: bool) -> None:
        try:
            if polite and proc.poll() is None:
                try:
                    proc.stdin.write(b'{"op": "shutdown"}\n')
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
            try:
                proc.stdin.close()      # the worker ends itself when its stdin closes
            except (OSError, ValueError):
                pass
            if polite:
                try:
                    proc.wait(timeout=STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        except Exception:  # noqa: BLE001 - stopping never fails
            pass
        finally:
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass

    # ---- reader threads

    def _read_stdout(self, run: _Running) -> None:
        stream = run.proc.stdout
        broken = False
        while True:
            try:
                line = stream.readline(MAX_WORKER_LINE + 1)
            except (OSError, ValueError):
                break
            if not line:
                break
            if len(line) > MAX_WORKER_LINE and not line.endswith(b"\n"):
                broken = True
                break
            try:
                message = json.loads(line.decode("utf-8"))
            except ValueError:
                message = None
            with self._cond:
                if not isinstance(message, dict):
                    run.garbage += 1          # a stray line is ignored, never shown
                    continue
                event = message.get("event")
                if event == "ready" and run.state == "loading":
                    run.state = "ready"
                    seconds = message.get("load_seconds")
                    run.load_seconds = (round(float(seconds), 1)
                                        if isinstance(seconds, (int, float))
                                        and not isinstance(seconds, bool)
                                        else round(self._clock() - run.started, 1))
                elif event == "failed":
                    run.error = _safe_name(message.get("error"))
                elif event is None and "id" in message:
                    run.replies[message.get("id")] = message
                self._cond.notify_all()
        # The stream ended: the process is gone, or its output is unusable.
        with self._cond:
            if run.state != "ended":
                was_loading = run.state == "loading"
                run.state = "ended"
                if self._running is run:
                    self.failure = "load" if was_loading else "died"
                    self.failure_error = run.error if was_loading else None
                self._cond.notify_all()
        if broken:
            self._terminate(run.proc, polite=False)
        try:
            run.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self._terminate(run.proc, polite=False)

    def _read_stderr(self, run: _Running) -> None:
        stream = run.proc.stderr
        while True:
            try:
                line = stream.readline(4096)
            except (OSError, ValueError):
                return
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()[:500]
            with self._cond:
                run.stderr_tail.append(text)

    # ---- one request

    def request(self, payload: dict[str, Any], timeout: float) -> Any:
        """Send one request and return its ``result``. Starts the worker when
        none runs and waits for the models. Raises :class:`WorkerError`."""
        deadline = self._clock() + timeout
        with self._request_lock:
            self.start()
            with self._cond:
                run = self._running
                while run is not None and run.state == "loading":
                    self._check_load_time()
                    remaining = deadline - self._clock()
                    if run.state != "loading":
                        break
                    if remaining <= 0:
                        raise WorkerError("loading")
                    self._cond.wait(min(remaining, 1.0))
                if run is None or run.state != "ready":
                    raise WorkerError(self.failure or "load", error=self.failure_error)
                self._next_id += 1
                request_id = self._next_id
                line = json.dumps({**payload, "id": request_id}, ensure_ascii=True).encode("ascii")
            try:
                run.proc.stdin.write(line + b"\n")
                run.proc.stdin.flush()
            except (OSError, ValueError):
                with self._cond:
                    self._end(run, "died")
                raise WorkerError("died") from None
            with self._cond:
                while request_id not in run.replies:
                    if run.state == "ended":
                        raise WorkerError("died")
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        self._end(run, "timeout")
                        raise WorkerError("timeout")
                    self._cond.wait(min(remaining, 1.0))
                reply = run.replies.pop(request_id)
            if reply.get("ok") is True:
                return reply.get("result")
            raise WorkerError("model", stage=_safe_name(reply.get("stage")),
                              error=_safe_name(reply.get("error")))


# --------------------------------------------------------------------------- the service

_STAGES = {"layout": "finding the reactions", "molecule": "reading a molecule",
           "image": "opening the picture", "request": "taking the request"}


class Recognition:
    """What the host uses: status, recognition, idle stop, shutdown.

    ``command`` (tests) replaces the real worker command; the part then counts
    as installed."""

    def __init__(self, home: Path | None = None, *, command: Sequence[str] | None = None,
                 idle_seconds: float = IDLE_STOP_S, load_timeout: float = LOAD_TIMEOUT_S):
        self._home = Path(home) if home is not None else None
        self._command = list(command) if command is not None else None
        self.idle_seconds = idle_seconds
        self.load_timeout = load_timeout
        self._lock = threading.RLock()
        self._worker: Worker | None = None
        self._timer: threading.Timer | None = None
        self._generation = 0              # counts _touch calls: names the newest timer
        self._busy = 0
        self._closed = False

    @property
    def home(self) -> Path:
        return self._home if self._home is not None else recognition_home()

    @property
    def worker(self) -> Worker | None:
        return self._worker

    def _installed(self) -> dict[str, Any]:
        if self._command is not None:
            return {"installed": True, "home": str(self.home), "problem": None}
        return install_state(self.home)

    def _get_worker(self) -> Worker:
        home = self.home
        command = self._command if self._command is not None else [
            str(env_python(home)), "-I", str(worker_script()), "--models", str(models_dir(home))]
        with self._lock:
            if self._worker is None or self._worker.command != [str(c) for c in command]:
                if self._worker is not None:
                    self._worker.stop()
                cwd = home if self._command is None else None
                self._worker = Worker(command, cwd=cwd, load_timeout=self.load_timeout)
            return self._worker

    # ---- idle stop

    def _touch(self) -> None:
        """The recognition was used (or asked for) just now: the idle time
        starts again."""
        with self._lock:
            self._generation += 1
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.idle_seconds, self._idle_check,
                                          args=(self._generation,))
            self._timer.daemon = True
            self._timer.start()

    def _idle_check(self, generation: int) -> None:
        """The timer of the ``generation``-th use has run out. Only the newest
        timer stops the worker. No clock is asked here on purpose: a timer may
        fire a few milliseconds early by another clock (seen on Windows), and
        an "it is not yet time" answer would leave the worker running for
        good, since nothing would look again."""
        with self._lock:
            if generation != self._generation or self._busy or self._worker is None:
                return                    # a request re-arms the timer when it ends
            worker = self._worker
        if worker.state in ("loading", "ready"):
            _log().info("recognition worker stopped after %.0f s without use", self.idle_seconds)
        worker.stop()

    # ---- operations

    def status(self, start: bool = False) -> dict[str, Any]:
        """``recognition_status``. Never raises."""
        installed = self._installed()
        if not installed["installed"]:
            detail = NOT_INSTALLED
            if installed["problem"] and Path(installed["home"] or "").exists():
                detail = (f"The recognition part in {installed['home']} is not complete: "
                          f"{installed['problem']}. Run '{INSTALL_COMMAND}' again; it continues "
                          "where it stopped.")
            detail += how_to_run_the_installer()
            return {"installed": False, "state": "not_installed", "detail": detail}
        worker = self._get_worker()
        if start:
            if worker.state in ("stopped", "failed"):
                try:
                    worker.start()
                    _log().info("recognition worker started (pid %s)", worker.pid)
                except WorkerError:
                    pass
            # Asking for it is using it, also when it already runs: a capture
            # page that was just told "ready" must not lose the worker to the
            # idle stop a moment later.
            self._touch()
        state = worker.state
        result: dict[str, Any] = {"installed": True, "state": state}
        seconds = worker.load_seconds
        if seconds is not None and state in ("loading", "ready"):
            result["load_seconds"] = seconds
        if state == "failed":
            result["detail"] = self._failure_text(WorkerError(worker.failure or "load",
                                                              error=worker.failure_error))
        return result

    def _failure_text(self, exc: WorkerError) -> str:
        name = f" ({exc.error})" if exc.error else ""
        again = f" If it happens again, run '{INSTALL_COMMAND}' once more to repair it."
        if exc.kind == "start":
            return f"The recognition program could not be started{name}." + again
        if exc.kind == "load":
            if exc.error == "LoadTimeout":
                return ("The recognition models did not finish loading within "
                        f"{self.load_timeout:.0f} seconds." + again)
            return f"The recognition models could not be loaded{name}." + again
        if exc.kind == "loading":
            return ("The recognition models are still loading (the first run after the "
                    "installation can take a few minutes). Try again in a moment.")
        if exc.kind == "timeout":
            return ("The recognition gave no answer in time and was stopped. Try a smaller "
                    "selection.")
        if exc.kind == "died":
            return ("The recognition program ended unexpectedly, twice (the PC may be short of "
                    "memory: it needs about 2.5 GB). Try again, or try a smaller selection.")
        stage = _STAGES.get(exc.stage or "", "reading the picture")
        return f"The recognition failed while {stage}{name}. Try another selection."

    def recognize(self, image: str, mode: str,
                  timeout: float = float(protocol.RECOGNIZE_TIMEOUT_S)) -> dict[str, Any]:
        """``recognize``. Raises ``image_invalid``, ``recognition_not_installed``
        or ``recognition_failed``."""
        started = time.monotonic()
        check_png(image)
        self._closed = False
        if not self._installed()["installed"]:
            raise RegistryError(self.status()["detail"], code="recognition_not_installed")
        worker = self._get_worker()
        payload = {"op": "recognize", "mode": mode, "image": image}
        with self._lock:
            self._busy += 1
        try:
            try:
                raw = self._ask(worker, payload, started + timeout)
            except WorkerError as exc:
                _log().warning("recognition failed: %s stage=%s error=%s", exc.kind, exc.stage,
                               exc.error)
                if exc.kind == "model" and exc.stage == "image":
                    raise _image_invalid("The picture could not be opened as a PNG image.") \
                        from None
                raise _failed(self._failure_text(exc)) from None
        finally:
            with self._lock:
                self._busy -= 1
                if not self._closed:
                    self._touch()
        with _chemistry:
            return build_result(raw, mode, time.monotonic() - started)

    def _ask(self, worker: Worker, payload: dict[str, Any], deadline: float) -> Any:
        """One request, with ONE restart when the worker died during it."""
        try:
            return worker.request(payload, max(deadline - time.monotonic(), 0.001))
        except WorkerError as exc:
            if exc.kind != "died" or self._closed:      # no new worker while the host ends
                raise
        _log().warning("recognition worker ended during a request; restarting it once")
        return worker.request(payload, max(deadline - time.monotonic(), 0.001))

    def shutdown(self) -> None:
        """Stop the worker and the idle timer. Never raises."""
        with self._lock:
            self._closed = True
            self._generation += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            worker, self._worker = self._worker, None
        if worker is not None:
            worker.stop()


_default: Recognition | None = None
_default_lock = threading.Lock()
#: One structure job at a time. The host answers the short capture operations
#: while a ``recognize`` waits for the worker (host.serve), so two threads can
#: get here; RDKit's log switch (``BlockLogs``) is not made for that.
_chemistry = threading.Lock()


def default() -> Recognition:
    """The one :class:`Recognition` of this process (made at first use)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Recognition()
            atexit.register(shutdown)
        return _default


def shutdown() -> None:
    """Stop the worker of this process, if any. Called when the host ends."""
    global _default
    with _default_lock:
        current, _default = _default, None
    if current is not None:
        current.shutdown()


# --------------------------------------------------------------------------- ChemDraw

#: The folder inside the user's temporary folder that gets the documents.
CHEMDRAW_FOLDER = "ChemLinker"
_MAX_NAME_TRIES = 999


def chemdraw_folder() -> Path:
    return Path(tempfile.gettempdir()) / CHEMDRAW_FOLDER


def _open_file(path: str, opener: Callable[[str], None] | None) -> tuple[bool, str | None]:
    """Like ``RegistryService._open_route_file``: ask the system to open the
    document, outside the browser's job on Windows so ChemDraw outlives the
    host. Returns ``(opened, why not)``; never raises."""
    from . import service

    if os.environ.get(service.NO_OPEN_ENV, "").strip():
        return False, ("The file was not opened because opening files is switched off on this "
                       f"PC ({service.NO_OPEN_ENV} is set). Open it with ChemDraw yourself.")
    if opener is None:
        if os.name == "nt" and getattr(os, "startfile", None) is not None:
            opener = service.open_outside_job
        elif sys.platform == "darwin":
            opener = service.open_with_system
        else:
            return False, ("The file was written but not opened: files are only opened "
                           "automatically on Windows and macOS. Open it with ChemDraw yourself.")
    try:
        opener(path)
    except Exception as exc:  # noqa: BLE001 - not opening is never an error
        reason = str(exc).strip() or type(exc).__name__
        return False, (f"The file was written, but it could not be opened ({reason}). Is "
                       "ChemDraw installed on this computer?")
    return True, None


def reaction_to_chemdraw(steps: Sequence[dict[str, Any]], *, folder: Path | None = None,
                         opener: Callable[[str], None] | None = None,
                         now: Callable[[], datetime] = datetime.now) -> dict[str, Any]:
    """``reaction_to_chemdraw``: write ``steps`` as a NEW ChemDraw document
    ``reaction_<local time>.cdxml`` in the user's temporary folder and ask the
    system to open it. Returns ``{"path", "opened"}`` and ``"message"`` when
    there is something to say."""
    from . import cdxml
    from .routes import read_molfile

    drawn = [cdxml.ReactionStep(tuple(step["reactants"]), tuple(step.get("agents") or ()),
                                tuple(step["products"]), tuple(step.get("conditions") or ()))
             for step in steps]
    unreadable = sum(1 for step in drawn for molfile in step.reactants + step.agents + step.products
                     if read_molfile(molfile) is None)
    text = cdxml.render_reaction(drawn)

    base = Path(folder) if folder is not None else chemdraw_folder()
    stem = f"reaction_{now().strftime('%Y-%m-%d_%H%M%S')}"
    path = None
    try:
        base.mkdir(parents=True, exist_ok=True)
        for n in range(1, _MAX_NAME_TRIES + 1):
            candidate = base / (f"{stem}.cdxml" if n == 1 else f"{stem}_{n}.cdxml")
            try:
                with open(candidate, "x", encoding="utf-8", newline="") as handle:
                    handle.write(text)
            except FileExistsError:
                continue
            path = candidate
            break
    except OSError as exc:
        raise RegistryError(f"The ChemDraw file could not be written in {base}: "
                            f"{exc.strerror or type(exc).__name__}.", code="storage_error") from None
    if path is None:
        raise RegistryError(f"The ChemDraw file could not be written: {base} already holds "
                            f"{_MAX_NAME_TRIES} files named {stem}.", code="storage_error")

    opened, why = _open_file(str(path), opener)
    notes = [why] if why else []
    if unreadable:
        notes.append(f"{unreadable} structure(s) could not be read and are shown as '?'.")
    result: dict[str, Any] = {"path": str(path), "opened": opened}
    if notes:
        result["message"] = " ".join(notes)
    return result


# --------------------------------------------------------------------------- host operations

def op_recognition_status(params: dict[str, Any]) -> dict[str, Any]:
    return default().status(start=bool(params.get("start")))


def op_recognize(params: dict[str, Any]) -> dict[str, Any]:
    return default().recognize(params["image"], params["mode"])


def op_describe_structures(params: dict[str, Any]) -> dict[str, Any]:
    with _chemistry:
        return describe_structures(params["structures"])


def op_reaction_to_chemdraw(params: dict[str, Any]) -> dict[str, Any]:
    with _chemistry:
        return reaction_to_chemdraw(params["steps"])


#: Handlers of ``protocol.CAPTURE_OPS``: ``params -> result``. No registry.
OPERATIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "recognition_status": op_recognition_status,
    "recognize": op_recognize,
    "describe_structures": op_describe_structures,
    "reaction_to_chemdraw": op_reaction_to_chemdraw,
}
