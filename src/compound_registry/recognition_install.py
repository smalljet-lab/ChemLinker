"""``compound-registry install-recognition``: the optional recognition part.

"Capture reaction" reads a reaction scheme from a picture with RxnScribe,
MolScribe and EasyOCR. These need torch and about 1.6 GB of model files, far
too much for the registry's own environment, so they get a folder of their
own, the "recognition home" (see :mod:`compound_registry.recognition`)::

    <recognition home>/env/             a Python 3.12 environment made by uv (about 1.2 GB)
    <recognition home>/models/          the four model files (about 1.6 GB)
    <recognition home>/installed.json   written LAST; without it nothing counts as installed

Steps (:func:`install`), each one skipped when its work is already done, so
the command can be interrupted and run again:

1. create the environment (``uv venv --python 3.12``), or make it again when
   its Python no longer starts (uv builds it on a Python that lives in the
   ChemLinker folder; when that folder is moved, the environment is dead);
2. install the pinned packages of ``recognition_requirements.txt`` (shipped in
   this package) with ``uv pip install``: every version is fixed, torch is the
   CPU build, RxnScribe and MolScribe are source archives of one commit;
3. get the model files: downloaded with the standard library, or copied from
   ``--models-from DIR``; each is checked against its SHA-256 before it gets
   its final name (a partial download never looks like a model);
4. start the environment's Python once and import the libraries;
5. write ``installed.json``.

The internet is needed for this command only. Afterwards the recognition
never uses the network. Nothing here touches a registry, the browser
registration or the registry's own environment.

Running it again on a complete installation keeps ``installed.json`` in place
until a step really has to change something (:class:`_Record`), so a repair
that fails for an outside reason (no internet) does not switch a working
recognition off.

``env`` and ``models`` are common folder names and ``--home`` can name any
folder, so nothing is deleted on the strength of its name alone: ``env`` only
when it is a Python environment (``pyvenv.cfg``), ``models`` as a whole only
when this command created it (:data:`OWNER_TAG`), otherwise only the files
this command knows.

Everything that touches the machine goes through :class:`Tools`, which the
tests replace.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

from . import __version__, recognition
from .errors import RegistryError

__all__ = ["ModelFile", "MODEL_FILES", "PYTHON_VERSION", "PIP_OPTIONS", "OWNER_TAG", "Tools",
           "install", "uninstall", "requirements_path", "overrides_path", "find_uv",
           "requirements_fingerprint", "record_is_current"]

#: The Python of the recognition environment (the versions are pinned for it).
PYTHON_VERSION = "3.12"
#: What ``uv pip install`` needs besides the requirements: the index with the
#: CPU builds of torch, and permission to take each package from the index
#: that has the pinned version.
PIP_OPTIONS = ("--extra-index-url", "https://download.pytorch.org/whl/cpu",
               "--index-strategy", "unsafe-best-match")
#: Sent with the downloads: a generic name, nothing about the user or the PC.
USER_AGENT = "ChemLinker-installer"
_CHUNK = 1024 * 1024
_DOWNLOAD_TIMEOUT = 60
_CHECK_TIMEOUT = 900
_CHECK_PROGRAM = "import torch, molscribe, rxnscribe, easyocr; print(torch.__version__)"
_PROBE_TIMEOUT = 120
_TIMED_OUT = 124
#: Written into a ``models`` folder that :func:`install` CREATED: everything in
#: it is then ours, and :func:`uninstall` may remove the folder as a whole.
OWNER_TAG = ".chemlinker-recognition"
#: What the worker writes into ``models`` besides the model files (caches).
_WORKER_FOLDERS = ("mpl", "hf", "torch", "easyocr/user_network")


@dataclass(frozen=True)
class ModelFile:
    """One model file: where it goes inside ``models/``, where it comes from,
    and what it must be. ``member``: the download is a zip and this is the
    file inside it."""

    name: str
    url: str
    sha256: str
    size: int
    member: str | None = None


MODEL_FILES: tuple[ModelFile, ...] = (
    ModelFile(recognition.RXN_CHECKPOINT,
              "https://huggingface.co/yujieq/RxnScribe/resolve/main/pix2seq_reaction_full.ckpt",
              "b0020634f13fb3e1f588bddca97f68fd6483f0cdecd83e2ca31c2434ea4340fe", 432_324_497),
    ModelFile(recognition.MOL_CHECKPOINT,
              "https://huggingface.co/yujieq/MolScribe/resolve/main/swin_base_char_aux_1m680k.pth",
              "32821fa4ba8d17f9a92e374601438c5e757947dbf1141e9ad0ef51cccfa1c501", 1_134_173_494),
    ModelFile("easyocr/craft_mlt_25k.pth",
              "https://github.com/JaidedAI/EasyOCR/releases/download/pre-v1.1.6/craft_mlt_25k.zip",
              "4a5efbfb48b4081100544e75e1e2b57f8de3d84f213004b14b85fd4b3748db17", 83_152_330,
              member="craft_mlt_25k.pth"),
    ModelFile("easyocr/english_g2.pth",
              "https://github.com/JaidedAI/EasyOCR/releases/download/v1.3/english_g2.zip",
              "e2272681d9d67a04e2dff396b6e95077bc19001f8f6d3593c307b9852e1c29e8", 15_143_997,
              member="english_g2.pth"),
)


def requirements_path() -> Path:
    return Path(__file__).with_name("recognition_requirements.txt")


def overrides_path() -> Path:
    return Path(__file__).with_name("recognition_overrides.txt")


def requirements_fingerprint() -> str:
    """SHA-256 of the shipped requirements, line endings left out of it (a
    checkout may change them)."""
    data = requirements_path().read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def record_is_current(record: Any) -> bool:
    """True when ``record`` (the content of ``installed.json``) was written
    for the pinned programs and the model files of THIS version."""
    if not isinstance(record, dict):
        return False
    try:
        known = {requirements_fingerprint(), _sha256(requirements_path())}
    except OSError:
        return False
    return record.get("requirements_sha256") in known \
        and record.get("models") == {model.name: model.sha256 for model in MODEL_FILES}


def _fail(message: str) -> RegistryError:
    return RegistryError(message, code="recognition_install_failed")


def find_uv() -> str | None:
    """uv on the PATH, else where its official installer puts it."""
    found = shutil.which("uv")
    if found:
        return found
    name = "uv.exe" if os.name == "nt" else "uv"
    for folder in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        if (folder / name).is_file():
            return str(folder / name)
    return None


def _uv_environment() -> dict[str, str]:
    """The environment for uv: like the installer scripts, Python and the uv
    cache stay inside the project folder when this program runs from one."""
    env = dict(os.environ)
    root = Path(sys.prefix).parent
    for variable, folder in (("UV_PYTHON_INSTALL_DIR", ".python"), ("UV_CACHE_DIR", ".uv-cache")):
        if not env.get(variable, "").strip() and (root / folder).is_dir():
            env[variable] = str(root / folder)
    # uv must not take the registry's own environment for the one to change.
    env.pop("VIRTUAL_ENV", None)
    return env


def _run(command: list[str], *, timeout: float | None = None) -> int:
    """Run a program, its output going straight to the terminal."""
    try:
        return subprocess.run(command, env=_uv_environment(), stdin=subprocess.DEVNULL,
                              timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127


def _open_url(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT)  # noqa: S310 - fixed https URLs


@dataclass
class Tools:
    """Everything that touches the machine. Tests replace the fields."""

    #: ``() -> path of uv or None``
    find_uv: Callable[[], str | None] = find_uv
    #: ``(command, timeout=None) -> exit code``; output goes to the terminal.
    run: Callable[..., int] = _run
    #: ``(url) -> response`` with ``read(n)`` and ``close()``.
    open_url: Callable[[str], Any] = _open_url
    #: One line of progress for the user.
    say: Callable[[str], None] = print


# --------------------------------------------------------------------------- model files

def _sha256(path: Path, progress: Callable[[int], None] | None = None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                return digest.hexdigest()
            digest.update(block)
            if progress is not None:
                progress(len(block))


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.0f} MB"


class _Progress:
    """Says how far a file is, every 10 % at most."""

    def __init__(self, say: Callable[[str], None], total: int):
        self.say, self.total, self.done, self.shown = say, max(total, 1), 0, 0

    def __call__(self, count: int) -> None:
        self.done += count
        step = min(self.done * 10 // self.total, 10)
        if step > self.shown:
            self.shown = step
            self.say(f"      {_megabytes(self.done)} of {_megabytes(self.total)} ({step * 10} %)")

    def finish(self) -> None:
        """The stream ended: say so when the expected size was not reached
        (the size was only an estimate)."""
        if self.shown < 10:
            self.shown = 10
            self.say(f"      {_megabytes(self.done)} (100 %)")


def _download_size(response: Any) -> int | None:
    """The size the server announced (``Content-Length``), if any."""
    try:
        headers = getattr(response, "headers", None)
        value = headers.get("Content-Length") if headers is not None else None
        size = int(value) if value is not None else 0
    except (TypeError, ValueError, AttributeError):
        return None
    return size if size > 0 else None


def _copy_stream(source: Any, target: BinaryIO, progress: Callable[[int], None]) -> str:
    """Copy ``source`` to ``target``; returns the SHA-256 of what was copied."""
    digest = hashlib.sha256()
    while True:
        block = source.read(_CHUNK)
        if not block:
            return digest.hexdigest()
        target.write(block)
        digest.update(block)
        progress(len(block))


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _present(model: ModelFile, target: Path) -> bool:
    """True when ``target`` is exactly this model file."""
    try:
        return target.is_file() and target.stat().st_size == model.size \
            and _sha256(target) == model.sha256
    except OSError:
        return False


def _finish(model: ModelFile, part: Path, target: Path, digest: str, source: str) -> None:
    if digest != model.sha256:
        _remove(part)
        raise _fail(f"{model.name} from {source} is not the expected file (its SHA-256 checksum "
                    "differs). Nothing was kept. Run the command again; if it happens again the "
                    "file has changed at its source, and the recognition part cannot be "
                    "installed until ChemLinker is updated.")
    os.replace(part, target)


def _fetch_model(model: ModelFile, models: Path, models_from: Path | None, tools: Tools,
                 before_change: Callable[[], None] = lambda: None) -> str:
    """Put one model file in place; returns "present", "copied" or "downloaded".
    ``before_change`` is called before anything is written."""
    target = models / model.name
    if _present(model, target):
        tools.say(f"    {model.name}: already there")
        return "present"
    before_change()
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    _remove(part)
    if models_from is not None:
        source = models_from / model.name
        if not source.is_file():
            raise _fail(f"{source} does not exist. --models-from needs a folder that holds "
                        + ", ".join(m.name for m in MODEL_FILES) + ".")
        tools.say(f"    {model.name}: copying {_megabytes(model.size)} from {models_from}")
        try:
            with open(source, "rb") as reader, open(part, "wb") as writer:
                digest = _copy_stream(reader, writer, _Progress(tools.say, model.size))
        except OSError as exc:
            _remove(part)
            raise _fail(f"{model.name} could not be copied: {exc.strerror or exc}.") from None
        _finish(model, part, target, digest, str(source))
        return "copied"

    # ``model.size`` is the size of the model file. A zip download is smaller:
    # its progress is counted against what the server announces.
    size = "" if model.member is not None else f" {_megabytes(model.size)}"
    tools.say(f"    {model.name}: downloading{size} from {model.url}")
    download = part if model.member is None else target.with_name(target.name + ".zip.part")
    _remove(download)
    try:
        response = tools.open_url(model.url)
        try:
            progress = _Progress(tools.say, _download_size(response) or model.size)
            with open(download, "wb") as writer:
                digest = _copy_stream(response, writer, progress)
            progress.finish()
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
        if model.member is not None:
            try:
                with zipfile.ZipFile(download) as archive, archive.open(model.member) as reader, \
                        open(part, "wb") as writer:
                    digest = _copy_stream(reader, writer, lambda _count: None)
            except (zipfile.BadZipFile, KeyError):
                _remove(part)
                raise _fail(f"The download of {model.name} is not the expected zip file. Run "
                            "the command again.") from None
            finally:
                _remove(download)
    except RegistryError:
        raise
    except Exception as exc:  # noqa: BLE001 - urllib raises many kinds; all mean "no file"
        _remove(part)
        _remove(download)
        reason = getattr(exc, "reason", None) or getattr(exc, "strerror", None) or type(exc).__name__
        raise _fail(f"{model.name} could not be downloaded ({reason}). Check the internet "
                    "connection and run the command again: what is already there is kept.") \
            from None
    _finish(model, part, target, digest, model.url)
    return "downloaded"


# --------------------------------------------------------------------------- install

def _write_marker(home: Path) -> dict[str, Any]:
    marker = {
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "installed_by": __version__,
        "python": PYTHON_VERSION,
        "requirements_sha256": requirements_fingerprint(),
        "models": {model.name: model.sha256 for model in MODEL_FILES},
    }
    temporary = home / (recognition.MARKER_NAME + ".part")
    temporary.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, home / recognition.MARKER_NAME)
    return marker


class _Record:
    """``installed.json`` during :func:`install`.

    A record that does not fit this version is removed at once: the part
    counts as not installed until the last step. A record that fits stays
    until a step has to change something (:meth:`drop`), and is written anew
    at the end. So running the command on a complete installation never
    switches the recognition off, also when it fails half-way (no internet,
    interrupted)."""

    def __init__(self, home: Path):
        self.path = home / recognition.MARKER_NAME
        self.kept = record_is_current(recognition.read_record(home))
        if not self.kept:
            _remove(self.path)

    def drop(self) -> None:
        if self.kept:
            self.kept = False
            _remove(self.path)


def _removable_environment(env: Path) -> bool:
    """True when ``env`` may be deleted: a Python environment (it has a
    ``pyvenv.cfg``) or an empty folder. Anything else was not made here."""
    try:
        return (env / "pyvenv.cfg").is_file() or (env.is_dir() and not any(env.iterdir()))
    except OSError:
        return False


def install(home: Path | None = None, *, models_from: Path | None = None,
            tools: Tools | None = None) -> dict[str, Any]:
    """Install (or complete, or repair) the recognition part in ``home``
    (default: :func:`compound_registry.recognition.recognition_home`).
    Raises ``recognition_install_failed`` with what to do."""
    tools = tools if tools is not None else Tools()
    home = Path(home) if home is not None else recognition.recognition_home()
    say = tools.say
    if models_from is not None:
        models_from = Path(models_from)
        if not models_from.is_dir():
            raise _fail(f"--models-from: {models_from} is not a folder.")
    for needed in (requirements_path(), overrides_path(), recognition.worker_script()):
        if not needed.is_file():
            raise _fail(f"{needed} is missing from this installation of ChemLinker. Run the "
                        "ChemLinker installer again.")
    uv = tools.find_uv()
    if uv is None:
        raise _fail("uv, the Python package manager ChemLinker is installed with, was not "
                    "found. Run the ChemLinker installer again (it offers to install uv), "
                    "open a new terminal and repeat this command.")

    say(f"Installing the recognition part into {home}")
    say("(about 2.8 GB; the internet is needed now, never afterwards)")
    try:
        home.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(home).free
    except OSError as exc:
        raise _fail(f"{home} cannot be used: {exc.strerror or exc}.") from None
    if free < 4_000_000_000 and not recognition.env_python(home).is_file():
        say(f"warning: only {free / 1e9:.1f} GB are free on that drive; about 3 GB are needed.")
    record = _Record(home)

    python = recognition.env_python(home)
    env = home / "env"
    say("")
    say(f"[1/4] The Python environment ({env})")
    if python.is_file() and tools.run([str(python), "-I", "-c", "pass"],
                                      timeout=_PROBE_TIMEOUT) == 0:
        say("    already there")
    else:
        if python.is_file():
            say("    it is there but its Python does not start any more (was the ChemLinker "
                "folder moved?): it is made again")
        if env.exists() and not _removable_environment(env):
            raise _fail(f"{env} exists but is not a Python environment made by this command, "
                        "so it is not touched. Choose another folder with --home, or move "
                        "that folder away yourself.")
        record.drop()
        if env.exists():
            # Left over from an interrupted run, or dead: made again.
            shutil.rmtree(env, ignore_errors=True)
        code = tools.run([uv, "venv", str(env), "--python", PYTHON_VERSION])
        if code != 0 or not python.is_file():
            raise _fail(f"The Python environment could not be created (uv venv ended with code "
                        f"{code}); see its message above.")

    say("")
    say("[2/4] The recognition programs (torch, RxnScribe, MolScribe, EasyOCR: about 1.2 GB)")
    code = tools.run([uv, "pip", "install", "--python", str(python),
                      "-r", str(requirements_path()), "--overrides", str(overrides_path()),
                      *PIP_OPTIONS])
    if code != 0:
        raise _fail(f"The recognition programs could not be installed (uv pip install ended "
                    f"with code {code}); see its message above. Run the command again: what "
                    "was downloaded is kept.")

    say("")
    say("[3/4] The model files (about 1.6 GB)")
    models = recognition.models_dir(home)
    if not models.exists():
        models.mkdir(parents=True)
        (models / OWNER_TAG).write_text(
            "This folder was created by 'compound-registry install-recognition'.\n",
            encoding="utf-8")
    (models / "easyocr" / "user_network").mkdir(parents=True, exist_ok=True)
    fetched = {model.name: _fetch_model(model, models, models_from, tools, record.drop)
               for model in MODEL_FILES}

    say("")
    say("[4/4] Checking that the programs start (this can take a few minutes the first time)")
    code = tools.run([str(python), "-I", "-c", _CHECK_PROGRAM], timeout=_CHECK_TIMEOUT)
    if code == _TIMED_OUT:
        # Too slow says nothing about the installation: a record stays.
        raise _fail("Starting the recognition programs took longer than "
                    f"{_CHECK_TIMEOUT // 60} minutes and was stopped. Run the command again "
                    "(a virus scanner can make the very first start that slow).")
    if code != 0:
        record.drop()
        raise _fail(f"The recognition programs were installed but do not start (code {code}); "
                    "see the message above. Run the command again; if it still fails, run it "
                    "with --uninstall first.")

    marker = _write_marker(home)
    say("")
    say("The recognition part is installed. Restart the browser (or reload the extension), "
        "then use 'Capture reaction' from the ChemLinker button.")
    return {"installed": True, "home": str(home), "models": fetched, "marker": marker}


def _remove_tree(folder: Path) -> None:
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        raise _fail(f"{folder} could not be removed completely ({exc.strerror or exc}). "
                    "Close the browser (the recognition may still be running) and run "
                    "the command again.") from None


def _remove_known_models(models: Path) -> bool:
    """Remove what this command and the worker put into a ``models`` folder
    that was there before: the model files by name, partial downloads, the
    worker's cache folders. True when the folder is gone afterwards."""
    for model in MODEL_FILES:
        target = models / model.name
        for path in (target, target.with_name(target.name + ".part"),
                     target.with_name(target.name + ".zip.part")):
            _remove(path)
    for name in _WORKER_FOLDERS:
        if (models / name).is_dir():
            _remove_tree(models / name)
    for folder in (models / "easyocr", models):
        try:
            folder.rmdir()                         # only when empty
        except OSError:
            pass
    return not models.exists()


