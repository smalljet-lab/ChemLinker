"""Loading ``config.toml`` from the registry home folder.

The home folder is ``$COMPOUND_REGISTRY_HOME`` if set, else
``~/CompoundRegistry``. It holds ``config.toml``; by default also the live
database, the ``backups`` folder and the ``compounds`` folder (one folder per
compound, see :mod:`compound_registry.files`).
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .errors import ConfigError
from .idformat import IdFormat
from .models import MODE_OVERRIDES

CONFIG_FILENAME = "config.toml"
HOME_ENV_VAR = "COMPOUND_REGISTRY_HOME"

#: Defaults of the optional keys of config.toml. Paths are relative to the
#: home folder.
DEFAULT_DATABASE = "registry.sqlite"
DEFAULT_BACKUP_FOLDER = "backups"
DEFAULT_INTERVAL_HOURS = 24
DEFAULT_KEEP = 30
DEFAULT_STEREO_MODE = "abs"
#: Default of ``files.folder`` (relative to the home folder).
DEFAULT_FILES_FOLDER = "compounds"

# The commented file written for a new registry; <NAME> stands for a default.
_CONFIG_TEMPLATE = """\
# Compound registry configuration.
# Relative paths are relative to the folder that contains this file; ~ is expanded.

[id]
# Template of an ID: CORE_STEREO_BATCH, each number zero-padded to a fixed width.
# Do NOT change this once the first ID has been issued: the database remembers
# the format and refuses to open with a different one.
# IDs are also folder and file names: around the numbers only the letters
# A-Z and a-z, '_', '-' and '.' are allowed.
format = "<ID_FORMAT>"

[storage]
# The live database. Keep it on a local disk, NOT in OneDrive / Google Drive /
# Dropbox / iCloud or on a network drive (sync clients can corrupt SQLite files).
database = "<DATABASE>"

[backup]
# Backups may go to a synced folder; they are written completely before they appear.
# Every registry needs a backup folder of its own: do not point two registries
# at the same folder.
folder = "<BACKUP_FOLDER>"
interval_hours = <INTERVAL_HOURS>
keep = <KEEP>                         # 0 = keep every backup

[chemistry]
default_stereo_mode = "<STEREO_MODE>"       # one of abs, rac, rel, undefined

[files]
# One folder per compound is made here when an ID is created; other programs,
# e.g. the MestReNova add-on, save spectra into it. May be a synced or network folder.
folder = "<FILES_FOLDER>"
"""


def config_text(id_format: str | None = None) -> str:
    """The text of a new ``config.toml``: every key with its default, and
    ``id_format`` (default :attr:`IdFormat.DEFAULT`) as the ID template.
    Raises ``ConfigError`` for an invalid template."""
    template = IdFormat(id_format if id_format is not None else IdFormat.DEFAULT).template
    text = _CONFIG_TEMPLATE
    for name, value in (
        ("ID_FORMAT", template),
        ("DATABASE", DEFAULT_DATABASE),
        ("BACKUP_FOLDER", DEFAULT_BACKUP_FOLDER),
        ("INTERVAL_HOURS", str(DEFAULT_INTERVAL_HOURS)),
        ("KEEP", str(DEFAULT_KEEP)),
        ("STEREO_MODE", DEFAULT_STEREO_MODE),
        ("FILES_FOLDER", DEFAULT_FILES_FOLDER),
    ):
        text = text.replace(f"<{name}>", value)
    return text


DEFAULT_CONFIG_TEXT = config_text()

# Keys allowed in each known section. Unknown top-level tables are ignored so
# other components can add their own sections; unknown keys inside these
# sections are rejected because they are almost always typos.
_KNOWN_KEYS: dict[str, frozenset[str]] = {
    "id": frozenset({"format"}),
    "storage": frozenset({"database"}),
    "backup": frozenset({"folder", "interval_hours", "keep"}),
    "chemistry": frozenset({"default_stereo_mode"}),
    "files": frozenset({"folder"}),
}


@dataclass(frozen=True)
class Config:
    """Resolved configuration. All paths are absolute."""

    home: Path
    config_path: Path
    db_path: Path
    backup_dir: Path
    backup_interval_hours: float = float(DEFAULT_INTERVAL_HOURS)
    backup_keep: int = DEFAULT_KEEP
    id_format: str = IdFormat.DEFAULT
    default_stereo_mode: str = DEFAULT_STEREO_MODE
    #: The folder holding one folder per compound (``files.folder``). None
    #: (only when a Config is built by hand) means ``<home>/compounds``.
    files_dir: Path = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.files_dir is None:
            object.__setattr__(self, "files_dir",
                               _absolute(Path(self.home) / DEFAULT_FILES_FOLDER))


def _require_absolute_home(path: Path, origin: str) -> Path:
    """``path`` (``~`` expanded) if it is absolute, else ``ConfigError``.

    A relative home would make the registry depend on the current working
    directory: two programs started in different folders would then use two
    different registries and could issue the same IDs. On Windows, drive-
    relative (``D:reg``) and root-relative (``\\reg``) paths are relative too.
    """
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        raise ConfigError(
            f"The registry home folder from {origin} must be an absolute path, got "
            f"{str(path)!r}. A relative path would depend on the current folder, and two "
            f"programs could end up with two registries issuing the same IDs."
        )
    return expanded


def default_home() -> Path:
    """``$COMPOUND_REGISTRY_HOME`` if set (and not blank), else ``~/CompoundRegistry``.

    Raises ``ConfigError`` if the variable holds a relative path.
    """
    value = os.environ.get(HOME_ENV_VAR, "").strip()
    if value:
        return _require_absolute_home(Path(value), f"${HOME_ENV_VAR}")
    return Path.home() / "CompoundRegistry"


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_drive_relative(text: str) -> bool:
    """True for a Windows drive-relative path such as ``D:reg.sqlite``: it names
    a drive but is relative to that drive's (per-process) current folder."""
    pure = PureWindowsPath(text)
    return bool(pure.drive) and not pure.root and not pure.drive.startswith(("\\\\", "//"))


