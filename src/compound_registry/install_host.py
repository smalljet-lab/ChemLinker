"""Register the native-messaging host with the browser (Firefox and Chrome)
for the current user.

No administrator rights are needed. What is written where:

* **Windows** - the host manifest
  ``%LOCALAPPDATA%\\CompoundRegistry\\native-host\\compound_registry.json``
  (outside the virtual environment, so recreating ``.venv`` keeps the host
  configuration) and the registry key
  ``HKCU\\Software\\Mozilla\\NativeMessagingHosts\\compound_registry`` whose
  default value (REG_SZ) is the full path of that manifest.
* **macOS** - ``~/Library/Application Support/Mozilla/NativeMessagingHosts/compound_registry.json``.
* **Linux** - ``~/.mozilla/native-messaging-hosts/compound_registry.json``.

The manifest's ``path`` is the ``compound-registry-host`` launcher of the
Python environment that ran the installer (``<sys.prefix>``).

Which registry the extension uses
---------------------------------
A *host configuration* ``compound_registry.config.json``
(``{"home": "<absolute path>"}``) next to the manifest pins the registry
folder; without it the host uses ``$COMPOUND_REGISTRY_HOME`` /
``~/CompoundRegistry``. :func:`install` never changes that choice silently:
``home`` pins a folder, ``default_home=True`` removes the pin, and otherwise
an existing pin is kept. The registry must exist (``config.toml`` and the
database) - the host never creates one, because a new empty registry would
issue IDs that already exist - so :func:`install` creates it only when asked
(``create_registry`` or a confirmed prompt) and otherwise refuses with
``registry_missing`` before writing anything.

A registration that belongs to another installation (its launcher exists and
is not this environment's) is replaced only with ``force``.

Chrome
------
``browsers=("chrome",)`` (or ``("firefox", "chrome")``) registers the same
launcher for the Chrome build of the extension (manifest key
``allowed_origins`` = [:data:`~compound_registry.protocol.CHROME_ORIGIN`]):

* **Windows** - ``compound_registry.chrome.json`` in the SAME folder as the
  Firefox manifest, and ``HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\compound_registry``.
* **macOS** - ``~/Library/Application Support/Google/Chrome/NativeMessagingHosts/compound_registry.json``.
* **Linux** - ``$XDG_CONFIG_HOME/google-chrome/NativeMessagingHosts/compound_registry.json``
  (``$XDG_CONFIG_HOME`` defaults to ``~/.config``, as in Chrome).

Both browsers use ONE host configuration, :attr:`HostTarget.sidecar_path`
(on Windows the folder of both manifests; on macOS / Linux the Firefox
manifest folder, because Chrome needs its manifest in its own folder) - see
:func:`host_config_path`. So both browsers always use the same registry.
Registering Chrome alone never changes that registry: when anything is
configured already (a registration or a host configuration), a ``home`` /
``default_home`` that names another registry is refused
(``host_install_conflict``); ``browsers=("firefox", "chrome")`` (or Firefox
alone, which Chrome then follows) is the way to switch. While Chrome uses
the shared host configuration it is never left without a pin: "default
registry" is then written as the folder the installer resolves, because
unpinned each browser would use ``$COMPOUND_REGISTRY_HOME`` as its own
process sees it (and a host started by Chrome refuses an unpinned
registration). Uninstalling one
browser keeps the host configuration while the other browser's registration
still uses it.

Everything that touches the machine (platform, folders, launcher, Windows
registry) is gathered in a :class:`HostTarget`, so tests can point all of it
at temporary folders and an in-memory registry.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

from .errors import ConfigError, RegistryError
from .protocol import CHROME_ORIGIN, EXTENSION_ID, HOST_NAME

__all__ = [
    "MANIFEST_NAME", "SIDECAR_NAME", "REGISTRY_KEY", "REGISTRY_KEY_DISPLAY", "PLATFORMS",
    "CHROME_MANIFEST_NAME", "CHROME_REGISTRY_KEY", "CHROME_REGISTRY_KEY_DISPLAY", "BROWSERS",
    "RegistryAccess", "WindowsRegistry", "HostTarget", "RegistryLocation", "current_platform",
    "default_target", "manifest_document", "chrome_manifest_document", "host_config_path",
    "chrome_host_config_paths", "firefox_host_config_path", "read_host_config", "locate_registry",
    "registry_missing_error", "install", "uninstall", "status", "configured",
]

MANIFEST_NAME = f"{HOST_NAME}.json"
SIDECAR_NAME = f"{HOST_NAME}.config.json"
REGISTRY_KEY = rf"Software\Mozilla\NativeMessagingHosts\{HOST_NAME}"
REGISTRY_KEY_DISPLAY = rf"HKCU\{REGISTRY_KEY}"
#: Windows file name of the Chrome manifest (next to the Firefox manifest).
CHROME_MANIFEST_NAME = f"{HOST_NAME}.chrome.json"
CHROME_REGISTRY_KEY = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
CHROME_REGISTRY_KEY_DISPLAY = rf"HKCU\{CHROME_REGISTRY_KEY}"
LAUNCHER_NAME = "compound-registry-host"
DESCRIPTION = "Compound ID registry: local permanent compound IDs for the Sciformation ELN"
PLATFORMS = ("windows", "macos", "linux")
BROWSERS = ("firefox", "chrome")
_LABELS = {"firefox": "Firefox", "chrome": "Chrome"}
_KEYS = {"firefox": (REGISTRY_KEY, REGISTRY_KEY_DISPLAY),
         "chrome": (CHROME_REGISTRY_KEY, CHROME_REGISTRY_KEY_DISPLAY)}
_ALLOWED = {"firefox": "allowed_extensions", "chrome": "allowed_origins"}

#: Called with the registry folder when none exists there; True = create it.
ConfirmCreate = Callable[[str], bool]


# --------------------------------------------------------------------------- Windows registry

class RegistryAccess(Protocol):
    """The three registry operations the installer needs (all under HKCU)."""

    def read_default(self, key: str) -> str | None:
        """Default value of ``key``, or None if the key / value does not exist."""

    def write_default(self, key: str, value: str) -> None:
        """Create ``key`` if needed and set its default value (REG_SZ)."""

    def delete_key(self, key: str) -> bool:
        """Delete ``key``; False if it did not exist."""


class WindowsRegistry:
    """:class:`RegistryAccess` on the real ``HKEY_CURRENT_USER`` (``winreg``)."""

    def read_default(self, key: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, "")
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) else None

    def write_default(self, key: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_WRITE) as handle:
            winreg.SetValueEx(handle, "", 0, winreg.REG_SZ, value)

    def delete_key(self, key: str) -> bool:
        import winreg

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            return False
        return True


# --------------------------------------------------------------------------- target

def current_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


@dataclass(frozen=True)
class HostTarget:
    """Where the host is installed. ``registry`` is only used on Windows;
    ``chrome_manifest_dir`` is Chrome's manifest folder on macOS / Linux (on
    Windows the Chrome manifest goes into ``manifest_dir``)."""

    platform: str
    manifest_dir: Path
    launcher: Path
    registry: RegistryAccess | None = None
    chrome_manifest_dir: Path | None = None

    @property
    def manifest_path(self) -> Path:
        return self.manifest_dir / MANIFEST_NAME

    @property
    def chrome_manifest_path(self) -> Path | None:
        """The Chrome manifest of this installation (None: Chrome unknown here)."""
        if self.platform == "windows":
            return self.manifest_dir / CHROME_MANIFEST_NAME
        return self.chrome_manifest_dir / MANIFEST_NAME if self.chrome_manifest_dir else None

    def browser_manifest_path(self, browser: str) -> Path | None:
        return self.manifest_path if browser == "firefox" else self.chrome_manifest_path

    @property
    def sidecar_path(self) -> Path:
        return self.manifest_dir / SIDECAR_NAME

    def reg(self) -> RegistryAccess:
        if self.registry is None:
            raise RegistryError("No Windows registry access configured.", code="host_install_failed")
        return self.registry


def default_target(*, platform: str | None = None, prefix: Path | str | None = None,
                   user_home: Path | str | None = None, local_appdata: Path | str | None = None,
                   registry: RegistryAccess | None = None,
                   config_home: Path | str | None = None) -> HostTarget:
    """The target for this machine and this Python environment (``sys.prefix``).

    Windows manifest folder: ``<local_appdata>\\CompoundRegistry\\native-host``,
    where ``local_appdata`` defaults to ``%LOCALAPPDATA%`` - or, when
    ``user_home`` is given (tests) or the variable is unset,
    ``<user_home>\\AppData\\Local``.

    Linux Chrome manifest folder: ``<config_home>/google-chrome/NativeMessagingHosts``,
    where ``config_home`` defaults to ``$XDG_CONFIG_HOME`` (Chrome keeps its
    user data folder there) - or, when ``user_home`` is given (tests) or the
    variable is unset or not absolute, ``<user_home>/.config``.
    """
    platform = platform or current_platform()
    if platform not in PLATFORMS:
        raise RegistryError(f"Unknown platform {platform!r}.", code="invalid_argument")
    prefix_path = Path(prefix if prefix is not None else sys.prefix)
    home = Path(user_home) if user_home is not None else Path.home()
    if platform == "windows":
        if local_appdata is None:
            env_value = os.environ.get("LOCALAPPDATA", "").strip()
            local_appdata = (env_value if env_value and user_home is None
                             else home / "AppData" / "Local")
        return HostTarget(platform, Path(local_appdata) / "CompoundRegistry" / "native-host",
                          prefix_path / "Scripts" / f"{LAUNCHER_NAME}.exe",
                          registry if registry is not None else WindowsRegistry())
    launcher = prefix_path / "bin" / LAUNCHER_NAME
    if platform == "macos":
        folder = home / "Library" / "Application Support" / "Mozilla" / "NativeMessagingHosts"
        chrome = home / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"
    else:
        folder = home / ".mozilla" / "native-messaging-hosts"
        if config_home is None:
            xdg = os.environ.get("XDG_CONFIG_HOME", "")
            config_home = (xdg if xdg and user_home is None and PurePosixPath(xdg).is_absolute()
                           else home / ".config")
        chrome = Path(config_home) / "google-chrome" / "NativeMessagingHosts"
    return HostTarget(platform, folder, launcher, registry, chrome_manifest_dir=chrome)


# --------------------------------------------------------------------------- files

def _same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _failed(action: str, path: Path, exc: OSError) -> RegistryError:
    return RegistryError(f"Could not {action} {path}: {exc.strerror or exc}",
                         code="host_install_failed")


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` as UTF-8 (no BOM, ``\\n`` newlines) via temp file + os.replace."""
    data = text.encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    except OSError as exc:
        raise _failed("write", path, exc) from None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise _failed("write", path, exc) from None