def uninstall(home: Path | None = None, *, tools: Tools | None = None) -> dict[str, Any]:
    """Remove what :func:`install` put into ``home``, then the folder itself
    if nothing else is in it.

    Only what is known to come from this command is deleted, because ``env``
    and ``models`` are common names and ``home`` can be any folder:

    * ``env`` when it is a Python environment (it holds ``pyvenv.cfg``);
    * ``models`` as a whole when :func:`install` created it (:data:`OWNER_TAG`);
      in a folder that was there before, only the model files and the worker's
      caches, and only when ``installed.json`` shows that an installation was
      completed here;
    * ``installed.json``.

    Everything else is left alone and named in ``"kept"``."""
    tools = tools if tools is not None else Tools()
    home = Path(home) if home is not None else recognition.recognition_home()
    removed: list[str] = []
    kept: list[str] = []
    if not home.is_dir():
        tools.say(f"Nothing to remove: {home} does not exist.")
        return {"installed": False, "home": str(home), "removed": removed, "kept": kept}
    record = recognition.read_record(home)
    installed_here = isinstance(record, dict) and isinstance(record.get("models"), dict)
    _remove(home / recognition.MARKER_NAME)        # first: from now on "not installed"
    _remove(home / (recognition.MARKER_NAME + ".part"))

    env = home / "env"
    if env.exists():
        if _removable_environment(env):
            tools.say(f"Removing {env}")
            _remove_tree(env)
            removed.append(str(env))
        else:
            kept.append(str(env))
            tools.say(f"Left alone: {env} is not a Python environment made by this command.")

    models = recognition.models_dir(home)
    if models.exists():
        if (models / OWNER_TAG).is_file():
            tools.say(f"Removing {models}")
            _remove_tree(models)
            removed.append(str(models))
        elif installed_here:
            tools.say(f"Removing the model files in {models}")
            if _remove_known_models(models):
                removed.append(str(models))
            else:
                kept.append(str(models))
                tools.say(f"Left alone: the other files in {models} (not from this command).")
        else:
            kept.append(str(models))
            tools.say(f"Left alone: {models} was not created by this command, and nothing "
                      "shows that an installation was completed here. If it is the rest of "
                      "an interrupted installation, delete it yourself.")
    try:
        home.rmdir()
        removed.append(str(home))
    except OSError:
        pass                                       # something else is in it; it stays
    tools.say("The recognition part was removed." if removed
              else f"Nothing to remove in {home}.")
    return {"installed": False, "home": str(home), "removed": removed, "kept": kept}
