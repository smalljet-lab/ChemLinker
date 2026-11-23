"""Bridge between the MestReNova add-on and the registry.

The add-on (``mnova_addon/compoundRegistry.qs``, a script that runs inside
MestReNova) cannot read the registry itself. It starts this module as a
subprocess and reads one JSON document from stdout::

    python -m compound_registry.mnova propose --text-file TITLE.txt --image-dir DIR
    python -m compound_registry.mnova search --data-file QUERY.json --image-dir DIR
    python -m compound_registry.mnova record --data-file SPECTRA.json
    python -m compound_registry.mnova folder --data-file COMPOUND.json
    python -m compound_registry.mnova library --data-file QUERY.json --image-dir DIR
    python -m compound_registry.mnova install [--json]
    python -m compound_registry.mnova uninstall [--json]
    python -m compound_registry.mnova status [--json]

The first five are the calls of the add-on. Each of them also needs
``--addon-version VERSION`` with the version of this program (see below);
without it the call is refused with ``addon_outdated``.

``propose`` is read-only. It looks in the text of an NMR title (what the
chemist typed as a comment at the spectrometer, plus whatever the instrument
added) for the reference of an experiment the registry knows (``ABC-006`` in
``Nummer ABC-006-FC2-concentrated``) and returns the compounds registered for
that experiment, each with a PNG drawing. Choosing between them is always left
to the chemist: nothing here guesses which product a sample is.

``search`` is read-only too. It is for a spectrum whose title does not name
the experiment: the chemist types an experiment number (``ABC-006``, ``006``)
or a compound ID (``EX0001_01_001``, ``EX0001``) and gets the matching
compounds in the same form as ``propose``.

``record`` stores what the add-on read from the spectra of a linked document
(multiplet report, acquisition parameters, the saved ``.mnova`` and ``.pdf``
files) with the batch, see :meth:`RegistryService.record_spectra`.

``folder`` answers where the documents of a compound are saved: the folder
the registry keeps for it (``<files folder>/<CORE_STEREO>``, see
:mod:`compound_registry.files`), made now if it is missing.

The registry is the one of the browser extension: ``--home``, else the host
configuration written by ``compound-registry install-host``, else
``$COMPOUND_REGISTRY_HOME`` / the default folder. It is never created here.

The add-on and this program must be of the same version
---------------------------------------------------------
``install`` copies the add-on into MestReNova's own script folder and writes
three things into the copy: the Python that runs this module, the program
version, and (only when asked) a registry folder. The copy does not change
when the program is updated or its folder is moved, so:

* the add-on sends its version with every call (``--addon-version``) and the
  bridge refuses to work for an add-on of another version, with a message
  that says how to update it (``addon_outdated``);
* ``status`` tells whether MestReNova is on this PC, whether the add-on is
  installed, of which version, and whether it is the one this program would
  install now; ``install`` over an installed add-on brings it up to date and
  keeps its registry folder if one was written into it.

``install``, ``uninstall`` and ``status`` never ask anything. With ``--json``
they print one JSON document, ``{"ok": true, "result": {...}}`` or
``{"ok": false, "error": {"code", "message"}}``; without it, plain lines.
The add-on is installed automatically on Windows only; elsewhere
``--scripts-dir`` must name MestReNova's script folder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import __version__
from .errors import RegistryError

__all__ = ["find_experiments", "file_label", "propose", "search", "library", "record", "folder",
           "install", "uninstall", "status", "find_mestrenova", "check_addon_version", "main",
           "ADDON_FILES", "mnova_scripts_dir"]

#: Files copied into MestReNova's user script folder.
ADDON_FILES = ("compoundRegistry.qs", "compoundRegistry.mui")
#: Replaced in ``compoundRegistry.qs`` by the installer.
PYTHON_PLACEHOLDER = "@@COMPOUND_REGISTRY_PYTHON@@"
HOME_PLACEHOLDER = "@@COMPOUND_REGISTRY_HOME@@"
VERSION_PLACEHOLDER = "@@COMPOUND_REGISTRY_VERSION@@"
#: The script among :data:`ADDON_FILES` that carries the placeholders.
ADDON_SCRIPT = "compoundRegistry.qs"
#: The folders of MestReNova below ``%APPDATA%`` and below ``Program Files``.
_VENDOR_FOLDER = "Mestrelab Research S.L"
_PRODUCT = "MestReNova"

#: Most compounds one search returns.
MAX_SEARCH_RESULTS = 40
#: The most compounds one page of the library shows (the search narrows it).
MAX_LIBRARY = 300
_MAX_QUERY = 200
_MAX_LABEL = 60
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


# --------------------------------------------------------------------------- title parsing

def _ref_pattern(ref: str) -> re.Pattern[str] | None:
    """``ABC-006`` also matches ``abc-006``, ``ABC_006``, ``ABC 006`` and
    ``ABC006``, but neither ``ABC-0061`` nor ``XABC-006``."""
    parts = re.findall(r"[A-Za-z]+|[0-9]+", ref)
    if not parts:
        return None
    body = r"[-_ .]?".join(re.escape(p) for p in parts)
    tail = r"(?![0-9])" if parts[-1].isdigit() else r"(?![A-Za-z0-9])"
    return re.compile(r"(?<![A-Za-z0-9])" + body + tail, re.IGNORECASE)


def find_experiments(text: str, refs: Iterable[str]) -> list[dict[str, Any]]:
    """The experiment references from ``refs`` that occur in ``text``, in the
    order they occur: ``[{"experiment_ref", "sample", "line"}]``.

    ``sample`` is the whole word the reference sits in (``ABC-006-FC2-concentrated``),
    ``line`` the title line it was found on. Of two references that overlap in
    the text the longer one wins. A reference is reported once.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    hits: list[tuple[int, int, str]] = []
    for ref in sorted({r for r in refs if isinstance(r, str) and r.strip()}):
        pattern = _ref_pattern(ref.strip())
        if pattern is None:
            continue
        for m in pattern.finditer(text):
            hits.append((m.start(), m.end(), ref))
    hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))          # longest first
    kept: list[tuple[int, int, str]] = []
    for start, end, ref in hits:
        if all(end <= s or start >= e for s, e, _ in kept):
            kept.append((start, end, ref))
    kept.sort()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for start, end, ref in kept:
        if ref in seen:
            continue
        seen.add(ref)
        word_start = start
        while word_start > 0 and not text[word_start - 1].isspace():
            word_start -= 1
        word_end = end
        while word_end < len(text) and not text[word_end].isspace():
            word_end += 1
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line = text[line_start:line_end if line_end != -1 else len(text)].strip()
        out.append({"experiment_ref": ref, "sample": text[word_start:word_end].strip(".,;:"),
                    "line": line})
    return out


