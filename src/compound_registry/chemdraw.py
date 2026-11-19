"""Bridge to the ChemDraw desktop program installed on the same PC.

ChemDraw can turn a structure into its systematic name. It does this locally
(unlike ChemDraw JS in the ELN, which calls the vendor's web service), and it
can be driven through its Windows automation interface. This module does only
that: hand ChemDraw one molfile, get back the name and the InChI of what
ChemDraw read. Whether that name may be stored is decided in ``naming``.

How it works (checked against ChemDraw 26.0)
---------------------------------------------------------
* The automation class is ``ChemDraw_x64.Application`` (``ChemDraw.Application``
  on older or 32-bit installations). Whether it exists is read from the
  Windows registry; ChemDraw is not started to find out.
* Every start through the interface is a NEW, HIDDEN ChemDraw process. A
  ChemDraw window the user has open is another process and is not touched.
* Windows PowerShell (part of Windows) runs ``chemdraw_name.ps1`` from this
  folder, which talks to ChemDraw and quits it again. No Python package is
  needed for this. The text of the script is handed over on the command line
  (``-EncodedCommand``), not as a file: Windows refuses script files on a PC
  with the default execution policy, and files that were downloaded.
* If the script has to be stopped (time-out), the hidden ChemDraw it started
  would stay behind. ``chemdraw_cleanup.ps1`` then stops it: only a ChemDraw
  that was not running before the call, that Windows started for automation
  (command line ``-Embedding``) and that has no window. A ChemDraw the user
  opened never matches.
* Setting the environment variable ``COMPOUND_REGISTRY_NO_CHEMDRAW`` switches
  all of this off ("not installed"); the tests do that.

Nothing here is ever called by ``chem``; a name is never part of a key.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .protocol import NAME_GENERATION_TIMEOUT_S

__all__ = ["LABEL", "PROG_IDS", "ChemDrawError", "ChemDrawAnswer", "find_prog_id", "available",
           "name_molfile", "parse_output", "has_control", "script_args", "DEFAULT_TIMEOUT_S"]

#: What the program is called in messages and in the panel.
LABEL = "ChemDraw"

#: Automation classes to look for, newest first.
PROG_IDS = ("ChemDraw_x64.Application", "ChemDraw.Application")

#: Time ChemDraw gets for one name. Stopping it after a time-out can take a
#: few seconds more.
DEFAULT_TIMEOUT_S = float(NAME_GENERATION_TIMEOUT_S)

_SCRIPT = Path(__file__).with_name("chemdraw_name.ps1")
_CLEANUP = Path(__file__).with_name("chemdraw_cleanup.ps1")
_DISABLE_ENV = "COMPOUND_REGISTRY_NO_CHEMDRAW"
#: Windows accepts 32767 characters for a whole command line.
_MAX_ENCODED = 30000
#: What ends a '...' string in Windows PowerShell: the apostrophe and the four
#: typographic single quotes. Written twice, each stands for itself.
_QUOTES = re.compile("(['\u2018\u2019\u201a\u201b])")


class ChemDrawError(Exception):
    """ChemDraw gave no answer. ``code`` is ``unavailable`` (not installed, or
    switched off), ``timeout`` or ``failed``; ``message`` is plain words."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ChemDrawAnswer:
    name: str    # as ChemDraw wrote it; may be empty when it cannot name the structure
    inchi: str   # standard InChI of the structure as ChemDraw read it
    atoms: int   # number of atoms ChemDraw read


#: Runs the script: ``(arguments, stdin text, time-out in seconds)`` ->
#: ``(stdout text, timed out)``. Replaced in tests.
Runner = Callable[[list[str], str, float], tuple[str, bool]]


def find_prog_id() -> str | None:
    """The automation class of the installed ChemDraw, or None. Reads the
    Windows registry only."""
    if sys.platform != "win32" or _switched_off():
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - not Windows
        return None
    for prog_id in PROG_IDS:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + "\\CLSID"):
                return prog_id
        except OSError:
            continue
    return None


def _switched_off() -> bool:
    return bool(os.environ.get(_DISABLE_ENV))


def has_control(text: str) -> bool:
    """True when ``text`` has a control character or a line/paragraph
    separator (what a stored name must not contain)."""
    return any(unicodedata.category(ch) == "Cc" or ch in "\u2028\u2029" for ch in text)


def available() -> bool:
    """True when ChemDraw's automation interface is installed on this PC (it
    may still refuse to work, for example without a licence)."""
    return find_prog_id() is not None and _powershell() is not None and _SCRIPT.is_file()


def _powershell() -> str | None:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    path = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(path) if path.is_file() else None


