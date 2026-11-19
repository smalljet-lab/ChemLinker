"""Compound folders on this PC: one folder per stereo variant.

Every ``CORE_STEREO`` gets the folder ``<files folder>/<CORE_STEREO>``
(``files.folder`` in ``config.toml``, see :mod:`compound_registry.config`).
The registry makes it when an ID is created and puts the structure in it as
``<CORE_STEREO>.mol``; other programs (the MestReNova add-on) save spectra
into it. See "Compound folders and spectra" in :mod:`compound_registry.protocol`.

This module holds the file-system side only and never touches the registry
database:

* :func:`folder_name` / :func:`compound_folder` - where the folder of a
  ``CORE_STEREO`` is (None when its text cannot be a folder name);
* :func:`ensure_folder` - make the folder (and the ``.mol`` file) if missing.
  It never raises for file-system problems and never overwrites a file;
* :func:`list_files` - the regular files lying directly in a folder;
* :func:`resolve_pdf_name` / :func:`check_pdf` / :func:`read_pdf_chunk` -
  serve one PDF piece by piece, refusing anything that is not a PDF;
* :func:`resolve_recorded` - where a file recorded with a spectrum is now
  (the files folder may have been moved since).
"""

from __future__ import annotations

import base64
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from .errors import RegistryError
from .protocol import MAX_FILE_NAME, MAX_FOLDER_FILES, MAX_PDF_BYTES, PDF_CHUNK_BYTES

__all__ = ["MAX_FOLDER_NAME", "FolderResult", "folder_name", "compound_folder", "ensure_folder",
           "list_files", "resolve_pdf_name", "check_pdf", "pdf_readable", "read_pdf_chunk",
           "is_pdf_name", "same_path", "resolve_recorded"]

#: Longest CORE_STEREO text that is used as a folder name.
MAX_FOLDER_NAME = 100

_FOLDER_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_WINDOWS_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL",
                               *(f"COM{i}" for i in range(10)),
                               *(f"LPT{i}" for i in range(10))})
_PDF_MAGIC = b"%PDF-"


def _unavailable(message: str) -> RegistryError:
    return RegistryError(message, code="file_unavailable")


def _invalid(message: str) -> RegistryError:
    return RegistryError(message, code="invalid_argument")


def _reason(exc: BaseException) -> str:
    """An OS error in plain words (without Python's ``[WinError 5]`` prefix)."""
    text = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
    return str(text).rstrip(".")


# --------------------------------------------------------------------------- names

def folder_name(core_stereo_id: object) -> str | None:
    """The folder name of a ``CORE_STEREO``, or None when its text cannot be
    used as one: only ASCII letters, digits, ``_``, ``-`` and ``.``; not
    starting or ending with a dot; not a Windows device name (``CON``,
    ``NUL``, ``COM1`` ..., also with an extension); at most
    :data:`MAX_FOLDER_NAME` characters. (The ID format is configurable, so
    an ID can contain characters a folder name cannot.)"""
    if not isinstance(core_stereo_id, str):
        return None
    name = core_stereo_id
    if not name or len(name) > MAX_FOLDER_NAME or not _FOLDER_NAME.fullmatch(name):
        return None
    if name[0] in ". " or name[-1] in ". ":
        return None
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        return None
    return name


def compound_folder(files_dir: Path | str, core_stereo_id: object) -> Path | None:
    """``<files_dir>/<CORE_STEREO>`` (absolute), or None when the ID cannot
    be a folder name (see :func:`folder_name`). Nothing is created or read."""
    name = folder_name(core_stereo_id)
    if name is None:
        return None
    return Path(os.path.abspath(Path(files_dir).expanduser())) / name


def is_pdf_name(name: str) -> bool:
    """True when a file name ends in ``.pdf`` (any case)."""
    return name.lower().endswith(".pdf")


def same_path(a: Path | str, b: Path | str) -> bool:
    """True when two absolute paths name the same place (case ignored where
    the system ignores it); no file-system access."""
    return (os.path.normcase(os.path.abspath(str(a)))
            == os.path.normcase(os.path.abspath(str(b))))


# --------------------------------------------------------------------------- making a folder

@dataclass(frozen=True)
class FolderResult:
    """Outcome of :func:`ensure_folder`. ``path`` None: there is no folder
    (``warning`` says why). ``created``: this call made the folder.
    ``warning`` may also be set when the folder exists (the ``.mol`` file
    could not be written)."""

    path: Path | None
    created: bool
    warning: str | None