def file_label(text: str) -> str:
    """``text`` made safe as part of a Windows file name (may be empty)."""
    label = re.sub(r"[^A-Za-z0-9.+-]+", "_", text or "").strip("._ ")
    label = label[:_MAX_LABEL].rstrip("._ ")
    if label.upper() in _WINDOWS_RESERVED:
        label = "_" + label
    return label


# --------------------------------------------------------------------------- registry access

def _resolve_home(home: str | None) -> tuple[Path | None, str]:
    """``(home, source)``; home None means environment variable / default."""
    if home:
        return Path(home), "argument"
    from .install_host import default_target, read_host_config

    try:
        sidecar = default_target().sidecar_path
        if sidecar.is_file():
            pinned, _problem = read_host_config(sidecar)
            if pinned:
                return Path(pinned), "host-config"
    except (OSError, RegistryError):
        pass
    return None, "default"


def _open_service(home: Path | None) -> Any:
    """Open the EXISTING registry; never create one (``registry_missing``)."""
    from .install_host import locate_registry, registry_missing_error
    from .service import RegistryService

    location = locate_registry(home)
    if not location.exists:
        raise registry_missing_error(location)
    return RegistryService(location.config)


def _png(molblock: str, width: int, height: int) -> bytes | None:
    """PNG drawing with the same options as the SVG thumbnails; None on failure."""
    try:
        from rdkit import rdBase
        from rdkit.Chem.Draw import rdMolDraw2D

        from .render import _parse

        with rdBase.BlockLogs():
            mol = _parse(molblock)
            if mol is None:
                return None
            drawer = rdMolDraw2D.MolDraw2DCairo(width, height)
            opts = drawer.drawOptions()
            opts.addStereoAnnotation = True
            opts.addStereoGroupAnnotation = True
            prepared = rdMolDraw2D.PrepareMolForDrawing(
                mol, kekulize=True, addChiralHs=True, wedgeBonds=False)
            drawer.DrawMolecule(prepared)
            drawer.FinishDrawing()
            return drawer.GetDrawingText()
    except Exception:  # noqa: BLE001 - a missing picture must not hide the candidate
        return None


def _formula(molblock: str) -> str | None:
    try:
        from .properties import compute_properties

        return (compute_properties(molblock) or {}).get("sum_formula")
    except Exception:  # noqa: BLE001 - display only
        return None


def _by_experiment(svc: Any) -> dict[str, list[Any]]:
    """The batches that are not retired, by the number the chemist writes on
    the tube: the ELN code (``ABC-006``); ``experiment_ref`` (the ELN's
    internal key) only where a batch has no code."""
    by_ref: dict[str, list[Any]] = {}
    for b in svc.repo.iter_batches(include_retired=False):
        by_ref.setdefault(b.experiment_code or b.experiment_ref, []).append(b)
    return by_ref


