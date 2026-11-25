"""``compound-registry setup``: the installation, as one guided run.

``install.ps1`` / ``install.sh`` create the Python environment and then start
this; ``update.ps1`` / ``update.sh`` start it with ``--upgrade``. It asks, in
this order and each with a default that Enter accepts:

1. the data folder - where the registry database, its backups and the
   compound files are stored. An existing registry there is used as it is; a
   new, empty one is created only after a confirmation (and then its ID
   format is chosen, once and for all). The browsers are never moved to
   another registry without a confirmation;
2. the browser(s): Firefox, Chrome or both;
3. the address of the ELN the extension is built for;
4. the MestReNova add-on (only where MestReNova was found).

Then it acts: creates the registry when that was confirmed, registers the
host (:func:`compound_registry.install_host.install`), saves the ELN address
and builds the extension packages with npm, installs or refreshes the
MestReNova add-on, checks the stored structures when the chemistry version
changed (:func:`compound_registry.service.upgrade_registry`) and ends with
one list of what is left to do by hand.

Every option answers its question in advance. ``--yes`` asks nothing and
takes every default (it never creates a registry and never switches to
another one unless the options say so); ``--upgrade`` asks nothing and keeps
everything as it was installed; ``--dry-run`` only prints what would be done.

Everything that touches the machine is in :class:`Environment`, so tests
replace the questions, the output, the browser registration target, npm and
MestReNova.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from . import __version__, install_host
from .errors import RegistryError
from .idformat import IdFormat

__all__ = [
    "DEFAULT_ELN_ORIGIN", "LOCAL_CONFIG_FILE", "BROWSER_CHOICES", "Options", "Environment",
    "parse_eln_origin", "read_saved_eln_origin", "normalise_folder", "id_format_for_prefix",
    "find_project_root", "real_environment", "registry_summary", "node_version_ok",
    "extension_paths", "next_steps", "run_setup",
]

#: The ELN address the extension is built for unless another one is chosen.
#: The same value as DEFAULT_ELN_ORIGIN in extension/scripts/eln-origin.mjs.
DEFAULT_ELN_ORIGIN = "https://sciformation.chemie.unibas.ch"
#: The file in ``extension/`` the chosen ELN address is saved in (the build reads it).
LOCAL_CONFIG_FILE = "local.config.json"
BROWSER_CHOICES = {"firefox": ("firefox",), "chrome": ("chrome",), "both": ("firefox", "chrome")}
_LABELS = {"firefox": "Firefox", "chrome": "Chrome"}
#: What ``engines.node`` of extension/package.json asks for: ^20.19.0 || >=22.12.0.
_NODE_NEEDS = "20.19 or newer (of the 20 series), or 22.12 or newer"
_UPDATE_HINT = ("To update later: get the new version of the project folder, then run "
                "update.ps1 (Windows: double-click update.cmd) or 'sh update.sh' "
                "(macOS/Linux).")
#: The name and size of the optional recognition part. The installer asks
#: before it downloads it, and never downloads it in a run that asks nothing
#: (unless ``--recognition yes`` says so).
RECOGNISER_NAME = "ChemRecogniser"
RECOGNISER_SIZE = "about 2.8 GB"
_RECOGNITION_HINT = (f"Optional: {RECOGNISER_NAME} ('Capture reaction': reading a drawn reaction "
                     f"scheme from the screen) is not installed. It needs {RECOGNISER_SIZE} "
                     "on disk. To install it later, run 'compound-registry "
                     "install-recognition'.")
_RELEASES = "https://github.com/smalljet-lab/ChemLinker/releases"

Run = Callable[[Sequence[str], Path], "tuple[int, str]"]


# --------------------------------------------------------------------------- options, environment

@dataclass
class Options:
    """The command-line options of ``compound-registry setup``."""

    home: str | None = None
    browser: str | None = None          # firefox | chrome | both
    eln_origin: str | None = None
    id_prefix: str | None = None
    id_format: str | None = None
    create_registry: bool = False
    mnova: str | None = None            # yes | no
    recognition: str | None = None      # yes | no
    skip_extension: bool = False
    force: bool = False
    yes: bool = False
    upgrade: bool = False
    dry_run: bool = False

    @property
    def asks(self) -> bool:
        return not (self.yes or self.upgrade)


def _run_command(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    """Run ``argv`` in ``cwd``; (exit code, everything it printed)."""
    try:
        done = subprocess.run(list(argv), cwd=str(cwd), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    except OSError as exc:
        return 1, f"{argv[0]} could not be started: {exc.strerror or exc}"
    return done.returncode, done.stdout.decode("utf-8", "replace")


def _mnova_status() -> dict[str, Any]:
    from . import mnova

    return mnova.status()


def _mnova_install() -> dict[str, Any]:
    from . import mnova

    return mnova.install()


def _recognition_status() -> dict[str, Any]:
    from . import recognition

    return recognition.install_state()


def _recognition_install(say: Callable[[str], None]) -> dict[str, Any]:
    from . import recognition_install

    return recognition_install.install(tools=recognition_install.Tools(say=say))


def _upgrade_registry(home: str) -> dict[str, Any]:
    from .service import upgrade_registry

    return upgrade_registry(home)


def _create_registry(home: str, id_format: str | None) -> list[str]:
    from .service import create_registry

    _config, warnings = create_registry(home, id_format)
    return warnings


def find_project_root() -> Path | None:
    """The project folder this program runs from (it holds ``extension/``),
    or None when the package is installed without it."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "extension" / "package.json").is_file() else None