def ensure_folder(files_dir: Path | str, core_stereo_id: str, molfile: str | None) -> FolderResult:
    """Make sure the folder of ``core_stereo_id`` exists, and that it holds
    ``<CORE_STEREO>.mol`` with ``molfile``.

    The folder is made with its parents if missing. The molfile is written
    only if no file of that name exists yet (exclusive create): an existing
    file is never overwritten, nothing in the folder is ever changed or
    removed. Never raises for file-system problems (no permission, a file in
    the way, disk full, path too long, unreachable network folder): the
    result's ``warning`` says in plain words what went wrong. Not being able
    to write the molfile is a warning only; the folder still counts.
    """
    name = folder_name(core_stereo_id)
    if name is None:
        return FolderResult(None, False,
                            f"The ID {core_stereo_id!r} cannot be used as a folder name, so no "
                            "folder was made for this compound.")
    try:
        base = Path(os.path.abspath(Path(files_dir).expanduser()))
    except (OSError, ValueError, RuntimeError) as exc:
        return FolderResult(None, False, f"The folder for {name} could not be made because the "
                                         f"compound folder setting is unusable: {_reason(exc)}.")
    folder = base / name
    try:
        base.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        return FolderResult(None, False, f"The folder for {name} could not be made, because "
                                         f"{base} could not be made or used: {_reason(exc)}.")
    created = False
    try:
        folder.mkdir()
        created = True
    except FileExistsError:
        if not folder.is_dir():
            return FolderResult(None, False, f"The folder for {name} could not be made: a file "
                                             f"named {folder} is in the way.")
    except (OSError, ValueError) as exc:
        return FolderResult(None, False,
                            f"The folder {folder} could not be made: {_reason(exc)}.")
    return FolderResult(folder, created, _write_molfile(folder, name, molfile))


def _write_molfile(folder: Path, name: str, molfile: str | None) -> str | None:
    """Write ``<name>.mol`` unless it exists; a warning text on failure."""
    if not molfile or not molfile.strip():
        return None
    target = folder / f"{name}.mol"
    try:
        handle = open(target, "x", encoding="utf-8", newline="")
    except FileExistsError:
        return None
    except (OSError, ValueError) as exc:
        return (f"The folder {folder} is there, but the structure file {target.name} could "
                f"not be written: {_reason(exc)}.")
    try:
        with handle:
            handle.write(molfile if molfile.endswith("\n") else molfile + "\n")
    except (OSError, ValueError) as exc:
        # This call created the file, so a half-written one may be removed.
        try:
            target.unlink()
        except OSError:
            pass
        return (f"The folder {folder} is there, but the structure file {target.name} could "
                f"not be written: {_reason(exc)}.")
    return None


# --------------------------------------------------------------------------- listing

def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_files(folder: Path | str, limit: int = MAX_FOLDER_FILES
               ) -> tuple[list[dict[str, Any]], bool, str | None]:
    """``(files, truncated, warning)``: the regular files lying directly in
    ``folder`` (subfolders are neither listed nor entered), sorted by name
    ignoring case, at most ``limit`` of them (``truncated`` true when there
    are more). Each is ``{"name", "size", "modified", "is_pdf"}`` with
    ``modified`` as ISO 8601 UTC (``2026-11-18T09:30:00Z``). Entries that
    cannot be examined are skipped. A folder that cannot be read gives no
    files and a warning; never raises for file-system problems."""
    found: list[dict[str, Any]] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    info = entry.stat()          # a link to a file counts as that file
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    modified = _iso_utc(info.st_mtime)
                except (OSError, ValueError, OverflowError):
                    continue
                found.append({"name": entry.name, "size": info.st_size, "modified": modified,
                              "is_pdf": is_pdf_name(entry.name)})
    except FileNotFoundError:
        return [], False, None
    except (OSError, ValueError) as exc:
        return [], False, f"The files in {folder} could not be listed: {_reason(exc)}."
    found.sort(key=lambda f: (f["name"].casefold(), f["name"]))
    return found[:limit], len(found) > limit, None


# --------------------------------------------------------------------------- PDFs

def resolve_pdf_name(folder: Path | str, name: object) -> Path:
    """The file ``name`` directly in ``folder``. ``name`` must be a plain
    file name (no ``/``, ``\\`` or ``:``, not ``.`` or ``..``, no control
    characters, at most :data:`protocol.MAX_FILE_NAME` characters), else
    ``invalid_argument``. The real path (links resolved) must lie directly in
    the real ``folder``, so a link cannot lead outside it
    (``file_unavailable``). Whether the file exists is not checked here."""
    if not isinstance(name, str) or not name.strip():
        raise _invalid("A file name is required.")
    if (len(name) > MAX_FILE_NAME or name in (".", "..")
            or any(ch in "/\\:" or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name)):
        raise _invalid(f"{name[:80]!r} is not a plain file name.")
    base = Path(os.path.abspath(Path(folder)))
    try:
        real_base = os.path.realpath(base)
        real = Path(os.path.realpath(base / name))
    except (OSError, ValueError) as exc:
        raise _unavailable(f"The PDF {name} in {base} cannot be opened: {_reason(exc)}.") from None
    if not same_path(real.parent, real_base):
        raise _unavailable(f"The file {name} in {base} leads to a place outside that folder, so "
                           "it is not shown.")
    return base / name