def _image_folder(image_dir: str | Path | None) -> Path | None:
    if not image_dir:
        return None
    folder = Path(image_dir)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _candidate(svc: Any, b: Any, folder: Path | None, image_size: tuple[int, int]) -> dict[str, Any]:
    """One batch as the add-on shows it (with a PNG in ``folder`` if given)."""
    image = None
    if folder is not None:
        data = _png(b.molfile, *image_size)
        if data:
            path = folder / f"{file_label(b.full_id) or 'structure'}.png"
            path.write_bytes(data)
            image = str(path).replace("\\", "/")
    try:
        name = (svc.get_name(b.core_stereo_id).get("name") or {}).get("text")
    except RegistryError:
        name = None
    return {
        "full_id": b.full_id, "core_stereo_id": b.core_stereo_id,
        "experiment_ref": b.experiment_ref, "experiment_code": b.experiment_code,
        "product_index": b.product_index, "smiles": b.smiles,
        "inchikey": b.inchikey, "stereo_mode": b.stereo_mode,
        "formula": _formula(b.molfile), "name": name, "image": image,
    }


def propose(text: str, *, home: str | None = None, image_dir: str | Path | None = None,
            image_size: tuple[int, int] = (260, 180), service: Any = None) -> dict[str, Any]:
    """What the add-on shows for one spectrum title.

    ``{"home", "files_folder" (where the compound folders are), "matches": [{"experiment_ref" (the ELN code as registered), "sample", "line", "file_label",
    "candidates": [...]}], "known_experiments": N}``. A candidate is one batch
    that is not retired: ``full_id``, ``core_stereo_id``, ``experiment_code``, ``product_index``,
    ``smiles``, ``inchikey``, ``stereo_mode``, ``formula``, ``name`` and
    ``image`` (path of a PNG in ``image_dir``, or null).
    """
    own = service is None
    resolved, source = (None, "service") if not own else _resolve_home(home)
    svc = service if not own else _open_service(resolved)
    try:
        by_ref = _by_experiment(svc)
        folder = _image_folder(image_dir)
        matches = []
        for hit in find_experiments(text, by_ref):
            ordered = sorted(by_ref[hit["experiment_ref"]],
                             key=lambda b: (b.product_index, b.full_id))
            matches.append({**hit, "file_label": file_label(hit["sample"]),
                            "candidates": [_candidate(svc, b, folder, image_size)
                                           for b in ordered]})
        return {"home": str(svc.config.home), "home_source": source,
                "files_folder": str(svc.config.files_dir), "matches": matches,
                "known_experiments": len(by_ref)}
    finally:
        if own:
            svc.close()