@dataclass
class Environment:
    """Everything the wizard asks of the machine. ``ask`` shows a prompt and
    returns the answer (``EOFError`` when the input ended); ``say`` prints
    one line; ``can_ask`` is false when nobody is there to answer; ``target``
    is where the browser registration goes (None: this machine);
    ``project_root`` is the project folder (None: not known, nothing is
    built); ``which`` finds a program, ``run`` runs one. Nothing that reaches
    outside the registry folder and ``target`` has a default:
    :func:`real_environment` gives the real ones."""

    ask: Callable[[str], str]
    say: Callable[[str], None]
    can_ask: bool
    target: install_host.HostTarget | None
    project_root: Path | None
    which: Callable[[str], str | None]
    run: Run
    mnova_status: Callable[[], dict[str, Any]]
    mnova_install: Callable[[], dict[str, Any]]
    upgrade_registry: Callable[[str], dict[str, Any]] = _upgrade_registry
    create_registry: Callable[[str, "str | None"], list[str]] = _create_registry
    install: Callable[..., dict[str, Any]] = install_host.install
    #: The optional ChemRecogniser: ``() -> {"installed", "home", "problem"}``
    #: and ``(say) -> result`` (downloads about 2.8 GB when it is not there).
    recognition_status: Callable[[], dict[str, Any]] = _recognition_status
    recognition_install: Callable[[Callable[[str], None]], dict[str, Any]] = _recognition_install


def real_environment(ask: Callable[[str], str], say: Callable[[str], None],
                     can_ask: bool) -> Environment:
    """The environment of this machine: the real browser registration, npm,
    MestReNova and project folder."""
    return Environment(
        ask=ask, say=say, can_ask=can_ask, target=None, project_root=find_project_root(),
        which=shutil.which, run=_run_command, mnova_status=_mnova_status,
        mnova_install=_mnova_install, upgrade_registry=_upgrade_registry,
        create_registry=_create_registry, install=install_host.install)


# --------------------------------------------------------------------------- small pure parts

_HOST_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_IPV6_HOST = re.compile(r"^[0-9a-f:.]+$")


def parse_eln_origin(value: str) -> str:
    """``value`` as the origin the extension build accepts: ``https://host``
    or ``https://host:port`` (lower case, no default port). Raises
    ``RegistryError`` (``invalid_argument``) with the reason otherwise. The
    rule of ``parseElnOrigin`` in extension/scripts/eln-origin.mjs."""
    def fail(why: str) -> RegistryError:
        return RegistryError(
            f"{value!r} is not usable as the ELN address ({why}). Give the origin only, for "
            "example https://eln.example.org or https://eln.example.org:8443",
            code="invalid_argument")

    text = (value or "").strip()
    if not text:
        raise fail("it is empty")
    if re.search(r"\s", text):
        raise fail("it contains white space")
    if not text.isascii():
        raise fail("write the host name with ASCII characters only")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise fail("it is not a URL") from None
    if parts.scheme.lower() != "https":
        raise fail("it must start with https://")
    if "@" in text:
        raise fail("it must not contain a user name or password")
    if "?" in text or "#" in text:
        raise fail("it must not contain a query or a fragment")
    if parts.path not in ("", "/"):
        raise fail("it must not contain a path: Sciformation has to be served at the root of "
                   "the host")
    host = (parts.hostname or "").lower()
    if ":" in host:
        if not _IPV6_HOST.match(host):
            raise fail("the host name is not valid")
        host = f"[{host}]"
    elif not _HOST_NAME.match(host):
        raise fail("the host name is not valid")
    if port == 0:
        raise fail("the port is not valid")
    return f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"


def read_saved_eln_origin(root: Path | None) -> tuple[str | None, str | None]:
    """The address saved in ``<root>/extension/local.config.json``:
    ``(origin, None)``, ``(None, None)`` when nothing is saved, or
    ``(None, problem)`` when the file is unusable. Never raises."""
    if root is None:
        return None, None
    path = root / "extension" / LOCAL_CONFIG_FILE
    try:
        document = json.loads(path.read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, RecursionError):
        return None, f"{path} cannot be read as JSON"
    value = document.get("elnOrigin") if isinstance(document, dict) else None
    if not isinstance(value, str):
        return None, f'{path} does not hold {{"elnOrigin": "https://..."}}'
    try:
        return parse_eln_origin(value), None
    except RegistryError as exc:
        return None, f"{path}: {exc.message}"


def normalise_folder(text: str) -> str:
    """A folder as typed or pasted, ready to use: surrounding blanks and
    quotes removed, ``~`` and environment variables expanded, no trailing
    separator. Raises ``RegistryError`` (``invalid_argument``) unless the
    result is an absolute path."""
    value = (text or "").strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if not value:
        raise RegistryError("No folder was given.", code="invalid_argument")
    expanded = Path(os.path.expandvars(value)).expanduser()
    if not expanded.is_absolute():
        raise RegistryError(
            f"The data folder must be an absolute path (such as "
            f"{Path.home() / 'CompoundRegistry'}), got {value!r}.", code="invalid_argument")
    return os.path.normpath(str(expanded))