def resolve_recorded(files_dir: Path | str, core_stereo_id: object, stored: object) -> Path | None:
    """Where a file recorded with a spectrum (``mnova_file``, ``pdf_file``)
    is now. The record holds the full path the file had when it was saved.
    When nothing is there any more (the files folder was moved, or belongs to
    another PC or user), the file of the same name directly in the compound's
    folder ``<files_dir>/<CORE_STEREO>`` is taken instead, under the folder
    checks of :func:`resolve_pdf_name`.

    Returns the recorded path when it is a full path and neither place has
    the file, so that a message can name it; the place in the compound's
    folder for a record that is only a file name; None for an empty record
    or when no place can be told. Whether the file is a PDF is not checked."""
    if not isinstance(stored, str) or not stored.strip():
        return None
    text = stored.strip()
    recorded: Path | None = Path(text)
    try:
        if not recorded.is_absolute():
            recorded = None
        elif recorded.exists():
            return recorded
    except (OSError, ValueError):
        pass
    moved: Path | None = None
    folder = compound_folder(files_dir, core_stereo_id)
    if folder is not None:
        try:
            # PureWindowsPath splits at "\" and "/", so a path written on
            # Windows gives its file name on every system.
            moved = resolve_pdf_name(folder, PureWindowsPath(text).name)
            if moved.exists():
                return moved
        except (RegistryError, OSError, ValueError):
            moved = None
    return recorded if recorded is not None else moved


def _open_checked(path: Path) -> tuple[Any, int]:
    """Open ``path`` after every check of :func:`check_pdf`; returns (open
    binary file positioned after the first bytes, size). Raises
    ``file_unavailable`` with a plain-words message naming the file."""
    text = str(path)
    if not path.is_absolute():
        raise _unavailable(f"The PDF {text} is not given as a full path, so it cannot be found.")
    if not is_pdf_name(path.name):
        raise _unavailable(f"The file {text} is not a PDF (its name does not end in .pdf).")
    try:
        info = os.stat(path)
    except FileNotFoundError:
        raise _unavailable(f"The PDF {text} is not there any more.") from None
    except (OSError, ValueError) as exc:
        raise _unavailable(f"The PDF {text} cannot be reached: {_reason(exc)}.") from None
    if not stat.S_ISREG(info.st_mode):
        raise _unavailable(f"{text} is not a file, so it cannot be shown as a PDF.")
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        raise _unavailable(f"The PDF {text} is not there any more.") from None
    except (OSError, ValueError) as exc:
        raise _unavailable(f"The PDF {text} cannot be opened: {_reason(exc)}.") from None
    try:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise _unavailable(f"{text} is not a file, so it cannot be shown as a PDF.")
        size = info.st_size
        if size > MAX_PDF_BYTES:
            raise _unavailable(
                f"The PDF {text} is too large to show ({size / 1024 / 1024:.0f} MB, limit "
                f"{MAX_PDF_BYTES // 1024 // 1024} MB).")
        if handle.read(len(_PDF_MAGIC)) != _PDF_MAGIC:
            raise _unavailable(f"The file {text} is not a PDF (it does not start like one).")
    except RegistryError:
        handle.close()
        raise
    except (OSError, ValueError) as exc:
        handle.close()
        raise _unavailable(f"The PDF {text} cannot be read: {_reason(exc)}.") from None
    return handle, size


def check_pdf(path: Path | str) -> int:
    """The size of the PDF at ``path`` if :func:`read_pdf_chunk` would serve
    it now: an absolute path, a regular file whose name ends in ``.pdf``, at
    most :data:`protocol.MAX_PDF_BYTES` long, starting with ``%PDF-``.
    Otherwise ``file_unavailable`` with a message naming the file."""
    handle, size = _open_checked(Path(path))
    handle.close()
    return size


def pdf_readable(path: object) -> bool:
    """True when ``path`` (text) passes :func:`check_pdf` right now."""
    if not isinstance(path, str) or not path.strip():
        return False
    try:
        check_pdf(Path(path))
    except RegistryError:
        return False
    return True


def read_pdf_chunk(path: Path | str, offset: int = 0,
                   chunk_bytes: int = PDF_CHUNK_BYTES) -> dict[str, Any]:
    """One piece of the PDF at ``path``: ``{"name", "size", "offset", "data",
    "eof"}`` with ``data`` the base64 text of at most ``chunk_bytes`` bytes
    starting at ``offset``. The checks of :func:`check_pdf` run on every
    call. ``offset`` beyond ``size`` is ``invalid_argument``. ``eof`` is true
    when this piece reaches the end of the file; if ``size`` differs between
    calls, the file changed and the reading must start again."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise _invalid(f"offset must be a whole number >= 0, got {offset!r}.")
    path = Path(path)
    handle, size = _open_checked(path)
    with handle:
        if offset > size:
            raise _invalid(f"offset {offset} lies beyond the end of {path.name} ({size} bytes); "
                           "the file may have changed: start again at 0.")
        try:
            handle.seek(offset)
            data = handle.read(max(0, min(chunk_bytes, size - offset)))
        except (OSError, ValueError) as exc:
            raise _unavailable(f"The PDF {path} cannot be read: {_reason(exc)}.") from None
    return {"name": path.name, "size": size, "offset": offset,
            "data": base64.b64encode(data).decode("ascii"),
            "eof": offset + len(data) >= size}