def _write_default(home: Path, config_path: Path) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        with open(config_path, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(DEFAULT_CONFIG_TEXT)
    except FileExistsError:
        pass  # created concurrently by another process: use that one
    except OSError as exc:
        raise ConfigError(f"Cannot create {config_path}: {exc}") from None


def _read(config_path: Path) -> dict:
    try:
        text = config_path.read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"Cannot read {config_path}: {exc}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{config_path} is not valid UTF-8 text") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from None


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] in config.toml must be a table")
    unknown = sorted(set(value) - _KNOWN_KEYS[name])
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in [{name}] of config.toml: {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(_KNOWN_KEYS[name]))})"
        )
    return value


def _path_value(section: dict, key: str, full_key: str, default: str, home: Path) -> Path:
    value = section.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{full_key} must be a non-empty path string, got {value!r}")
    if _is_drive_relative(value.strip()):
        raise ConfigError(
            f"{full_key} = {value!r} is relative to the current folder of drive "
            f"{PureWindowsPath(value.strip()).drive}, which changes from program to program. "
            f"Use a full path such as 'D:/folder/name' or a path relative to the "
            f"registry home folder."
        )
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = home / path
    return _absolute(path)


def _number_value(section: dict, key: str, full_key: str, default: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{full_key} must be a number, got {value!r}")
    if not math.isfinite(value) or value < 0:
        raise ConfigError(f"{full_key} must be a finite number >= 0, got {value!r}")
    return float(value)


def _int_value(section: dict, key: str, full_key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{full_key} must be a whole number, got {value!r}")
    if value < 0:
        raise ConfigError(f"{full_key} must be >= 0, got {value!r}")
    return value


def load_config(home: Path | None = None, create: bool = True) -> Config:
    """Read ``<home>/config.toml`` and return a validated :class:`Config`.

    If the file is missing and ``create`` is true, the home folder and a
    commented default file are created. If it is missing and ``create`` is
    false, built-in defaults are used and nothing is written.

    Raises ``ConfigError`` naming the offending key for invalid values, and
    for a relative ``home`` (argument or ``$COMPOUND_REGISTRY_HOME``).
    """
    if home is not None:
        home = _require_absolute_home(Path(home), "the home argument")
    home = _absolute(home if home is not None else default_home())
    config_path = home / CONFIG_FILENAME

    if config_path.exists():
        data = _read(config_path)
    elif create:
        _write_default(home, config_path)
        data = _read(config_path)
    else:
        data = {}

    id_sec = _section(data, "id")
    storage = _section(data, "storage")
    backup = _section(data, "backup")
    chem = _section(data, "chemistry")
    files = _section(data, "files")

    id_format = id_sec.get("format", IdFormat.DEFAULT)
    if not isinstance(id_format, str):
        raise ConfigError(f"id.format must be a string, got {id_format!r}")
    try:
        IdFormat(id_format)
    except ConfigError as exc:
        raise ConfigError(f"id.format is invalid: {exc.message}") from None

    db_path = _path_value(storage, "database", "storage.database", DEFAULT_DATABASE, home)
    backup_dir = _path_value(backup, "folder", "backup.folder", DEFAULT_BACKUP_FOLDER, home)
    if os.path.normcase(str(db_path)) == os.path.normcase(str(backup_dir)):
        raise ConfigError("backup.folder must not be the same path as storage.database")
    files_dir = _path_value(files, "folder", "files.folder", DEFAULT_FILES_FOLDER, home)
    if os.path.normcase(str(db_path)) == os.path.normcase(str(files_dir)):
        raise ConfigError("files.folder must not be the same path as storage.database")
    interval = _number_value(backup, "interval_hours", "backup.interval_hours",
                             float(DEFAULT_INTERVAL_HOURS))
    keep = _int_value(backup, "keep", "backup.keep", DEFAULT_KEEP)

    mode = chem.get("default_stereo_mode", DEFAULT_STEREO_MODE)
    if mode not in MODE_OVERRIDES:
        raise ConfigError(
            f"chemistry.default_stereo_mode must be one of {', '.join(MODE_OVERRIDES)}, got {mode!r}"
        )

    return Config(
        home=home,
        config_path=config_path,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_interval_hours=interval,
        backup_keep=keep,
        id_format=id_format,
        default_stereo_mode=mode,
        files_dir=files_dir,
    )


# --------------------------------------------------------------------------- sync folders

_SYNC_NAMES_EXACT = {
    "onedrive": "OneDrive",
    "google drive": "Google Drive",
    "googledrive": "Google Drive",
    "my drive": "Google Drive",
    "dropbox": "Dropbox",
    "iclouddrive": "iCloud Drive",
    "icloud drive": "iCloud Drive",
}
_SYNC_NAME_PREFIXES = {
    "onedrive - ": "OneDrive",      # OneDrive for Business: "OneDrive - Company"
    "dropbox (": "Dropbox",         # Dropbox Business: "Dropbox (Company)"
}


def _is_within(path: Path, root: Path) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


def _is_network_drive(path: Path) -> bool:
    text = str(path)
    if text.startswith(("\\\\", "//")):
        return True  # UNC path \\server\share
    if os.name != "nt":
        return False
    drive = PureWindowsPath(text).drive
    if not drive or len(drive) != 2 or drive[1] != ":":
        return False
    try:
        import ctypes

        DRIVE_REMOTE = 4
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE  # type: ignore[attr-defined]
    except Exception:
        return False


def sync_folder_warning(path: Path) -> str | None:
    """A human-readable warning if ``path`` (the LIVE database) appears to be
    inside a cloud-sync folder or on a network drive, else ``None``.

    Sync clients and network file systems can corrupt or fork a SQLite file
    that is being written. The backup folder may be a sync folder; do not call
    this for it.
    """
    path = _absolute(Path(path).expanduser())
    advice = (
        "Keep the live database on a local disk (set [storage] database in config.toml) "
        "and use the backup folder for syncing."
    )

    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(var, "").strip()
        if root and _is_within(path, Path(root)):
            return f"The registry database {path} is inside OneDrive ({root}). {advice}"

    for part in path.parts:
        name = part.casefold().rstrip("\\/")
        service = _SYNC_NAMES_EXACT.get(name)
        if service is None:
            for prefix, svc in _SYNC_NAME_PREFIXES.items():
                if name.startswith(prefix):
                    service = svc
                    break
        if service is not None:
            return f"The registry database {path} appears to be inside {service} ('{part}'). {advice}"

    if _is_network_drive(path):
        return f"The registry database {path} is on a network drive. {advice}"
    return None