def id_format_for_prefix(prefix: str) -> str:
    """The ID template for IDs that start with ``prefix``: exactly three letters A-Z.
    Small letters are turned into capitals."""
    text = (prefix or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", text):
        raise RegistryError("The first three letters of the IDs must be exactly three capital "
                            f"letters from A to Z (for example PET or LEM), got {prefix!r}.",
                            code="invalid_argument")
    return f"{text}{{core:06d}}_{{stereo:02d}}_{{batch:03d}}"


def _checked_format(template: str) -> IdFormat:
    fmt = IdFormat(template)
    fmt.require_file_safe()
    return fmt


def registry_summary(config: Any) -> dict[str, Any]:
    """What the registry of ``config`` holds, read without changing the file:
    ``{"id_format", "example", "batches", "retired", "cores", "problem"}``
    (the counts are None, and ``problem`` says why, when the database cannot
    be read)."""
    template = str(config.id_format)
    result: dict[str, Any] = {"id_format": template, "example": None, "batches": None,
                              "retired": None, "cores": None, "problem": None}
    try:
        conn = sqlite3.connect(Path(config.db_path).as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'id_format'").fetchone()
            if row and row[0]:
                template = result["id_format"] = str(row[0])
            result["cores"] = conn.execute("SELECT count(*) FROM cores").fetchone()[0]
            result["batches"] = conn.execute("SELECT count(*) FROM batches").fetchone()[0]
            result["retired"] = conn.execute(
                "SELECT count(*) FROM batches WHERE retired_at IS NOT NULL").fetchone()[0]
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        result["problem"] = f"the database could not be read ({exc})"
    try:
        result["example"] = IdFormat(template).format(1, 1, 1)
    except RegistryError:
        pass
    return result


def node_version_ok(text: str) -> bool:
    """Whether ``node --version`` output satisfies the extension's build
    (``^20.19.0 || >=22.12.0``)."""
    match = re.match(r"\s*v?(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    return (major == 20 and minor >= 19) or (major == 22 and minor >= 12) or major > 22


def extension_paths(root: Path) -> dict[str, Path]:
    """Where the build puts the extension: ``firefox_package`` (the .xpi),
    ``firefox_folder`` (unpacked, for a temporary add-on), ``chrome_folder``
    (for 'Load unpacked') and ``chrome_package`` (the .zip)."""
    ext = root / "extension"
    version = __version__
    try:
        version = str(json.loads((ext / "package.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {
        "firefox_package": ext / "artifacts" / f"chemlinker-{version}-firefox.xpi",
        "firefox_folder": ext / "dist",
        "chrome_folder": root / "chrome-extension",
        "chrome_package": ext / "artifacts" / f"chemlinker-{version}-chrome.zip",
    }


def next_steps(browsers: Sequence[str], *, root: Path | None, build_note: str | None = None,
               build_commands: bool = False, mnova: bool = False,
               upgrade: bool = False, download: bool = False) -> list[str]:
    """The one list of what is left to do by hand after the host is
    registered, for ``browsers``. ``build_note`` says why the extension
    packages were not built; ``build_commands`` adds the commands that build
    them (for ``install-host``, which does not build); with neither, they are
    there. ``download`` (with ``build_note``): the ready-made packages of the
    Releases page fit this installation, so the browser steps say how to use
    them. ``mnova``: the add-on was installed or refreshed; ``upgrade``: an
    installation was brought up to date."""
    paths = extension_paths(root) if root is not None else None
    missing = bool(build_note)
    after = " (after step 1)" if missing else ""

    def place(key: str, fallback: str) -> str:
        return str(paths[key]) if paths is not None else fallback

    def package_name(key: str, suffix: str) -> str:
        return (paths[key].name if paths is not None
                else f"chemlinker-{__version__}-{suffix}")

    lines: list[str] = []
    number = 0

    def step(text: str) -> None:
        nonlocal number
        number += 1
        lines.append(f"  {number}. {text}")

    def more(text: str) -> None:
        lines.append(f"     {text}")

    if build_note:
        step("Build the extension - it was NOT built:")
        for line in build_note.splitlines():
            more(line)
    elif build_commands:
        step("Build the extension (needs Node.js). The installer (install.ps1 / install.sh) "
             "does it;")
        more(f"by hand, in {root / 'extension' if root is not None else 'the extension folder'}:")
        more("  npm ci")
        if "firefox" in browsers:
            more("  npm run package")
        if "chrome" in browsers:
            more("  npm run package:chrome")
    if "firefox" in browsers:
        package = place("firefox_package",
                        "extension/artifacts/chemlinker-<version>-firefox.xpi")
        manifest = (str(paths["firefox_folder"] / "manifest.json") if paths is not None
                    else "extension/dist/manifest.json")
        step(f"Firefox{after}: {'install the new' if upgrade else 'install the'} extension file")
        if missing and download:
            more(f"{package_name('firefox_package', 'firefox.xpi')} that you downloaded"
                 + (", or, after a build," if paths is not None else ""))
        if paths is not None or not (missing and download):
            more(package)
        more("- Firefox ESR, Developer Edition and Nightly install it for good: set")
        more("  xpinstall.signatures.required to false in about:config (once), then open")
        more("  about:addons, gear menu, 'Install Add-on From File...' and choose the file.")
        more("- Ordinary Firefox (release, beta) refuses a file that Mozilla has not signed.")
        more("  There it can only be loaded until Firefox is closed: open")
        more("  about:debugging#/runtime/this-firefox, 'Load Temporary Add-on...' and choose")
        more("  the same file" if missing else f"  {manifest}")
        more("  (again after every start of Firefox) - unless your group has the file signed.")
    if "chrome" in browsers:
        folder = place("chrome_folder", "chrome-extension")
        if upgrade:
            step(f"Chrome{after}: open chrome://extensions and click the reload arrow of "
                 "'ChemLinker'.")
            more("If it is not loaded yet: switch on 'Developer mode', click 'Load unpacked' and")
            more(f"choose the folder {folder}")
        else:
            step(f"Chrome{after}: open chrome://extensions, switch on 'Developer mode', click "
                 "'Load unpacked'")
            more(f"and choose the folder {folder}")
        if missing and download:
            more(f"With the downloaded {package_name('chrome_package', 'chrome.zip')}: unpack it "
                 "first, into a folder that")
            more("stays" + (" (the folder Chrome has loaded the extension from, if it is loaded)"
                            if upgrade else "") + ", and choose that folder.")
    step("Reload the ELN tab(s) that are open. The browser finds the registry program at the")
    more("extension's next request.")
    if mnova:
        step("Restart MestReNova, so that it loads the add-on.")
    return lines


# --------------------------------------------------------------------------- questions

class _Cancelled(RegistryError):
    code = "cancelled"


def _ask(env: Environment, prompt: str) -> str:
    try:
        return env.ask(prompt).strip()
    except EOFError:
        raise _Cancelled("The input ended before every question was answered. Nothing more was "
                         "done.") from None


def _ask_text(env: Environment, question: str, default: str) -> str:
    return _ask(env, f"{question} [{default}]: ") or default


def _ask_yes_no(env: Environment, question: str, default: bool) -> bool:
    while True:
        answer = _ask(env, f"{question} [{'Y/n' if default else 'y/N'}]: ").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        env.say("Please answer y or n.")


def _needs_answer(text: str) -> RegistryError:
    return RegistryError(text + " Nothing was changed.", code="setup_needs_answer")


# --------------------------------------------------------------------------- the decisions

@dataclass
class _Plan:
    home: str
    location: install_host.RegistryLocation
    create: bool = False
    id_format: str | None = None
    switch_from: str | None = None
    browsers: tuple[str, ...] = ()
    force: bool = False
    eln_origin: str | None = None       # None: no address was chosen in this run
    save_eln: bool = False
    build: bool = False
    mnova: bool = False
    recognition: bool = False
    notes: list[str] = field(default_factory=list)


def _wanted_format(options: Options) -> str | None:
    if options.id_format is not None and options.id_prefix is not None:
        raise RegistryError("Give either --id-prefix or --id-format, not both.",
                            code="invalid_argument")
    if options.id_format is not None:
        return _checked_format(options.id_format).template
    if options.id_prefix is not None:
        return _checked_format(id_format_for_prefix(options.id_prefix)).template
    return None


def _default_folder(now: dict[str, Any]) -> str:
    if now["home"]:
        return str(now["home"])
    from .config import default_home

    try:
        return str(default_home())
    except RegistryError:
        return str(Path.home() / "CompoundRegistry")


def _sync_warning(location: install_host.RegistryLocation) -> str | None:
    from .config import sync_folder_warning

    return sync_folder_warning(Path(location.config.db_path))


def _require_folder_path(home: str) -> None:
    """Refuse a data folder that is a file, or lies below one."""
    for path in (Path(home), *Path(home).parents):
        try:
            if path.is_dir():
                return
            in_the_way = path.exists()
        except OSError:
            return
        if in_the_way:
            raise RegistryError(
                f"{home} cannot be the data folder: {path} is a file, not a folder.",
                code="invalid_argument")


def _creates_database(location: install_host.RegistryLocation) -> bool:
    return str(location.config.db_path) in location.missing


def _choose_folder(options: Options, env: Environment, now: dict[str, Any],
                   wanted_format: str | None) -> _Plan:
    """Question 1: the data folder, the registry in it, a switch of registry."""
    say = env.say
    current = now["home"]
    default = _default_folder(now)
    given = options.home
    if options.upgrade and given is None and current is None:
        raise _needs_answer(
            "Nothing is installed for this user yet (the registry program is not registered "
            "with Firefox or Chrome), so there is nothing to bring up to date. Run the "
            "installer (install.ps1 / install.sh) instead.")
    while True:
        if given is not None:
            text, given_now = given, True
        elif options.asks:
            say("")
            say("Folder in which the registry database, its backups and the compound files are")
            say("stored. Use a folder on a local disk.")
            text, given_now = _ask_text(env, "Data folder", default), False
        else:
            text, given_now = default, False
        try:
            home = normalise_folder(text)
            _require_folder_path(home)
            location = install_host.locate_registry(home)
        except RegistryError as exc:
            if given_now or not options.asks:
                raise
            say(f"  {exc.message}")
            continue
        plan = _Plan(home=location.home, location=location)
        known = current is not None and install_host._same_path(current, location.home)
        warning = _sync_warning(location)
        if warning:
            say(f"  warning: {warning}")
            if options.asks and not known and not _ask_yes_no(
                    env, "Use this folder anyway?", False):
                given = None
                continue
        if location.exists:
            if wanted_format is not None:
                raise RegistryError(
                    f"An ID format can only be chosen for a new registry, but {location.home} "
                    "already has one. The ID format of an existing registry cannot be changed.",
                    code="invalid_argument")
            summary = registry_summary(location.config)
            say(f"A registry exists in {location.home} and will be used:")
            say(f"  ID format  {summary['id_format']}"
                + (f"  (IDs look like {summary['example']})" if summary["example"] else ""))
            if summary["problem"]:
                say(f"  Contents   unknown: {summary['problem']}")
            else:
                say(f"  Contents   {summary['batches']} ID(s) of {summary['cores']} compound(s), "
                    f"{summary['retired']} retired")
        else:
            decided = _decide_creation(options, env, plan, wanted_format)
            if not decided:
                given = None
                continue
        if current is not None and not known:
            say("")
            say("The browsers are set up for ANOTHER registry now:")
            say(f"  now   {current}")
            say(f"  new   {location.home}")
            say("IDs issued from now on would come from the new one. The other registry is not")
            say("touched.")
            if options.asks:
                if not _ask_yes_no(env, "Switch the browsers to the new registry?", False):
                    given = None
                    continue
            elif options.home is None:
                raise _needs_answer(
                    f"The browsers use the registry {current}, and this run would switch them "
                    f"to {location.home}. Pass --home with the folder that is meant.")
            plan.switch_from = str(current)
        return plan


def _decide_creation(options: Options, env: Environment, plan: _Plan,
                     wanted_format: str | None) -> bool:
    """No (complete) registry in the folder: confirm creating one and choose
    its ID format. False: the user wants another folder."""
    say = env.say
    location = plan.location
    partial = len(location.missing) == 1
    new_database = _creates_database(location)
    if partial and wanted_format is not None:
        raise RegistryError(
            f"An ID format can only be chosen for a new registry, but {location.home} "
            "already holds a part of one.", code="invalid_argument")
    if not options.create_registry and not options.asks:
        raise _needs_answer(
            f"No registry exists in {location.home} (missing: "
            f"{' and '.join(location.missing)}). A new, empty registry is only created when "
            "--create-registry is given (first installation); if the registry is elsewhere, "
            "pass --home with its folder.")
    say("")
    if not partial:
        say(f"No registry exists in {location.home}.")
        say("A NEW, EMPTY registry will be created there: its numbering starts at 1.")
        say("Do this only for a FIRST installation. If your registry was moved, or is on a drive")
        say("that is not connected, do not create one: choose the folder it is in.")
        question = "Create a new, empty registry?"
    elif new_database:
        say(f"The DATABASE of the registry in {location.home} is missing")
        say(f"({location.missing[0]}).")
        say("If it was moved, or is on a drive that is not connected, or a sync has not finished,")
        say("do not create one: choose the folder it is in, or restore it from a backup.")
        say("Creating it gives a NEW, EMPTY database: the IDs issued so far are not in it.")
        question = "Create a new, empty database?"
    else:
        say(f"The folder {location.home} holds a registry database without its settings file")
        say(f"(missing: {location.missing[0]}).")
        question = "Write a settings file with the defaults and use this database?"
    if not options.create_registry and not _ask_yes_no(env, question, False):
        return False
    if partial and new_database:
        say("A new, empty database will be created; config.toml is kept.")
    elif partial:
        say("The settings file will be created; the database is kept.")
    plan.create = True
    if partial:
        return True
    if wanted_format is None and options.asks:
        say("")
        say("ID format. Every ID starts with three capital letters of your choice, for example")
        say("your initials: PET gives PET000001_01_001 (compound 1, stereo variant 1, batch 1).")
        say("The letters can NEVER be changed once the registry exists.")
        while True:
            prefix = _ask(env, "First three letters of the compound IDs (A-Z, e.g. PET or LEM): ")
            try:
                fmt = _checked_format(id_format_for_prefix(prefix))
            except RegistryError as exc:
                say(f"  {exc.message}")
                continue
            if _ask_yes_no(env, f"IDs will look like {fmt.format(1, 1, 1)}. Use this format?",
                           True):
                wanted_format = fmt.template
                break
    plan.id_format = wanted_format
    example = IdFormat(wanted_format or IdFormat.DEFAULT).format(1, 1, 1)
    say(f"ID format of the new registry: {wanted_format or IdFormat.DEFAULT} (first ID "
        f"{example}). It cannot be changed afterwards.")
    return True


def _choose_browsers(options: Options, env: Environment, now: dict[str, Any],
                     target: install_host.HostTarget, plan: _Plan) -> None:
    """Question 2, and whether another installation's registration is replaced."""
    say = env.say
    chrome_possible = target.chrome_manifest_path is not None
    registered = tuple(now["browsers"])
    if registered:
        default = "both" if len(registered) > 1 else registered[0]
    else:
        default = "both" if chrome_possible else "firefox"
    if options.browser is not None:
        choice = options.browser
    elif options.asks and chrome_possible:
        say("")
        if target.platform != "windows":
            say("(On macOS and Linux, Chrome means Google Chrome; Chromium, Edge and Brave are "
                "not set up.)")
        while True:
            answer = _ask_text(env, "Browser - firefox, chrome or both", default).lower()
            matches = [name for name in BROWSER_CHOICES if name.startswith(answer)]
            if len(matches) == 1:
                choice = matches[0]
                break
            say("Please answer firefox, chrome or both.")
    else:
        choice = default
    if choice not in BROWSER_CHOICES:
        raise RegistryError(f"Unknown browser {choice!r}: use firefox, chrome or both.",
                            code="invalid_argument")
    browsers = BROWSER_CHOICES[choice]
    if "chrome" in browsers and not chrome_possible:
        raise RegistryError("Chrome cannot be set up on this system by the installer.",
                            code="invalid_argument")
    if browsers == ("chrome",) and plan.switch_from is not None:
        browsers = BROWSER_CHOICES["both"]
        plan.notes.append("Firefox is registered as well: both browsers share one registry "
                          "setting, and it can only be switched for both.")
    plan.browsers = browsers
    plan.force = options.force
    for browser in browsers:
        status = now["status"].get(browser) or {}
        launcher = status.get("launcher")
        other = (status.get("registered") and launcher and status.get("launcher_exists")
                 and not install_host._same_path(launcher, status.get("expected_launcher") or ""))
        if not other or plan.force:
            continue
        say("")
        say(f"{_LABELS[browser]} is registered with ANOTHER installation of this program:")
        say(f"  {launcher}")
        if options.asks and _ask_yes_no(env, "Let this installation take over?", False):
            plan.force = True
            continue
        raise RegistryError(
            f"{_LABELS[browser]}'s registration of the registry program belongs to another "
            f"installation ({launcher}), which still exists. Nothing was changed. Run the "
            "installer of that installation instead, or repeat this with --force if this "
            "installation should take over.", code="host_install_conflict")


def _choose_eln(options: Options, env: Environment, plan: _Plan) -> None:
    """Question 3: the ELN address the extension is built for."""
    say = env.say
    root = env.project_root
    saved, problem = read_saved_eln_origin(root)
    if problem and (options.asks or options.eln_origin is not None or options.skip_extension):
        say(f"  warning: the saved ELN address is not usable ({problem}).")
    if options.eln_origin is not None:
        origin = parse_eln_origin(options.eln_origin)
    elif options.skip_extension or root is None:
        return
    elif problem and not options.asks:
        raise _needs_answer(
            f"The saved ELN address is not usable ({problem}), and this run asks nothing, so "
            "the extension would be built for another ELN than before. Pass --eln-origin URL "
            f"with the address of your ELN (the default is {DEFAULT_ELN_ORIGIN}).")
    elif options.asks:
        say("")
        say("Address of your Sciformation ELN (https://host, without a path). The extension only")
        say("works on this address.")
        default = saved or DEFAULT_ELN_ORIGIN
        while True:
            answer = _ask_text(env, "ELN address", default)
            try:
                origin = parse_eln_origin(answer)
                break
            except RegistryError as exc:
                say(f"  {exc.message}")
    else:
        origin = saved or DEFAULT_ELN_ORIGIN
    plan.eln_origin = origin
    plan.save_eln = root is not None and origin != saved
    plan.build = root is not None and not options.skip_extension


def _choose_mnova(options: Options, env: Environment, plan: _Plan) -> dict[str, Any]:
    """Question 4: the MestReNova add-on (asked only where MestReNova is)."""
    say = env.say
    try:
        status = env.mnova_status()
    except Exception as exc:  # noqa: BLE001 - an optional part must not stop the installation
        say(f"  warning: MestReNova could not be looked for ({type(exc).__name__}: {exc}).")
        status = {"supported": False, "installed": False, "mestrenova": {"found": False}}
    supported = bool(status.get("supported"))
    installed = bool(status.get("installed"))
    found = bool((status.get("mestrenova") or {}).get("found")) or installed
    if options.mnova == "no":
        return status
    if options.mnova == "yes":
        if supported:
            plan.mnova = True
        else:
            plan.notes.append("The MestReNova add-on is installed by this program on Windows "
                              "only; it was left out.")
        return status
    if not supported or not found:
        return status
    if not options.asks:
        plan.mnova = installed
        return status
    say("")
    version = (status.get("mestrenova") or {}).get("version")
    say(f"MestReNova{' ' + str(version) if version else ''} was found on this PC. The add-on "
        "assigns spectra to compound IDs")
    if installed:
        say("and is installed already; it has to match this program version.")
    else:
        say("and saves them into the compound folders.")
    plan.mnova = _ask_yes_no(
        env, "Bring the MestReNova add-on up to date?" if installed
        else "Install the MestReNova add-on?", installed)
    return status


def _recognition_started(status: dict[str, Any]) -> bool:
    """Whether the recognition folder exists at all (an installation that was
    begun), as opposed to never installed."""
    home = status.get("home")
    try:
        return bool(home) and Path(home).is_dir()
    except OSError:
        return False


def _choose_recognition(options: Options, env: Environment, plan: _Plan) -> dict[str, Any]:
    """Question 5: the optional ChemRecogniser ("Capture reaction"). It is a
    large download, so a run that asks nothing never starts it by itself:
    ``--yes`` leaves it out, ``--upgrade`` only brings an existing one up to
    date."""
    say = env.say
    try:
        status = env.recognition_status()
    except Exception as exc:  # noqa: BLE001 - an optional part must not stop the installation
        say(f"  warning: {RECOGNISER_NAME} could not be looked for ({type(exc).__name__}: {exc}).")
        status = {"installed": False, "home": None, "problem": None}
    installed = bool(status.get("installed"))
    # Begun but incomplete, or made for an older version: worth repairing.
    present = installed or _recognition_started(status)
    if options.recognition == "no":
        return status
    if options.recognition == "yes":
        plan.recognition = True
        return status
    if not options.asks:
        plan.recognition = options.upgrade and present
        return status
    say("")
    say(f"{RECOGNISER_NAME} reads a drawn reaction scheme from a picture of the screen and")
    say("gives the SMILES of its compounds ('Capture reaction' in the extension). It is")
    say(f"optional and runs on this PC only. Size of the install: {RECOGNISER_SIZE} on disk,")
    say("most of it downloaded now; it uses about 2.5 GB of memory while it runs.")
    if installed:
        say("It is installed already; it has to match this program version.")
        question = f"Bring {RECOGNISER_NAME} up to date?"
    elif present:
        say(f"An installation was found but is not complete: {status.get('problem')}.")
        question = f"Complete the installation of {RECOGNISER_NAME}?"
    else:
        question = f"Install {RECOGNISER_NAME} ({RECOGNISER_SIZE})?"
    plan.recognition = _ask_yes_no(env, question, present)
    return status


# --------------------------------------------------------------------------- the actions

def _write_local_config(root: Path, origin: str) -> Path:
    path = root / "extension" / LOCAL_CONFIG_FILE
    install_host._write_atomic(path, json.dumps({"elnOrigin": origin}, indent=2) + "\n")
    return path


def _tail(output: str, lines: int = 15) -> list[str]:
    return [line.rstrip() for line in output.strip().splitlines()[-lines:]]


def _nodejs_ways_out(origin: str) -> str:
    text = ("- Install Node.js (the LTS version, https://nodejs.org), then run the installer "
            "again.")
    if origin == DEFAULT_ELN_ORIGIN:
        text += (f"\n- Or download the ready-made extension packages of version {__version__} "
                 f"from\n  {_RELEASES} (they are built for {DEFAULT_ELN_ORIGIN}).")
    else:
        text += ("\n  (The ready-made packages of the project cannot be used: they are built "
                 f"for\n  {DEFAULT_ELN_ORIGIN}, not for {origin}.)")
    return text


def _npm_commands(browsers: Sequence[str], origin: str) -> list[list[str]]:
    scripts = {"firefox": "package", "chrome": "package:chrome"}
    return [["run", scripts[b], "--", "--force", "--eln-origin", origin] for b in browsers]


def _needs_npm_ci(ext: Path) -> bool:
    """True when ``node_modules`` is missing or older than package-lock.json
    (npm writes ``node_modules/.package-lock.json`` at every installation)."""
    marker = ext / "node_modules" / ".package-lock.json"
    try:
        return (ext / "package-lock.json").stat().st_mtime > marker.stat().st_mtime
    except OSError:
        return True


def _build_extension(env: Environment, plan: _Plan, dry_run: bool) -> tuple[str | None, bool]:
    """Build and package the extension for the chosen browsers. Returns
    ``(note, failed)``: ``note`` is None when the packages are there, else
    the text saying why not and what to do; ``failed`` is true when the build
    was tried and broke (missing Node.js is not a failure)."""
    say = env.say
    root, origin = env.project_root, plan.eln_origin
    assert root is not None and origin is not None
    ext = root / "extension"
    commands = _npm_commands(plan.browsers, origin)
    node, npm = env.which("node"), env.which("npm")
    if dry_run:
        if not node or not npm:
            say("Would NOT build the extension: Node.js (node and npm) was not found.")
            return f"Node.js was not found.\n{_nodejs_ways_out(origin)}", False
        if _needs_npm_ci(ext):
            say(f"Would run in {ext}: npm ci")
        for command in commands:
            say(f"Would run in {ext}: npm {' '.join(command)}")
        paths = extension_paths(root)
        for browser in plan.browsers:
            keys = (("firefox_folder", "firefox_package") if browser == "firefox"
                    else ("chrome_folder", "chrome_package"))
            for key in keys:
                say(f"Would write {paths[key]}")
        return None, False
    if not node or not npm:
        return f"Node.js was not found.\n{_nodejs_ways_out(origin)}", False
    code, output = env.run([node, "--version"], ext)
    if code != 0 or not node_version_ok(output):
        return (f"Node.js {output.strip() or '(unknown version)'} is too old: the build needs "
                f"version {_NODE_NEEDS}.\n{_nodejs_ways_out(origin)}"), False
    steps = ([["ci"]] if _needs_npm_ci(ext) else []) + commands
    for command in steps:
        say(f"==> npm {' '.join(command)}   (in {ext}; this can take a minute)")
        code, output = env.run([npm, *command], ext)
        if code != 0:
            for line in _tail(output):
                say(f"    {line}")
            return (f"'npm {' '.join(command)}' failed in {ext} (exit code {code}); its last "
                    "lines are shown above.\n- Fix what it reports and run the installer "
                    "again."), True
    for line in _tail(output, 3):
        say(f"    {line}")
    return None, False


def _extension_missing(root: Path, browsers: Sequence[str]) -> bool:
    """True when what the browser steps name was not built before."""
    paths = extension_paths(root)
    wanted = [paths["firefox_package"]] if "firefox" in browsers else []
    if "chrome" in browsers:
        wanted.append(paths["chrome_folder"] / "manifest.json")
    return not all(path.is_file() for path in wanted)


def _show_plan(env: Environment, plan: _Plan, dry_run: bool) -> None:
    say = env.say
    say("")
    say("This would be done:" if dry_run else "This will be done:")
    if plan.create and len(plan.location.missing) == 2:
        say(f"  - create a NEW, EMPTY registry in {plan.home}"
            f" (ID format {plan.id_format or IdFormat.DEFAULT})")
    elif plan.create and _creates_database(plan.location):
        say(f"  - create a NEW, EMPTY database for the registry in {plan.home}")
    elif plan.create:
        say(f"  - write the missing settings file of the registry in {plan.home}")
    else:
        say(f"  - use the registry in {plan.home}")
    names = " and ".join(_LABELS[b] for b in plan.browsers)
    say(f"  - register the registry program with {names}"
        + (" (replacing the registration of another installation)" if plan.force else ""))
    if plan.switch_from:
        say(f"    and switch from the registry {plan.switch_from}")
    if plan.build:
        say(f"  - build the extension for {names}, for the ELN at {plan.eln_origin}")
    elif plan.save_eln:
        say(f"  - save the ELN address {plan.eln_origin} for the next build of the extension")
    if plan.mnova:
        say("  - install the MestReNova add-on (or bring it up to date)")
    if plan.recognition:
        say(f"  - install {RECOGNISER_NAME} (or bring it up to date): {RECOGNISER_SIZE} on disk, "
            "downloaded from the internet if it is not there yet")
    for note in plan.notes:
        say(f"  note: {note}")


def _registration_files(target: install_host.HostTarget, browsers: Sequence[str]) -> list[str]:
    items = [str(target.sidecar_path)]
    for browser in browsers:
        items.append(str(target.browser_manifest_path(browser)))
        if target.platform == "windows":
            items.append("registry key " + install_host._KEYS[browser][1])
    return items


def run_setup(options: Options, env: Environment) -> int:
    """Run the installation. Returns the exit code: 0, or 1 when the host is
    registered but building the extension failed. Refusals raise
    ``RegistryError`` before anything is changed."""
    say = env.say
    if options.asks and not env.can_ask:
        raise _needs_answer(
            "The installer has questions, but nobody can answer them here (the input is not a "
            "terminal). Run it in a terminal, or pass --yes (take every default) or --upgrade "
            "(keep everything as installed) together with the options that are needed "
            "(--home, --browser, ...).")
    if options.mnova not in (None, "yes", "no"):
        raise RegistryError("--mnova takes yes or no.", code="invalid_argument")
    if options.home is not None and " --" in options.home:
        raise RegistryError(
            f"--home {options.home!r} contains ' --': an option seems to have become a part of "
            "the folder name (on Windows, a quoted folder that ends in a backslash does this). "
            "Give the folder without a separator at its end. Nothing was changed.",
            code="invalid_argument")
    wanted_format = _wanted_format(options)
    target = env.target if env.target is not None else install_host.default_target()
    if not target.launcher.is_file():
        raise RegistryError(
            f"The host program {target.launcher} does not exist. Run the project installer "
            "(install.ps1 on Windows, install.sh on macOS/Linux) so that it is created, then "
            "try again.", code="host_launcher_missing")
    now = install_host.configured(target=target)

    say(f"ChemLinker {__version__} - "
        + ("update" if options.upgrade else "installation")
        + (" (dry run: nothing is changed)" if options.dry_run else ""))
    plan = _choose_folder(options, env, now, wanted_format)
    _choose_browsers(options, env, now, target, plan)
    _choose_eln(options, env, plan)
    _choose_mnova(options, env, plan)
    recognition_now = _choose_recognition(options, env, plan)
    _show_plan(env, plan, options.dry_run)
    if options.asks and not options.dry_run and not _ask_yes_no(env, "Continue?", True):
        raise _Cancelled("Stopped before anything was changed.")
    say("")

    root = env.project_root
    if options.dry_run:
        if plan.create:
            for missing in plan.location.missing:
                say(f"Would create {missing}")
        for item in _registration_files(target, plan.browsers):
            say(f"Would write {item}")
        if plan.save_eln and root is not None:
            say(f"Would write {root / 'extension' / LOCAL_CONFIG_FILE}")
        if plan.build:
            _build_extension(env, plan, dry_run=True)
        if plan.mnova:
            say("Would install the MestReNova add-on into the script folder of MestReNova.")
        if plan.recognition:
            say(f"Would install {RECOGNISER_NAME} ({RECOGNISER_SIZE}) into "
                f"{recognition_now.get('home') or 'its folder'}.")
        if not plan.create:
            say(f"Would check the stored structures of the registry in {plan.home} if the "
                "chemistry version changed.")
        say("")
        say("Dry run: nothing was changed.")
        return 0

    # 1. The registry. (The warning about a synced folder was shown with the question.)
    sync_warning = _sync_warning(plan.location)
    if plan.create:
        warnings = env.create_registry(plan.home, plan.id_format)
        banner = "=" * 64
        if len(plan.location.missing) == 2:
            say(banner)
            say(f"  A NEW, EMPTY registry was created in {plan.home}")
            say(banner)
        elif _creates_database(plan.location):
            say(banner)
            say(f"  A NEW, EMPTY database was created in {plan.home}")
            say(banner)
        else:
            say(f"The settings file {plan.location.missing[0]} was written; the database is "
                "used as it is.")
        for warning in warnings:
            if warning != sync_warning:
                say(f"  warning: {warning}")

    # 2. The browser registration.
    result = env.install(plan.home, browsers=plan.browsers, force=plan.force, target=target)
    say(f"The registry program is registered with "
        f"{' and '.join(_LABELS[b] for b in plan.browsers)} for the current user.")
    for message in result.get("messages") or []:
        say(f"  - {message}")
    for warning in result.get("warnings") or []:
        if warning != sync_warning:
            say(f"  warning: {warning}")

    # 3. The extension.
    build_note: str | None = None
    build_failed = False
    download = False
    if plan.save_eln and root is not None and plan.eln_origin is not None:
        saved_in = _write_local_config(root, plan.eln_origin)
        say(f"Saved the ELN address {plan.eln_origin} in {saved_in}.")
    if options.skip_extension:
        say("The extension was not built (--skip-extension).")
    if root is None:
        build_note = ("This program does not run from its project folder, so the extension "
                      "cannot be built here.\n- Get the extension packages from "
                      f"{_RELEASES}.")
        download = True
    elif options.skip_extension:
        if _extension_missing(root, plan.browsers):
            build_note = ("--skip-extension was given, and no extension built earlier was "
                          "found.\n- Run the installer again without --skip-extension.")
    else:
        build_note, build_failed = _build_extension(env, plan, dry_run=False)
        if build_note is None:
            say("The extension was built.")
        else:
            download = not build_failed and plan.eln_origin == DEFAULT_ELN_ORIGIN

    # 4. The MestReNova add-on.
    mnova_done = False
    if plan.mnova:
        try:
            installed = env.mnova_install()
        except RegistryError as exc:
            say(f"  warning: the MestReNova add-on was not installed: {exc.message}")
        else:
            mnova_done = True
            done = "was brought up to date" if installed.get("replaced") else "was installed"
            say(f"The MestReNova add-on {done} in {installed.get('scripts_dir')}.")

    # 5. The optional ChemRecogniser (a long download; it reports its own progress).
    recognition_done = bool(recognition_now.get("installed"))
    if plan.recognition:
        say("")
        say(f"Installing {RECOGNISER_NAME} ({RECOGNISER_SIZE}). This can take a while; it can "
            "be interrupted and repeated.")
        try:
            env.recognition_install(say)
        except RegistryError as exc:
            recognition_done = False
            say(f"  warning: {RECOGNISER_NAME} was not installed: {exc.message}")
            say("  Everything else is installed. To try again later, run "
                "'compound-registry install-recognition'.")
        except Exception as exc:  # noqa: BLE001 - an optional part must not stop the installation
            recognition_done = False
            say(f"  warning: {RECOGNISER_NAME} was not installed ({type(exc).__name__}: {exc}).")
        else:
            recognition_done = True
            say(f"{RECOGNISER_NAME} is installed.")

    # 6. The structures of an existing registry, after a change of the chemistry.
    if not plan.create:
        try:
            check = env.upgrade_registry(plan.home)
        except RegistryError as exc:
            say(f"  warning: the registry could not be checked: {exc.message}")
        else:
            say(f"Registry check: {check.get('message')}")

    # 7. What is left.
    say("")
    say("What to do now:")
    for line in next_steps(plan.browsers, root=root, build_note=build_note,
                           mnova=mnova_done, upgrade=options.upgrade, download=download):
        say(line)
    say("")
    say(_UPDATE_HINT)
    if not recognition_done:
        say(_RECOGNITION_HINT)
    return 1 if build_failed else 0