def _plain(text: str) -> str:
    """Letters and digits only, lower case: ``ABC-006`` and ``abc 006`` are the same."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def search(query: str, *, text: str = "", home: str | None = None,
           image_dir: str | Path | None = None, image_size: tuple[int, int] = (260, 180),
           service: Any = None) -> dict[str, Any]:
    """The compounds a typed experiment number or compound ID stands for, for
    a spectrum whose title does not name its experiment. Read-only.

    ``query`` matches an experiment when it names it as a title would
    (``ABC-006``, ``abc006``); only when it names none that way, every
    experiment it is a part of the number of (``006``). It matches a batch when it is part of its ID (``EX0001``,
    ``0001_01``). Spaces, ``-``, ``_`` and case are ignored. Experiments
    come before single batches. At most :data:`MAX_SEARCH_RESULTS` compounds
    are returned (``truncated`` true when there were more).

    The result has the form of :func:`propose` plus ``query`` and
    ``truncated``. ``sample`` is empty (nothing was read from the title);
    ``file_label`` comes from the first line of ``text``, the dataset name
    the spectrometer gave, so that the saved files can be told apart.
    """
    if not isinstance(query, str) or not isinstance(text, str):
        raise RegistryError("The search text must be text.", code="invalid_argument")
    query = query.strip()
    if len(query) > _MAX_QUERY:
        raise RegistryError(f"The search text is too long (limit {_MAX_QUERY} characters).",
                            code="invalid_argument")
    own = service is None
    resolved, source = (None, "service") if not own else _resolve_home(home)
    svc = service if not own else _open_service(resolved)
    try:
        by_ref = _by_experiment(svc)
        plain = _plain(query)
        refs: list[str] = []
        if plain:
            refs = [hit["experiment_ref"] for hit in find_experiments(query, by_ref)]
            if not refs:
                refs = sorted(r for r in by_ref if plain in _plain(r))
        chosen: dict[str, list[Any]] = {ref: list(by_ref[ref]) for ref in refs}
        if plain:
            for ref in sorted(by_ref):
                for b in by_ref[ref]:
                    if ref not in refs and plain in _plain(b.full_id):
                        chosen.setdefault(ref, []).append(b)
        folder = _image_folder(image_dir)
        label = file_label(text.strip().split("\n", 1)[0]) if text.strip() else ""
        matches = []
        left = MAX_SEARCH_RESULTS
        total = sum(len(v) for v in chosen.values())
        for ref, batches in chosen.items():
            ordered = sorted(batches, key=lambda b: (b.product_index, b.full_id))[:left]
            left -= len(ordered)
            if ordered:
                matches.append({"experiment_ref": ref, "sample": "", "line": "",
                                "file_label": label,
                                "candidates": [_candidate(svc, b, folder, image_size)
                                               for b in ordered]})
        return {"home": str(svc.config.home), "home_source": source,
                "files_folder": str(svc.config.files_dir), "query": query,
                "matches": matches, "truncated": total > MAX_SEARCH_RESULTS,
                "known_experiments": len(by_ref)}
    finally:
        if own:
            svc.close()


def library(query: str = "", *, home: str | None = None, image_dir: str | Path | None = None,
            image_size: tuple[int, int] = (220, 150), service: Any = None) -> dict[str, Any]:
    """The compounds of the registry with the MestReNova documents saved in
    their folders, for opening the spectrum of a known compound next to one's
    own. Read-only: no folder is made, nothing is written to the registry.

    One entry per compound (stereo variant) that has a batch that is not
    retired, ordered by ID: ``core_stereo_id``, ``ids`` (its batches),
    ``experiments`` (their ELN numbers), ``name``, ``formula``, ``smiles``,
    ``image`` (PNG in ``image_dir``, or null), ``folder`` and ``documents``:
    the ``.mnova`` files lying directly in the folder, newest first, each
    ``{"name", "path", "modified"}``.

    ``query`` narrows the list: every word of it must be part of the ID, a
    batch ID, an experiment number, the name or the formula (spaces, ``-``,
    ``_`` and case are ignored inside a word). An empty query lists all. At
    most :data:`MAX_LIBRARY` compounds are returned (``truncated`` true when
    there were more; ``total`` is the number that matched).
    """
    if not isinstance(query, str):
        raise RegistryError("The search text must be text.", code="invalid_argument")
    query = query.strip()
    if len(query) > _MAX_QUERY:
        raise RegistryError(f"The search text is too long (limit {_MAX_QUERY} characters).",
                            code="invalid_argument")
    from . import files as files_mod

    own = service is None
    resolved, source = (None, "service") if not own else _resolve_home(home)
    svc = service if not own else _open_service(resolved)
    try:
        groups: dict[str, list[Any]] = {}
        for b in svc.repo.iter_batches(include_retired=False):
            groups.setdefault(b.core_stereo_id, []).append(b)
        words = [w for w in (_plain(part) for part in query.split()) if w]
        found = []
        for cs in sorted(groups):
            batches = sorted(groups[cs], key=lambda b: b.full_id)
            first = batches[0]
            try:
                name = (svc.get_name(cs).get("name") or {}).get("text")
            except RegistryError:
                name = None
            formula = _formula(first.molfile)
            experiments = sorted({b.experiment_code or b.experiment_ref for b in batches})
            ids = [b.full_id for b in batches]
            if words:
                hay = [_plain(t) for t in (cs, *ids, *experiments, name or "", formula or "")]
                if not all(any(w in h for h in hay) for w in words):
                    continue
            found.append((cs, first, ids, experiments, name, formula))
        folder = _image_folder(image_dir)
        compounds = []
        for cs, first, ids, experiments, name, formula in found[:MAX_LIBRARY]:
            image = None
            if folder is not None:
                data = _png(first.molfile, *image_size)
                if data:
                    path = folder / f"library-{file_label(cs) or 'structure'}.png"
                    path.write_bytes(data)
                    image = str(path).replace("\\", "/")
            documents: list[dict[str, Any]] = []
            where = files_mod.compound_folder(svc.config.files_dir, cs)
            if where is not None and where.is_dir():
                listed, _, _ = files_mod.list_files(where)
                documents = [{"name": f["name"], "path": str(where / f["name"]),
                              "modified": f["modified"]}
                             for f in listed if f["name"].lower().endswith(".mnova")]
                documents.sort(key=lambda d: d["modified"], reverse=True)
            compounds.append({"core_stereo_id": cs, "ids": ids, "experiments": experiments,
                              "name": name, "formula": formula, "smiles": first.smiles,
                              "image": image, "folder": str(where) if where is not None else None,
                              "documents": documents})
        return {"home": str(svc.config.home), "home_source": source,
                "files_folder": str(svc.config.files_dir), "query": query,
                "compounds": compounds, "total": len(found),
                "truncated": len(found) > MAX_LIBRARY}
    finally:
        if own:
            svc.close()


def record(payload: Any, *, home: str | None = None, service: Any = None) -> dict[str, Any]:
    """Store the spectra of a linked document: ``payload`` is
    ``{"full_id": ..., "spectra": [...]}`` as written by the add-on. Returns
    ``RegistryService.record_spectra`` plus ``home``."""
    if not isinstance(payload, dict):
        raise RegistryError("The spectrum data must be an object with full_id and spectra.",
                            code="invalid_argument")
    own = service is None
    svc = service if not own else _open_service(_resolve_home(home)[0])
    try:
        result = svc.record_spectra(payload.get("full_id"), payload.get("spectra"))
        return {**result, "home": str(svc.config.home)}
    finally:
        if own:
            svc.close()


def folder(payload: Any, *, home: str | None = None, service: Any = None) -> dict[str, Any]:
    """The folder the documents of a compound are saved in: ``payload`` is
    ``{"core_stereo_id": ...}``. The registry makes the folder (with the
    structure file) if it is missing. Returns ``{"core_stereo_id", "path",
    "created", "warning", "molfile" (the structure as registered, for pasting
    it on the spectrum; may be empty), "files_folder", "home"}``; ``file_unavailable``
    when there is no folder (the message says why), ``not_found`` for an
    unknown compound."""
    if not isinstance(payload, dict) or not isinstance(payload.get("core_stereo_id"), str):
        raise RegistryError("The folder request must be an object with core_stereo_id.",
                            code="invalid_argument")
    own = service is None
    svc = service if not own else _open_service(_resolve_home(home)[0])
    try:
        result = svc.make_folders(payload["core_stereo_id"])
        entry = result["folders"][0]
        if not entry.get("path"):
            raise RegistryError(entry.get("warning") or
                                f"There is no folder for {entry['core_stereo_id']}.",
                                code="file_unavailable")
        stereo = svc.repo.get_stereo(entry["core_stereo_id"])
        return {**entry, "molfile": (stereo.molfile if stereo is not None else "") or "",
                "files_folder": result["files_folder"], "home": str(svc.config.home)}
    finally:
        if own:
            svc.close()


# --------------------------------------------------------------------------- installation

def _unsupported() -> RegistryError:
    return RegistryError(
        "The MestReNova add-on is installed automatically on Windows only. On this system, "
        "give MestReNova's script folder with --scripts-dir.", code="mnova_unsupported")


def mnova_scripts_dir(appdata: str | Path | None = None) -> Path:
    """MestReNova's per-user script folder on Windows (searched at start-up,
    no admin rights). Without ``appdata`` it is taken from ``%APPDATA%``; on
    another system that is ``mnova_unsupported``, because where MestReNova
    looks for scripts there is not known."""
    if appdata is None:
        if sys.platform != "win32":
            raise _unsupported()
        appdata = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    return Path(appdata) / _VENDOR_FOLDER / _PRODUCT / "scripts"


def _addon_dir() -> Path:
    return Path(__file__).resolve().parent / "mnova_addon"


def _js_text(value: str) -> str:
    """``value`` as the inside of a double-quoted string of the add-on script
    (plain ASCII, so the script reads the same in every encoding)."""
    return json.dumps(value, ensure_ascii=True)[1:-1]


def _rendered(name: str, python: str, home: str, version: str) -> str:
    """The add-on file ``name`` as it is installed."""
    text = (_addon_dir() / name).read_text(encoding="utf-8")
    text = text.replace(PYTHON_PLACEHOLDER, _js_text(python.replace("\\", "/")))
    text = text.replace(HOME_PLACEHOLDER, _js_text(home.replace("\\", "/")))
    return text.replace(VERSION_PLACEHOLDER, _js_text(version))


def _installed_value(script: str, field: str) -> str | None:
    """What an installed add-on script holds in ``field`` of its settings
    (``python``, ``home``, ``version``); None when the field is missing,
    empty or was never filled in."""
    found = re.search(r'^[ \t]*' + field + r':[ \t]*"((?:[^"\\\n]|\\.)*)"', script, re.MULTILINE)
    if found is None:
        return None
    try:
        value = json.loads('"' + found.group(1) + '"')
    except ValueError:
        return None
    if not isinstance(value, str) or not value or value.startswith("@@"):
        return None
    return value


def _read_installed(target: Path) -> str | None:
    """The text of the installed add-on script in ``target``, or None."""
    try:
        return (target / ADDON_SCRIPT).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _uninstall_entries() -> list[tuple[str, str, str]]:
    """``(name, version, folder)`` of the programs Windows lists as installed
    whose name starts with MestReNova. Reads the Windows registry only."""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - not Windows
        return []
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    views = ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
             (winreg.HKEY_CURRENT_USER, 0))
    found: list[tuple[str, str, str]] = []
    for hive, view in views:
        try:
            root = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name) as entry:
                        display = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                        if not display.lower().startswith(_PRODUCT.lower()):
                            continue
                        values = []
                        for value_name in ("DisplayVersion", "InstallLocation"):
                            try:
                                values.append(str(winreg.QueryValueEx(entry, value_name)[0]))
                            except OSError:
                                values.append("")
                        found.append((display, values[0], values[1]))
                except OSError:
                    continue
    return found


def _program_dirs() -> list[Path]:
    """Where MestReNova is installed by default (Windows)."""
    if sys.platform != "win32":
        return []
    roots = [os.environ.get(name) for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")]
    return [Path(root) / _VENDOR_FOLDER / _PRODUCT for root in dict.fromkeys(roots) if root]


def find_mestrenova(*, appdata: str | Path | None = None,
                    program_dirs: Iterable[str | Path] | None = None,
                    entries: Iterable[tuple[str, str, str]] | None = None) -> dict[str, Any]:
    """Whether MestReNova is on this PC, without starting it:
    ``{"found", "version", "folder", "evidence"}``.

    ``evidence`` says what was found: ``"installed-programs"`` (the list of
    installed programs of Windows; ``version`` and ``folder`` come from it),
    ``"program-folder"`` (``MestReNova.exe`` in its default folder) or
    ``"user-folder"`` (only the folder MestReNova keeps for the user, so it
    ran here at some time); None with ``found`` false. ``version`` and
    ``folder`` are None when they are not known. The three arguments replace
    what is read from this PC (tests)."""
    for _name, version, location in (_uninstall_entries() if entries is None else entries):
        return {"found": True, "version": version.strip() or None,
                "folder": location.strip().rstrip("\\/") or None,
                "evidence": "installed-programs"}
    for folder in (_program_dirs() if program_dirs is None else program_dirs):
        try:
            if (Path(folder) / "MestReNova.exe").is_file():
                return {"found": True, "version": None, "folder": str(folder),
                        "evidence": "program-folder"}
        except OSError:
            continue
    try:
        if appdata is not None or sys.platform == "win32":
            if mnova_scripts_dir(appdata).parent.is_dir():
                return {"found": True, "version": None, "folder": None,
                        "evidence": "user-folder"}
    except OSError:
        pass
    return {"found": False, "version": None, "folder": None, "evidence": None}


def install(*, scripts_dir: str | Path | None = None, python: str | Path | None = None,
            home: str | None = None, keep_home: bool = True) -> dict[str, Any]:
    """Copy the add-on into MestReNova's user script folder and tell it which
    Python to start and which version it is. Installing over an installed
    add-on replaces it (this is how it is brought up to date).

    ``home`` writes a registry folder into the add-on; without it the add-on
    follows the registry of the browser extension (the host configuration
    written by ``compound-registry install-host``). A registry folder that an
    installed add-on already holds is kept unless ``keep_home`` is false.

    Returns ``{"installed": [paths], "scripts_dir", "python", "home",
    "version", "replaced" (an add-on was there before), "note"}``."""
    target = Path(scripts_dir) if scripts_dir is not None else mnova_scripts_dir()
    if home is not None and not Path(home).is_absolute():
        raise RegistryError(f"--home must be an absolute path, got {home!r}",
                            code="invalid_argument")
    exe = Path(python) if python is not None else Path(sys.executable)
    if exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").is_file():
        exe = exe.parent / "pythonw.exe"       # no console window flashing in MestReNova
    before = _read_installed(target)
    if home is None and keep_home and before is not None:
        home = _installed_value(before, "home")
    try:
        target.mkdir(parents=True, exist_ok=True)
        written = []
        for name in ADDON_FILES:
            text = _rendered(name, str(exe), home or "", __version__)
            (target / name).write_text(text, encoding="utf-8", newline="\n")
            written.append(str(target / name))
    except OSError as exc:
        raise RegistryError(f"Could not install the MestReNova add-on into {target}: {exc}",
                            code="mnova_install_failed") from exc
    return {"installed": written, "scripts_dir": str(target), "python": str(exe), "home": home,
            "version": __version__, "replaced": before is not None,
            "note": "Restart MestReNova to load the add-on."}


def uninstall(*, scripts_dir: str | Path | None = None) -> dict[str, Any]:
    """Remove the add-on's files from MestReNova's user script folder:
    ``{"removed": [paths], "scripts_dir"}``. Nothing else in the folder is
    touched; no installed add-on is not an error."""
    target = Path(scripts_dir) if scripts_dir is not None else mnova_scripts_dir()
    removed = []
    for name in ADDON_FILES:
        path = target / name
        try:
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        except OSError as exc:
            raise RegistryError(f"Could not remove {path}: {exc}",
                                code="mnova_install_failed") from exc
    return {"removed": removed, "scripts_dir": str(target)}


def status(*, scripts_dir: str | Path | None = None,
           mestrenova: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only: what is known about MestReNova and the add-on on this PC.

    ``supported``: the add-on can be installed here without naming the script
    folder (Windows). ``mestrenova``: see :func:`find_mestrenova` (given, or
    looked up). ``scripts_dir``: the folder looked at, None when there is
    none to look at. ``installed``: the add-on's script lies there.
    ``addon_version``: the program version that installed it (None for an
    add-on from before versions were written, or when not installed).
    ``program_version``: this program. ``python`` / ``python_exists``: the
    Python the add-on starts and whether it is still there. ``home``: the
    registry folder written into it, or None. ``current``: the installed
    files are exactly what ``install`` would write now and the Python exists,
    so nothing needs to be done. ``problems``: plain sentences saying what is
    wrong with an installed add-on (empty when ``current``)."""
    found = find_mestrenova() if mestrenova is None else mestrenova
    result: dict[str, Any] = {
        "supported": sys.platform == "win32", "mestrenova": found, "scripts_dir": None,
        "installed": False, "addon_version": None, "program_version": __version__,
        "python": None, "python_exists": False, "home": None, "current": False, "problems": []}
    if scripts_dir is None and sys.platform != "win32":
        return result
    target = Path(scripts_dir) if scripts_dir is not None else mnova_scripts_dir()
    result["scripts_dir"] = str(target)
    script = _read_installed(target)
    if script is None:
        return result
    python, home = _installed_value(script, "python"), _installed_value(script, "home")
    version = _installed_value(script, "version")
    problems: list[str] = []
    exists = False
    if python is None:
        problems.append("The installed add-on does not say which Python to start.")
    else:
        try:
            exists = Path(python).is_file()
        except OSError:
            exists = False
        if not exists:
            problems.append(f"The installed add-on starts {python}, which is not there any more "
                            "(the folder of the ChemLinker program was moved or removed).")
    if version != __version__:
        problems.append(
            f"The installed add-on is {_version_words(version)}, the ChemLinker "
            f"program is version {__version__}.")
    same = python is not None
    for name in ADDON_FILES:
        try:
            installed_text = (target / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            problems.append(f"{name} is missing in {target}.")
            same = False
            continue
        if same and installed_text != _rendered(name, python or "", home or "", __version__):
            same = False
    if not same and not problems:
        problems.append("The installed add-on differs from the one this program installs.")
    result.update({"installed": True, "addon_version": version, "python": python,
                   "python_exists": exists, "home": home,
                   "current": same and exists and not problems, "problems": problems})
    return result


def _version_words(version: str | None) -> str:
    return f"version {version}" if version else "from before versions were written into it"


def _install_command() -> str:
    """The command that installs the add-on, with the Python of this program
    in full: ``python`` alone is often not on the path, or is another one."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        exe = exe.parent / "python.exe"
    return f'"{exe}" -m compound_registry.mnova install'


def _update_hint() -> str:
    return ("Update the add-on: run the installer of ChemLinker again "
            f"(compound-registry setup), or run\n{_install_command()}\n"
            "Then restart MestReNova.")


def check_addon_version(addon_version: str | None) -> None:
    """Refuse (``addon_outdated``) to work for an add-on that another version
    of the program installed; the message says how to update it. None stands
    for an add-on from before it sent its version."""
    sent = addon_version.strip() if isinstance(addon_version, str) else ""
    if sent == __version__:
        return
    raise RegistryError(
        f"The MestReNova add-on is {_version_words(sent or None)}, the ChemLinker "
        f"program is version {__version__}.\n{_update_hint()}", code="addon_outdated")


# --------------------------------------------------------------------------- command line

_ADDON_VERSION_HELP = ("the program version that installed the calling add-on; the call is "
                       "refused when it is not the version of this program")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m compound_registry.mnova",
                                     description="MestReNova add-on bridge.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("propose", help="compounds of the experiment named in an NMR title")
    p.add_argument("--text-file", required=True, help="UTF-8 file with the title text")
    p.add_argument("--image-dir", help="folder for the structure pictures (PNG)")
    p.add_argument("--home", help="registry folder (default: the registry of the browser extension)")
    p.add_argument("--out", help="write the JSON answer to this file instead of stdout")
    p.add_argument("--addon-version", help=_ADDON_VERSION_HELP)
    q = sub.add_parser("search", help="compounds for a typed experiment number or ID")
    q.add_argument("--data-file", required=True, help="JSON file: {query, text}")
    q.add_argument("--image-dir", help="folder for the structure pictures (PNG)")
    q.add_argument("--home", help="registry folder (default: the registry of the browser extension)")
    q.add_argument("--out", help="write the JSON answer to this file instead of stdout")
    q.add_argument("--addon-version", help=_ADDON_VERSION_HELP)
    lib = sub.add_parser("library", help="the compounds and their saved MestReNova documents")
    lib.add_argument("--data-file", required=True, help="JSON file: {query}")
    lib.add_argument("--image-dir", help="folder for the structure pictures (PNG)")
    lib.add_argument("--home", help="registry folder (default: the registry of the browser extension)")
    lib.add_argument("--out", help="write the JSON answer to this file instead of stdout")
    lib.add_argument("--addon-version", help=_ADDON_VERSION_HELP)
    r = sub.add_parser("record", help="store the NMR data of a linked document")
    r.add_argument("--data-file", required=True, help="JSON file: {full_id, spectra: [...]}")
    r.add_argument("--home", help="registry folder (default: the registry of the browser extension)")
    r.add_argument("--out", help="write the JSON answer to this file instead of stdout")
    r.add_argument("--addon-version", help=_ADDON_VERSION_HELP)
    f = sub.add_parser("folder", help="the folder of a compound (made if missing)")
    f.add_argument("--data-file", required=True, help="JSON file: {core_stereo_id}")
    f.add_argument("--home", help="registry folder (default: the registry of the browser extension)")
    f.add_argument("--out", help="write the JSON answer to this file instead of stdout")
    f.add_argument("--addon-version", help=_ADDON_VERSION_HELP)
    i = sub.add_parser("install", help="install the add-on for the current user, or bring "
                                       "an installed one up to date")
    i.add_argument("--scripts-dir", help="MestReNova user script folder")
    i.add_argument("--home", help="write this registry folder into the add-on (default: keep "
                                  "the one an installed add-on holds, else follow the registry "
                                  "of the browser extension)")
    i.add_argument("--follow-extension", action="store_true",
                   help="drop a registry folder written into an installed add-on")
    i.add_argument("--python", help="the Python the add-on starts (default: this one)")
    i.add_argument("--json", action="store_true", help="print one JSON document")
    u = sub.add_parser("uninstall", help="remove the add-on")
    u.add_argument("--scripts-dir", help="MestReNova user script folder")
    u.add_argument("--json", action="store_true", help="print one JSON document")
    s = sub.add_parser("status", help="is MestReNova here, is the add-on installed and current")
    s.add_argument("--scripts-dir", help="MestReNova user script folder")
    s.add_argument("--json", action="store_true", help="print one JSON document")
    return parser


def _setup_lines(command: str, result: dict[str, Any]) -> list[str]:
    """What ``install``, ``uninstall`` and ``status`` print without --json."""
    if command == "install":
        lines = [f"MestReNova add-on {result['version']} "
                 f"{'updated' if result['replaced'] else 'installed'} in {result['scripts_dir']}",
                 f"It starts {result['python']}"]
        if result["home"]:
            lines.append(f"It uses the registry in {result['home']}")
        if not find_mestrenova()["found"]:
            lines.append("MestReNova was not found on this PC; it loads the add-on once it is "
                         "installed.")
        return lines + [result["note"]]
    if command == "uninstall":
        if not result["removed"]:
            return [f"No MestReNova add-on was installed in {result['scripts_dir']}"]
        return [f"Removed {path}" for path in result["removed"]]
    found = result["mestrenova"]
    if found["found"]:
        detail = ", ".join(x for x in (found["version"], found["folder"]) if x)
        lines = ["MestReNova   found" + (f" ({detail})" if detail else "")]
    else:
        lines = ["MestReNova   not found on this PC"]
    if result["scripts_dir"] is None:
        return lines + ["Add-on       not looked for: give MestReNova's script folder with "
                        "--scripts-dir"]
    if not result["installed"]:
        return lines + [f"Add-on       not installed (looked in {result['scripts_dir']})"]
    lines.append(f"Add-on       installed, {_version_words(result['addon_version'])}, in "
                 f"{result['scripts_dir']}")
    if result["current"]:
        return lines + ["             up to date"]
    return lines + [f"             {problem}" for problem in result["problems"]] + [
        "             to bring it up to date, run the installer of ChemLinker again",
        "             (compound-registry setup), or run",
        f"             {_install_command()}"]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    out_path = getattr(args, "out", None)
    setup = args.command in ("install", "uninstall", "status")
    try:
        if setup:
            if args.command == "install":
                result = install(scripts_dir=args.scripts_dir, python=args.python,
                                 home=args.home, keep_home=not args.follow_extension)
            elif args.command == "uninstall":
                result = uninstall(scripts_dir=args.scripts_dir)
            else:
                result = status(scripts_dir=args.scripts_dir)
        else:
            check_addon_version(args.addon_version)
            result = _answer(args)
        document: dict[str, Any] = {"ok": True, "result": result}
        code = 0
    except RegistryError as exc:
        document = {"ok": False, "error": exc.to_dict()}
        code = 1
    except OSError as exc:
        document = {"ok": False, "error": {"code": "io_error", "message": str(exc)}}
        code = 1
    except Exception as exc:  # noqa: BLE001 - the add-on must always get an answer it can show
        document = {"ok": False, "error": {"code": "internal_error",
                                           "message": f"{type(exc).__name__}: {exc}"}}
        code = 1
    if setup and not args.json:
        if code == 0:
            _print("\n".join(_setup_lines(args.command, document["result"])), sys.stdout)
        else:
            _print("error: " + document["error"]["message"], sys.stderr)
        return code
    text_out = json.dumps(document, ensure_ascii=True, indent=1)
    if out_path:
        try:
            Path(out_path).write_text(text_out, encoding="ascii")
            return code
        except OSError:
            code = 1           # the answer is still printed below, where there is a stdout
    _print(text_out, sys.stdout)
    return code


def _print(text: str, stream: Any) -> None:
    if stream is None:                     # pythonw.exe has neither stdout nor stderr
        return
    try:
        stream.write(text + "\n")
    except (OSError, ValueError, UnicodeError):
        pass


def _answer(args: argparse.Namespace) -> dict[str, Any]:
    """The result of one bridge command of the add-on."""
    if args.command == "propose":
        text = Path(args.text_file).read_text(encoding="utf-8-sig", errors="replace")
        return propose(text, home=args.home or None, image_dir=args.image_dir)
    try:
        payload = json.loads(Path(args.data_file).read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise RegistryError(f"Unreadable data file: {exc}", code="invalid_argument") from exc
    if args.command == "search":
        if not isinstance(payload, dict):
            raise RegistryError("The search request must be an object with query.",
                                code="invalid_argument")
        return search(payload.get("query"), text=payload.get("text") or "",
                      home=args.home or None, image_dir=args.image_dir)
    if args.command == "library":
        if not isinstance(payload, dict):
            raise RegistryError("The library request must be an object.",
                                code="invalid_argument")
        return library(payload.get("query") or "", home=args.home or None,
                       image_dir=args.image_dir)
    command = record if args.command == "record" else folder
    return command(payload, home=args.home or None)


if __name__ == "__main__":
    raise SystemExit(main())