def _json_text(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _remove(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _failed("remove", path, exc) from None
    return True


def manifest_document(launcher: Path | str) -> dict[str, Any]:
    """The host manifest Firefox reads."""
    return {
        "name": HOST_NAME,
        "description": DESCRIPTION,
        "path": str(launcher),
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }


def chrome_manifest_document(launcher: Path | str) -> dict[str, Any]:
    """The host manifest Chrome reads."""
    return {
        "name": HOST_NAME,
        "description": DESCRIPTION,
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [CHROME_ORIGIN],
    }


def _manifest_document(browser: str, launcher: Path | str) -> dict[str, Any]:
    return manifest_document(launcher) if browser == "firefox" else chrome_manifest_document(launcher)


def host_config_path(target: HostTarget, browser: str, manifest_path: Path) -> Path:
    """The host configuration a host started through ``manifest_path`` uses.

    Firefox (and Chrome on Windows): the file next to that manifest. Chrome on
    macOS / Linux: Chrome's manifest has to live in Chrome's own folder, so it
    shares the host configuration of the Firefox manifest folder
    (:attr:`HostTarget.sidecar_path`)."""
    if browser == "firefox" or target.platform == "windows":
        return manifest_path.parent / SIDECAR_NAME
    return target.sidecar_path


def chrome_host_config_paths(target: HostTarget) -> tuple[Path, Path | None]:
    """For a host started by Chrome: (the host configuration of Chrome's
    registration, the one of Firefox's registration or None when Firefox is
    not registered). Read-only. Raises ``ConfigError`` when Chrome's
    registration cannot be found - the host then refuses instead of guessing
    a registry."""
    reg = _current_registration(target, "chrome")
    where = (CHROME_REGISTRY_KEY_DISPLAY if target.platform == "windows"
             else str(target.chrome_manifest_path))
    if reg.read_problems or reg.manifest_path is None or not reg.manifest_path.is_file():
        detail = (reg.read_problems[0] if reg.read_problems
                  else f"no manifest {reg.manifest_path}" if reg.manifest_path is not None
                  else f"nothing registered at {where}")
        raise ConfigError(
            f"Chrome started the registry host, but Chrome's registration of it cannot be found "
            f"({detail}), so it is not known which registry to use. The extension does not "
            "fall back to another registry, because IDs could then be issued in the wrong "
            "one. Run 'compound-registry install-host --browser chrome' (install.ps1 -Browser "
            "Chrome) again.")
    chrome = host_config_path(target, "chrome", reg.manifest_path)
    fx = _current_registration(target, "firefox")
    if fx.read_problems:
        # Unknown is not "not registered": Firefox may be pinned to another registry.
        raise ConfigError(
            f"Chrome started the registry host, but Firefox's registration of it cannot be "
            f"read ({fx.read_problems[0]}), so it is not known whether both browsers use the "
            "same registry. The extension does not pick one, because IDs could then be "
            "issued in the wrong registry. Fix the access to the registry key, or run "
            "'compound-registry host-status' to see what is wrong.")
    if fx.manifest_path is None or not fx.manifest_path.is_file():
        return chrome, None
    firefox = firefox_host_config_path(fx.manifest_path)
    if firefox is None:
        raise ConfigError(
            f"Chrome started the registry host, but Firefox's registration uses the manifest "
            f"{fx.manifest_path}, which is not named {MANIFEST_NAME}: Firefox's host then "
            "ignores any host configuration and uses $COMPOUND_REGISTRY_HOME as Firefox sees "
            "it, which a host started by Chrome cannot know. The extension does not pick a "
            "registry, because IDs could then be issued in the wrong one. Run "
            "'compound-registry install-host --browser both' so that both browsers share one "
            "host configuration.")
    return chrome, firefox


def firefox_host_config_path(manifest_path: Path) -> Path | None:
    """The host configuration a host started by Firefox with ``manifest_path``
    reads: the file next to it when the manifest is named
    :data:`MANIFEST_NAME`, else None (that host then uses
    ``$COMPOUND_REGISTRY_HOME`` / the default as Firefox's process sees it).
    The one rule :func:`compound_registry.host.resolve_home` follows."""
    if os.path.normcase(manifest_path.name) != os.path.normcase(MANIFEST_NAME):
        return None
    return manifest_path.parent / SIDECAR_NAME


def read_host_config(path: Path) -> tuple[str | None, str | None]:
    """Read the host configuration ``compound_registry.config.json``.

    Returns ``(home, None)`` when it is valid, else ``(None, problem)`` with a
    short description of what is wrong. Never raises.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return None, f"it cannot be read ({exc.strerror or exc.__class__.__name__})"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "it is not UTF-8 text"
    try:
        document = json.loads(text)
    except (ValueError, RecursionError) as exc:
        where = (f" (line {exc.lineno}, column {exc.colno})"
                 if isinstance(exc, json.JSONDecodeError) else "")
        return None, f"it is not valid JSON{where}"
    if not isinstance(document, dict):
        return None, "it must be a JSON object such as {\"home\": \"C:/path/to/registry\"}"
    home = document.get("home")
    if not isinstance(home, str) or not home.strip():
        return None, "it has no text value \"home\""
    if not Path(home).is_absolute():
        return None, f"\"home\" must be an absolute path, got {home!r}"
    return home, None


def _read_manifest(path: Path, browser: str = "firefox") -> tuple[dict[str, Any], str | None]:
    try:
        document = json.loads(path.read_bytes().decode("utf-8-sig"))
    except OSError as exc:
        return {}, f"it cannot be read ({exc.strerror or exc.__class__.__name__})"
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}, "it is not valid UTF-8 JSON"
    if not isinstance(document, dict):
        return {}, "it is not a JSON object"
    if document.get("name") != HOST_NAME:
        return {}, f"its name is {document.get('name')!r}, expected {HOST_NAME!r}"
    if document.get("type") != "stdio":
        return {}, f"its type is {document.get('type')!r}, expected 'stdio'"
    if not isinstance(document.get("path"), str) or not document["path"].strip():
        return {}, "it has no program path"
    allowed = document.get(_ALLOWED[browser])
    if not isinstance(allowed, list):
        return {}, f"it has no list '{_ALLOWED[browser]}'"
    return document, None


def _manifest_launcher(manifest_path: Path, document: dict[str, Any], platform: str) -> Path:
    launcher = Path(document["path"])
    if not launcher.is_absolute() and platform == "windows":
        launcher = manifest_path.parent / launcher   # Firefox: relative to the manifest
    return launcher


# --------------------------------------------------------------------------- the registry itself

@dataclass(frozen=True)
class RegistryLocation:
    """A registry folder and what of it is missing (read-only lookup)."""

    config: Any                     # compound_registry.config.Config
    missing: tuple[str, ...]        # paths of config.toml / database that do not exist

    @property
    def home(self) -> str:
        return str(self.config.home)

    @property
    def exists(self) -> bool:
        return not self.missing


def locate_registry(home: Path | str | None) -> RegistryLocation:
    """Where the registry for ``home`` (None: ``$COMPOUND_REGISTRY_HOME`` /
    default) is and whether ``config.toml`` and the database exist. Creates
    nothing. Raises ``ConfigError`` for a relative home or an invalid
    ``config.toml``."""
    from .config import load_config  # light import (no RDKit)

    config = load_config(Path(home) if home is not None else None, create=False)
    missing = []
    if not Path(config.config_path).is_file():
        missing.append(str(config.config_path))
    if not Path(config.db_path).is_file():
        missing.append(str(config.db_path))
    return RegistryLocation(config, tuple(missing))


def registry_missing_error(location: RegistryLocation, *, installer: bool = False,
                           cli: bool = False) -> RegistryError:
    """``registry_missing``: where it looked, why nothing is created, what to check.
    The wording is for the extension by default, for ``install-host`` with
    ``installer`` and for the other CLI commands with ``cli``."""
    what = " and ".join(location.missing)
    who = "This command" if cli else "The extension"
    text = (f"No registry was found in {location.home} (missing: {what}). "
            f"{who} never creates a registry, because a new, empty one would issue IDs "
            "that already exist in your notebooks. Check whether the registry folder was "
            "moved or renamed, whether its drive is connected and whether a sync has "
            "finished. ")
    if cli:
        text += ("If the registry is elsewhere, pass --home with its folder. Only for a FIRST "
                 "installation, create it with 'compound-registry init' (with --home PATH for "
                 "a folder other than the default). Nothing was done.")
    elif installer:
        text += ("If the registry is elsewhere, pass --home with its folder. Only for a FIRST "
                 "installation, create it with --create-registry (install.ps1 -CreateRegistry). "
                 "Nothing was installed.")
    else:
        text += ("A first installation creates the registry with install.ps1 / install.sh or "
                 "'compound-registry install-host --create-registry'.")
    return RegistryError(text, code="registry_missing")


def _registry_summary(location: RegistryLocation, created: bool) -> dict[str, Any]:
    from .service import RegistryService  # RDKit; only needed here

    with RegistryService(location.config) as svc:
        doctor = svc.doctor()
    stats = doctor.get("stats") or {}
    return {
        "home": location.home,
        "config_path": str(location.config.config_path),
        "db_path": str(location.config.db_path),
        "created": created,
        "batches": stats.get("batches"),
        "retired": stats.get("retired"),
        "integrity": doctor.get("integrity"),
    }


def _default_home_text() -> tuple[str | None, str | None]:
    """(path, problem) of the registry used without a host configuration,
    as seen from THIS process's environment."""
    from .config import default_home

    try:
        return str(default_home()), None
    except RegistryError as exc:
        return None, exc.message


# --------------------------------------------------------------------------- current registration

@dataclass
class _Registration:
    """What Firefox currently finds for HOST_NAME."""

    manifest_path: Path | None = None     # None: nothing registered
    registry_value: str | None = None
    manifest: dict[str, Any] | None = None
    problem: str | None = None            # manifest missing / unusable
    launcher: Path | None = None
    read_problems: list[str] = field(default_factory=list)

    def kind(self, target: HostTarget) -> str:
        """``none`` | ``ours`` | ``broken`` (manifest missing / unusable) |
        ``stale`` (another launcher that no longer exists) | ``other``."""
        if self.manifest_path is None:
            return "none"
        if self.launcher is None:
            return "broken"
        if _same_path(self.launcher, target.launcher):
            return "ours"
        return "other" if self.launcher.is_file() else "stale"


def _current_registration(target: HostTarget, browser: str = "firefox") -> _Registration:
    """What ``browser`` currently finds (nothing for Chrome when the target
    knows no Chrome folder)."""
    reg = _Registration()
    key, display = _KEYS[browser]
    if target.platform == "windows":
        try:
            reg.registry_value = target.reg().read_default(key)
        except OSError as exc:
            reg.read_problems.append(f"The registry key {display} cannot be read: "
                                     f"{exc.strerror or exc}")
        if reg.registry_value:
            reg.manifest_path = Path(reg.registry_value)
    else:
        own = target.browser_manifest_path(browser)
        if own is not None and own.is_file():
            reg.manifest_path = own
    if reg.manifest_path is None:
        return reg
    if not reg.manifest_path.is_file():
        reg.problem = "the file is missing"
        return reg
    document, problem = _read_manifest(reg.manifest_path, browser)
    if problem is not None:
        reg.problem = problem
        return reg
    reg.manifest = document
    reg.launcher = _manifest_launcher(reg.manifest_path, document, target.platform)
    return reg


def _names_our_launcher(manifest_path: Path, target: HostTarget, browser: str = "firefox") -> bool:
    if not manifest_path.is_file():
        return False
    document, problem = _read_manifest(manifest_path, browser)
    return problem is None and _same_path(
        _manifest_launcher(manifest_path, document, target.platform), target.launcher)


def _conflict_error(reg: _Registration, action: str, browser: str = "firefox") -> RegistryError:
    return RegistryError(
        f"{_LABELS[browser]}'s registration of the registry host belongs to ANOTHER installation (program "
        f"{reg.launcher}, manifest {reg.manifest_path}), which still exists. {action} it would "
        "switch the extension away from that installation and possibly to another registry. "
        "Nothing was changed. Run the command from that installation instead, or repeat it "
        "with --force if this installation should take over.", code="host_install_conflict")


# --------------------------------------------------------------------------- install

def _browsers(browsers: Any) -> tuple[str, ...]:
    """The requested browsers, validated, in :data:`BROWSERS` order
    (``"both"`` = all of them)."""
    if isinstance(browsers, str):
        browsers = BROWSERS if browsers == "both" else (browsers,)
    wanted = tuple(browsers or ())
    unknown = [b for b in wanted if b not in BROWSERS]
    if unknown or not wanted:
        raise RegistryError(f"Unknown browser {(unknown or [''])[0]!r}: use firefox, chrome or "
                            "both.", code="invalid_argument")
    return tuple(b for b in BROWSERS if b in wanted)


def _registrations(target: HostTarget) -> dict[str, _Registration]:
    regs = {"firefox": _current_registration(target, "firefox")}
    if target.chrome_manifest_path is not None:
        regs["chrome"] = _current_registration(target, "chrome")
    return regs


def _no_chrome_error() -> RegistryError:
    return RegistryError("No Chrome manifest folder is known for this installation target.",
                         code="host_install_failed")


def _invalid_existing_error(sidecar: Path, problem: str, hint: str = "") -> ConfigError:
    return ConfigError(
        f"The existing host configuration {sidecar} is invalid: {problem}. It is "
        "not clear which registry the extension should use, so nothing was "
        "installed. Run install-host again with --home PATH (the registry to use) "
        f"or --default-home (the default registry){hint}.")


def _existing_choice(target: HostTarget, regs: dict[str, _Registration],
                     hint: str = "") -> tuple[bool, str | None]:
    """The registry configured now: the host configurations of the existing
    registrations (Firefox's and Chrome's) and this installation's.

    Returns (anything configured, normalised home or None = default
    registry). ``ConfigError`` when one is invalid or when they name
    different registries (then it is not clear which one is meant)."""
    sources: list[tuple[str, Path, bool]] = []   # (who, host configuration, read it)
    for browser, reg in regs.items():
        if reg.manifest_path is not None and reg.manifest_path.is_file():
            who = f"{_LABELS[browser]}'s registration"
            sidecar = (firefox_host_config_path(reg.manifest_path) if browser == "firefox"
                       else host_config_path(target, browser, reg.manifest_path))
            if sidecar is None:     # Firefox ignores any host configuration: unpinned
                sources.append((f"{who} (manifest not named {MANIFEST_NAME})",
                                reg.manifest_path, False))
            else:
                sources.append((who, sidecar, True))
    if target.sidecar_path.exists():
        sources.append(("this installation", target.sidecar_path, True))
    seen: list[tuple[str, Path, str | None]] = []
    for who, sidecar, readable in sources:
        if any(_same_path(sidecar, s) for _w, s, _v in seen):
            continue
        value = None
        if readable and sidecar.exists():
            kept, problem = read_host_config(sidecar)
            if problem is not None:
                raise _invalid_existing_error(sidecar, problem, hint)
            value = os.path.normpath(kept)
        seen.append((who, sidecar, value))
    if len({os.path.normcase(v) if v is not None else None for _w, _s, v in seen}) > 1:
        listing = "; ".join(f"{who}: {sidecar} -> {value or 'the default registry'}"
                            for who, sidecar, value in seen)
        raise ConfigError(
            f"The host configurations name DIFFERENT registries ({listing}). It is not clear "
            "which registry the extension should use in Firefox and Chrome, so nothing was "
            "installed. Choose it for both browsers: 'compound-registry install-host --browser "
            "both --home PATH' (or --default-home).")
    return bool(seen), (seen[0][2] if seen else None)


def _same_home(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return _same_path(a, b)


def _repoint_error(current: str | None, requested: str | None) -> RegistryError:
    return RegistryError(
        f"Registering Chrome must not change the registry the extension uses. It is "
        f"{current or 'the default registry'} now (Firefox and Chrome share one host "
        f"configuration), and this command asks for {requested or 'the default registry'}. "
        "Nothing was installed. Run it without --home / --default-home to register Chrome "
        "with the current registry, or switch the registry for BOTH browsers with "
        "'compound-registry install-host --browser both --home PATH' (or --default-home).",
        code="host_install_conflict")


def _previous(reg: _Registration, kind: str) -> dict[str, Any] | None:
    if kind == "none":
        return None
    return {"kind": kind, "manifest_path": str(reg.manifest_path),
            "launcher": str(reg.launcher) if reg.launcher else None}


def install(home: Path | str | None = None, *, default_home: bool = False,
            create_registry: bool = False, force: bool = False,
            confirm_create: ConfirmCreate | None = None,
            target: HostTarget | None = None,
            browsers: Any = ("firefox",),
            id_format: str | None = None) -> dict[str, Any]:
    """Register the host for Firefox (and / or Chrome: ``browsers``).
    Returns what was done; ``warnings`` also say when the registry database
    lies in a cloud-sync folder or on a network drive.

    Registry choice: ``home`` pins that folder; ``default_home`` removes a
    pin (default registry); neither keeps an existing valid pin (an invalid
    one is an error). The chosen registry must exist; if it does not, it is
    created when ``create_registry`` is true or ``confirm_create(path)``
    returns true, otherwise ``registry_missing`` is raised. ``id_format`` is
    the ID template of a registry created here (see
    ``service.create_registry``); it is refused (``invalid_argument``) when
    the registry already exists. Every check runs
    before anything is written: a refusal writes nothing.

    Chrome alone never changes the registry already configured (see the
    module documentation); the result's top-level manifest keys describe the
    first browser (Firefox when it is included), ``registrations`` all of them.
    When Chrome uses the shared host configuration, an unpinned choice is
    pinned to the folder resolved now (``home_choice`` ``"default-pinned"``).
    An unreadable registration of either browser is refused
    (``host_install_failed``): unknown is never treated as "not registered".
    """
    target = target if target is not None else default_target()
    browsers = _browsers(browsers)
    if home is not None and default_home:
        raise RegistryError("Give either --home or --default-home, not both.",
                            code="invalid_argument")
    if not target.launcher.is_file():
        raise RegistryError(
            f"The host program {target.launcher} does not exist. Run the project installer "
            f"(install.ps1 on Windows, install.sh on macOS/Linux) so that it is created, "
            f"then try again.", code="host_launcher_missing")
    if "chrome" in browsers and target.chrome_manifest_path is None:
        raise _no_chrome_error()

    messages: list[str] = []
    warnings: list[str] = []
    regs = _registrations(target)
    for browser, reg in regs.items():
        # Both browsers share one registry, so an unreadable registration of
        # EITHER one is unknown, never "not registered" (that could hide a pin).
        if reg.read_problems:
            raise RegistryError(
                f"{reg.read_problems[0]}. It is not known whether or how {_LABELS[browser]} is "
                "registered (and so which registry it uses), so nothing was installed. Fix the "
                "access to that registry key and try again.", code="host_install_failed")
    kinds = {browser: reg.kind(target) for browser, reg in regs.items()}
    for browser in browsers:
        if kinds[browser] == "other" and not force:
            raise _conflict_error(regs[browser], "Replacing", browser)
    current = regs["firefox"]
    chrome_only = browsers == ("chrome",)
    chrome_hint = " together with --browser both" if chrome_only else ""
    if chrome_only and current.manifest_path is not None and not _same_path(
            current.manifest_path, target.manifest_path):
        raise RegistryError(
            f"Firefox's registration of the registry host uses the manifest "
            f"{current.manifest_path}, not {target.manifest_path}, so Chrome could not share "
            "its host configuration and might use another registry. Nothing was changed. Run "
            "'compound-registry install-host --browser both' (install.ps1 -Browser Both) so "
            "that both browsers are registered with one host configuration.",
            code="host_install_conflict")

    # 1. Which registry.
    pinned: str | None
    if home is not None:
        home_path = Path(home).expanduser()
        if not str(home).strip() or not home_path.is_absolute():
            raise RegistryError(
                f"The registry folder for the extension must be an absolute path, got "
                f"{str(home)!r}.", code="invalid_argument")
        pinned, home_choice = os.path.normpath(str(home_path)), "pinned"
    elif default_home:
        pinned, home_choice = None, "default"
    elif "chrome" in browsers:
        _configured, kept_home = _existing_choice(target, regs, chrome_hint)
        pinned, home_choice = (kept_home, "kept") if kept_home is not None else (None, "default")
        if kept_home is not None:
            messages.append(f"Keeping the registry {pinned} chosen at a previous installation "
                            "(use --home or --default-home to change it).")
    else:
        pinned, home_choice = None, "default"
        candidates = []
        if current.manifest_path is not None:
            used = firefox_host_config_path(current.manifest_path)
            if used is not None:
                candidates.append(used)
        candidates.append(target.sidecar_path)
        for sidecar in candidates:
            if not sidecar.exists():
                continue
            kept, problem = read_host_config(sidecar)
            if problem is not None:
                raise _invalid_existing_error(sidecar, problem)
            pinned, home_choice = os.path.normpath(kept), "kept"
            messages.append(f"Keeping the registry {pinned} chosen at a previous installation "
                            "(use --home or --default-home to change it).")
            break
    if chrome_only:
        # Chrome shares the host configuration: registering it must never
        # move Firefox (or an existing Chrome registration) to another registry.
        configured, current_home = _existing_choice(target, regs, chrome_hint)
        if configured and not _same_home(current_home, pinned):
            raise _repoint_error(current_home, pinned)

    # 2. The registry must exist (or be created on request) - before writing anything.
    location = locate_registry(pinned)
    created = False
    if location.exists and id_format is not None:
        raise RegistryError(
            f"An ID format can only be chosen for a new registry, but {location.home} already "
            "has one. The ID format of an existing registry cannot be changed.",
            code="invalid_argument")
    if not location.exists:
        if create_registry or (confirm_create is not None and confirm_create(location.home)):
            from .service import create_registry as make_registry

            _config, creation_warnings = make_registry(location.home, id_format)
            created = True
            location = locate_registry(pinned)
            if not location.exists:
                raise registry_missing_error(location, installer=True)
        else:
            raise registry_missing_error(location, installer=True)
    # Chrome uses the shared host configuration: never leave it unpinned, or
    # each browser would use $COMPOUND_REGISTRY_HOME as ITS environment has it
    # (a browser kept running since before a 'setx' differs) - possibly two
    # registries, which no host process can detect. Pin what we resolve now.
    chrome_reg = regs.get("chrome")
    chrome_shares = "chrome" in browsers or (
        chrome_reg is not None and chrome_reg.manifest_path is not None
        and _same_path(host_config_path(target, "chrome", chrome_reg.manifest_path),
                       target.sidecar_path))
    auto_pinned = pinned is None and chrome_shares
    if auto_pinned:
        pinned, home_choice = os.path.normpath(location.home), "default-pinned"
    registry = _registry_summary(location, created)
    if created:
        warnings.extend(creation_warnings)
    else:
        from .config import sync_folder_warning

        sync_warning = sync_folder_warning(Path(location.config.db_path))
        if sync_warning:
            warnings.append(sync_warning)

    # 3. Write: host configuration, manifests, registry values.
    sidecar_removed = False
    if pinned is not None:
        _write_atomic(target.sidecar_path, _json_text({"home": pinned}))
        if home_choice == "pinned":
            messages.append(f"Wrote the host configuration {target.sidecar_path} "
                            f"(registry {pinned}).")
        elif auto_pinned:
            messages.append(f"Pinned the registry {pinned} in the host configuration "
                            f"{target.sidecar_path}, which Firefox and Chrome share: unpinned, "
                            "each browser would use $COMPOUND_REGISTRY_HOME as its own "
                            "environment has it.")
            env_value = os.environ.get("COMPOUND_REGISTRY_HOME", "").strip()
            if env_value:
                warnings.append(
                    f"COMPOUND_REGISTRY_HOME is set here ({env_value}), so that registry is now "
                    "pinned for both browsers. If Firefox ran without this variable, it used "
                    "~/CompoundRegistry until now: check with 'compound-registry host-status' "
                    "and, if needed, pin the right registry with 'compound-registry "
                    "install-host --browser both --home PATH'.")
    else:
        sidecar_removed = _remove(target.sidecar_path)
        if sidecar_removed:
            messages.append(f"Removed the host configuration {target.sidecar_path}: the "
                            "extension now uses the default registry.")

    registrations: dict[str, dict[str, Any]] = {}
    for browser in browsers:
        manifest_path = target.browser_manifest_path(browser)
        assert manifest_path is not None
        document = _manifest_document(browser, target.launcher)
        _write_atomic(manifest_path, _json_text(document))
        label = "" if browser == "firefox" else "Chrome "
        messages.append(f"Wrote the {label}host manifest {manifest_path}.")
        registry_key = None
        if target.platform == "windows":
            key, display = _KEYS[browser]
            try:
                target.reg().write_default(key, str(manifest_path))
            except OSError as exc:
                raise RegistryError(f"Could not write the registry key {display}: "
                                    f"{exc.strerror or exc}", code="host_install_failed") from None
            registry_key = display
            messages.append(f"Set {display} to the {label}manifest path.")
        registrations[browser] = {
            "manifest_path": str(manifest_path), "manifest": document,
            "registry_key": registry_key,
            "previous_registration": _previous(regs[browser], kinds[browser])}

    for browser in browsers:
        reg, kind = regs[browser], kinds[browser]
        prefix = "" if browser == "firefox" else "Chrome: "
        if kind == "other":
            warnings.append(f"{prefix}Replaced the registration of another installation (program "
                            f"{reg.launcher}); that installation no longer serves the "
                            "extension.")
        elif kind == "stale":
            messages.append(f"{prefix}The previous registration pointed to {reg.launcher}, which "
                            "no longer exists (project folder moved?); it was replaced.")
        elif kind == "broken":
            messages.append(f"{prefix}The previous registration ({reg.manifest_path}) was "
                            f"unusable ({reg.problem}); it was replaced.")
    if chrome_only and kinds["firefox"] == "other":
        warnings.append(f"Firefox's registration belongs to another installation (program "
                        f"{current.launcher}). Both browsers use the same registry, but Firefox "
                        "starts that installation's host program.")
    chrome = regs.get("chrome")
    if "chrome" not in browsers and chrome is not None and chrome.manifest_path is not None:
        if _same_path(host_config_path(target, "chrome", chrome.manifest_path),
                      target.sidecar_path):
            messages.append("Chrome is registered too and shares this host configuration: it "
                            "uses the same registry.")
        else:
            warnings.append(f"Chrome's registration ({chrome.manifest_path}) does not share this "
                            "host configuration, so Chrome may use ANOTHER registry. Run "
                            "'compound-registry install-host --browser both'.")

    who = " and ".join(_LABELS[b] for b in browsers)
    one = len(browsers) == 1
    if pinned is not None:
        source, note = "host-config", "pinned in the host configuration"
    else:
        source = "default"
        note = (f"default registry: $COMPOUND_REGISTRY_HOME as {who} "
                f"{'sees' if one else 'see'} it, else ~/CompoundRegistry")
        env_value = os.environ.get("COMPOUND_REGISTRY_HOME", "").strip()
        if env_value:
            warnings.append(
                f"COMPOUND_REGISTRY_HOME is set here ({env_value}). The extension uses it only "
                f"if {who} {'is' if one else 'are'} started with the same variable; otherwise "
                "it uses ~/CompoundRegistry, a DIFFERENT registry. To be sure, run install-host "
                f"again with --home {env_value}.")

    primary = registrations[browsers[0]]
    return {
        "platform": target.platform,
        "browsers": list(browsers),
        "manifest_path": primary["manifest_path"],
        "manifest": primary["manifest"],
        "launcher": str(target.launcher),
        "registry_key": primary["registry_key"],
        "sidecar_path": str(target.sidecar_path),
        "home_override": pinned,
        "home_choice": home_choice,
        "sidecar_removed": sidecar_removed,
        "extension_home": location.home,
        "extension_home_source": source,
        "extension_home_note": note,
        "registry": registry,
        "registry_created": created,
        "previous_registration": primary["previous_registration"],
        "registrations": registrations,
        "messages": messages,
        "warnings": warnings,
    }


def uninstall(*, force: bool = False, target: HostTarget | None = None,
              browsers: Any = ("firefox",)) -> dict[str, Any]:
    """Remove the registration, the manifest and the host configuration of
    each of ``browsers`` - only when they belong to this environment (or the
    registered launcher no longer exists), else ``host_install_conflict``
    unless ``force``. The host configuration is kept while a registration of
    the other browser still uses it."""
    target = target if target is not None else default_target()
    browsers = _browsers(browsers)
    if "chrome" in browsers and target.chrome_manifest_path is None:
        raise _no_chrome_error()
    regs = _registrations(target)
    kinds = {browser: reg.kind(target) for browser, reg in regs.items()}
    for browser in browsers:
        if kinds[browser] == "other" and not force:
            raise _conflict_error(regs[browser], "Removing", browser)
    removed: list[str] = []
    messages: list[str] = []
    still_used = [(browser, host_config_path(target, browser, reg.manifest_path))
                  for browser, reg in regs.items()
                  if browser not in browsers and reg.manifest_path is not None]

    def remove_config(path: Path) -> None:
        users = [_LABELS[b] for b, p in still_used if _same_path(p, path)]
        if users:
            if path.exists():
                messages.append(f"Kept the host configuration {path}: {users[0]}'s registration "
                                "still uses it.")
        elif _remove(path):
            removed.append(str(path))

    for browser in browsers:
        current = regs[browser]
        key, display = _KEYS[browser]
        if target.platform == "windows" and current.registry_value is not None:
            try:
                if target.reg().delete_key(key):
                    removed.append(display)
            except OSError as exc:
                raise RegistryError(f"Could not remove the registry key {display}: "
                                    f"{exc.strerror or exc}", code="host_install_failed") from None
        # Files: the registered manifest when it is in one of our folders, and our
        # own manifest wherever it names this environment's launcher.
        own_places = [target.browser_manifest_path(browser)]
        for manifest in own_places:
            assert manifest is not None
            registered_here = (current.manifest_path is not None
                               and _same_path(current.manifest_path, manifest))
            if registered_here or _names_our_launcher(manifest, target, browser):
                remove_config(host_config_path(target, browser, manifest))
                if _remove(manifest):
                    removed.append(str(manifest))
        if current.manifest_path is not None and not any(
                _same_path(current.manifest_path, m) for m in own_places):
            messages.append(f"The registration pointed to the manifest {current.manifest_path}; "
                            "the registration was removed, that file was left in place.")
    if target.platform == "windows":
        try:
            target.manifest_dir.rmdir()  # our own folder; only when empty
        except OSError:
            pass
    for browser in browsers:
        if kinds[browser] == "other":
            messages.append(f"The registration of another installation (program "
                            f"{regs[browser].launcher}) was removed because --force was given.")
    if not removed:
        where = "" if browsers == ("firefox",) else f" for {' and '.join(_LABELS[b] for b in browsers)}"
        messages.append(f"Nothing to remove: the host was not registered{where}.")
    primary = regs[browsers[0]]
    return {
        "platform": target.platform,
        "browsers": list(browsers),
        "removed": removed,
        "previous_registry_value": primary.registry_value,
        "previous_launcher": str(primary.launcher) if primary.launcher else None,
        "manifest_path": str(target.browser_manifest_path(browsers[0])),
        "messages": messages,
    }


# --------------------------------------------------------------------------- status

def status(*, target: HostTarget | None = None, browser: str = "firefox") -> dict[str, Any]:
    """Read-only: is the host registered for ``browser`` and this
    environment, which registry does the extension use, and what is wrong?"""
    target = target if target is not None else default_target()
    if browser not in BROWSERS:
        raise RegistryError(f"Unknown browser {browser!r}: use firefox or chrome.",
                            code="invalid_argument")
    expected = target.browser_manifest_path(browser)
    if expected is None:
        raise _no_chrome_error()
    label = _LABELS[browser]
    rerun = ("'compound-registry install-host'" if browser == "firefox"
             else "'compound-registry install-host --browser chrome'")
    _key, display = _KEYS[browser]
    current = _current_registration(target, browser)
    problems: list[str] = list(current.read_problems)
    registered = current.manifest_path is not None
    manifest_path = current.manifest_path or expected
    if not registered:
        where = (f"no value in {display}" if target.platform == "windows"
                 else f"no manifest {manifest_path}")
        problems.append(f"The host is not registered for {label} ({where}). Run {rerun}.")
    elif not _same_path(manifest_path, expected):
        problems.append(f"{label} uses the host manifest {manifest_path}, not the one of this "
                        f"installation ({expected}). Run {rerun} from this project to use it.")

    manifest_exists = manifest_path.is_file()
    manifest_valid = current.manifest is not None
    if registered and not manifest_exists:
        problems.append(f"The host manifest {manifest_path} is missing. Run {rerun} again.")
    elif registered and current.problem is not None:
        problems.append(f"The host manifest {manifest_path} is not usable: {current.problem}. "
                        f"Run {rerun} again.")
    launcher = current.launcher
    allowed = list(current.manifest[_ALLOWED[browser]]) if current.manifest else None
    launcher_exists = launcher.is_file() if launcher is not None else False
    if launcher is not None:
        if not launcher_exists:
            problems.append(f"The host manifest points to {launcher}, which does not exist. "
                            "Run the project installer again.")
        if not _same_path(launcher, target.launcher):
            problems.append(f"The registered host belongs to another Python environment "
                            f"({launcher}), not to this one ({target.launcher}). Run "
                            f"{rerun} from this project to switch.")
    if browser == "firefox":
        if allowed is not None and allowed != [EXTENSION_ID]:
            problems.append(f"The host manifest allows the add-on(s) {allowed}, expected "
                            f"[{EXTENSION_ID!r}]. Run 'compound-registry install-host' again.")
    elif allowed is not None and allowed != [CHROME_ORIGIN]:
        problems.append(f"The host manifest allows the origin(s) {allowed}, expected "
                        f"[{CHROME_ORIGIN!r}]. Run {rerun} again.")

    sidecar = host_config_path(target, browser, manifest_path)
    if browser == "chrome" and registered:
        fx = _current_registration(target, "firefox")
        # What makes a host started by Chrome refuse (see chrome_host_config_paths).
        if fx.read_problems:
            problems.append(f"Firefox's registration cannot be read ({fx.read_problems[0]}), so "
                            "the extension refuses every request in Chrome: it cannot check "
                            "that both browsers use the same registry.")
        elif (fx.manifest_path is not None and fx.manifest_path.is_file()
              and firefox_host_config_path(fx.manifest_path) is None):
            problems.append(f"Firefox's registration uses the manifest {fx.manifest_path}, not "
                            f"named {MANIFEST_NAME}, so it ignores the host configuration and the "
                            "extension refuses every request in Chrome. Run 'compound-registry "
                            "install-host --browser both'.")
        if not sidecar.exists():
            problems.append(f"Chrome's registration has no host configuration ({sidecar}), so "
                            "the extension refuses every request in Chrome (the registry is not "
                            f"pinned). Run {rerun} again: it pins the registry.")
        if fx.manifest_path is not None and not _same_path(
                host_config_path(target, "firefox", fx.manifest_path), sidecar):
            problems.append(f"Chrome's host configuration {sidecar} is not the one Firefox uses "
                            f"({host_config_path(target, 'firefox', fx.manifest_path)}), so the "
                            "two browsers may use DIFFERENT registries. Run 'compound-registry "
                            "install-host --browser both'.")
    home_override = None
    sidecar_invalid = False
    if sidecar.exists():
        home_override, problem = read_host_config(sidecar)
        if problem is not None:
            sidecar_invalid = True
            problems.append(f"The host configuration {sidecar} is invalid: {problem}. The "
                            "extension's requests will fail until it is fixed or "
                            "'compound-registry install-host --home PATH' is run again.")

    # Which registry the extension uses, and does it exist?
    extension_home: str | None = None
    extension_source = "host-config" if (home_override or sidecar_invalid) else "default"
    registry_exists: bool | None = None
    if not sidecar_invalid:
        try:
            location = locate_registry(home_override)
        except RegistryError as exc:
            if home_override is None:
                extension_home, problem = _default_home_text()
            else:
                extension_home = home_override
            problems.append(f"The registry the extension uses cannot be checked: {exc.message}")
        else:
            extension_home = location.home
            registry_exists = location.exists
            if registered and not location.exists:
                problems.append(registry_missing_error(location).message)

    return {
        "platform": target.platform,
        "browser": browser,
        "registered": registered,
        "registry_key": display if target.platform == "windows" else None,
        "registry_value": current.registry_value,
        "manifest_path": str(manifest_path),
        "expected_manifest_path": str(expected),
        "manifest_exists": manifest_exists,
        "manifest_valid": manifest_valid,
        "launcher": str(launcher) if launcher is not None else None,
        "expected_launcher": str(target.launcher),
        "launcher_exists": launcher_exists,
        _ALLOWED[browser]: allowed,
        "sidecar_path": str(sidecar),
        "home_override": home_override,
        "extension_home": extension_home,
        "extension_home_source": extension_source,
        "extension_registry_exists": registry_exists,
        "problems": problems,
    }


def configured(*, target: HostTarget | None = None) -> dict[str, Any]:
    """Read-only: what is set up on this machine now.

    ``{"browsers": [...], "home": folder | None, "status": {browser: status}}``:
    ``browsers`` are those the host is registered for (in :data:`BROWSERS`
    order), ``home`` is the registry folder the first of them uses (None when
    nothing is registered or the folder cannot be told), ``status`` holds
    :func:`status` of every browser that could be asked. Never raises for a
    registration it cannot read: that browser then counts as not registered
    here and its status is missing."""
    target = target if target is not None else default_target()
    statuses: dict[str, dict[str, Any]] = {}
    for browser in BROWSERS:
        try:
            statuses[browser] = status(target=target, browser=browser)
        except (RegistryError, OSError):
            continue
    registered = [b for b in BROWSERS if statuses.get(b, {}).get("registered")]
    home = next((statuses[b]["extension_home"] for b in registered
                 if statuses[b].get("extension_home")), None)
    return {"browsers": registered, "home": home, "status": statuses}