def script_args(shell: str, script: Path, **parameters: str) -> list[str]:
    """The command line that runs the PowerShell script ``script`` with
    ``parameters`` (``ProgId="..."`` becomes ``-ProgId '...'``). A value
    reaches the script exactly as given, whatever characters it has.

    The script's text travels as ``-EncodedCommand`` (base64 of UTF-16), so
    PowerShell's execution policy, which applies to script files only, cannot
    block it. Standard input stays free for the script. Raises ``OSError``
    when the script cannot be read or is too long for a command line."""
    text = script.read_text(encoding="utf-8-sig")
    command = "& {\n" + text + "\n}"
    for name, value in parameters.items():
        command += f" -{name} '" + _QUOTES.sub(r"\1\1", str(value)) + "'"
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    if len(encoded) > _MAX_ENCODED:
        raise OSError(f"{script.name} is too long to be run by Windows PowerShell")
    return [shell, "-NoProfile", "-NonInteractive", "-NoLogo", "-EncodedCommand", encoded]


def _stop_leftovers(before: list[int]) -> None:
    """Stop the hidden ChemDraw a stopped naming script left behind (see
    ``chemdraw_cleanup.ps1`` for exactly which processes qualify). ``before``
    are the ChemDraw processes that ran before the call. Never raises."""
    shell = _powershell()
    if shell is None or not _CLEANUP.is_file():
        return
    try:
        subprocess.run(
            script_args(shell, _CLEANUP, Before=",".join(str(pid) for pid in before) or "none"),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _run(args: list[str], stdin_text: str, timeout: float) -> tuple[str, bool]:
    """The real runner: no console window, nothing inherited (the native host's
    own stdout carries protocol frames and must stay untouched)."""
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out, _ = proc.communicate(stdin_text.encode("ascii"), timeout=timeout)
        return out.decode("ascii", "replace"), False
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = b""
        return out.decode("ascii", "replace"), True


def parse_output(text: str) -> tuple[list[int] | None, dict | None]:
    """``(ChemDraw processes that ran before the script started ChemDraw, the
    script's result object)``. The first is None when the script did not get
    as far as starting ChemDraw; the second is None without a result line."""
    before: list[int] | None = None
    result: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("before:"):
            before = [int(part) for part in line[7:].split(",") if part.strip().isdigit()]
        elif line.startswith("result:"):
            try:
                decoded = json.loads(base64.b64decode(line[7:], validate=True).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(decoded, dict):
                result = decoded
    return before, result


def name_molfile(molfile: str, *, timeout: float = DEFAULT_TIMEOUT_S,
                 runner: Runner | None = None, prog_id: str | None = None,
                 cleanup: Callable[[list[int]], None] | None = None) -> ChemDrawAnswer:
    """Ask the installed ChemDraw for the name of ``molfile`` (V2000 or V3000).

    Raises :class:`ChemDrawError`. An empty ``name`` in the answer means
    ChemDraw read the structure but has no name for it.
    """
    if not isinstance(molfile, str) or not molfile.strip():
        raise ChemDrawError("failed", "No structure was given.")
    if not _switched_off():
        prog_id = prog_id or find_prog_id()
    else:
        prog_id = None
    shell = _powershell()
    if prog_id is None or shell is None or not _SCRIPT.is_file():
        raise ChemDrawError("unavailable", f"{LABEL} was not found on this PC.")
    payload = base64.b64encode(molfile.encode("utf-8")).decode("ascii") + "\n"
    try:
        args = script_args(shell, _SCRIPT, ProgId=prog_id)
        output, timed_out = (runner or _run)(args, payload, timeout)
    except OSError as exc:
        raise ChemDrawError("failed", f"{LABEL} could not be started: {exc}") from exc
    before, result = parse_output(output)
    if timed_out:
        if before is not None:  # else the script never got as far as starting ChemDraw
            (cleanup or _stop_leftovers)(before)
        raise ChemDrawError("timeout", f"{LABEL} did not answer within {timeout:.0f} seconds.")
    if result is None:
        raise ChemDrawError("failed", f"{LABEL} gave no answer: Windows PowerShell, which "
                                      f"talks to {LABEL}, did not run (it may be blocked on "
                                      "this PC).")
    if result.get("ok") is not True:
        detail = str(result.get("message") or "no details").strip()
        if result.get("stage") == "start":
            raise ChemDrawError("failed", f"{LABEL} could not be started: {detail}")
        raise ChemDrawError("failed", f"{LABEL} could not read or name the structure: {detail}")
    name, inchi, atoms = result.get("name"), result.get("inchi"), result.get("atoms")
    return ChemDrawAnswer(
        name=name.strip() if isinstance(name, str) else "",
        inchi=inchi.strip() if isinstance(inchi, str) else "",
        atoms=atoms if isinstance(atoms, int) and not isinstance(atoms, bool) else 0,
    )
