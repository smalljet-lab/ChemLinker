"""SQLite implementation of :class:`~compound_registry.repository.RegistryRepository`.

Design notes
------------
* One database file. Rows of ``cores``, ``stereos`` and ``batches`` are never
  deleted or changed (enforced by triggers); a batch may only be retired once.
  Because nothing is ever deleted, ``max(number) + 1`` never reuses a number.
* Every write runs inside one ``BEGIN IMMEDIATE`` transaction, which takes the
  database write lock up front, so concurrent processes are serialised and a
  failed registration (conflict, overflow, crash) consumes no number.
* ``journal_mode=DELETE`` + ``synchronous=FULL``: the most conservative mode,
  a single self-contained file when idle (no ``-wal``/``-shm`` side files).
* The keys (``core_key``, ``stereo_key``) are stored verbatim and are the
  lookup keys; they are not hashed.
* Timestamps are ISO 8601 UTC with seconds precision and a trailing ``Z``.
* An existing registry (a file with a ``meta`` table) that lacks one of its
  tables, or whose numbering is not gap-free, is refused with
  ``StorageError(code="registry_damaged")``; nothing is re-created, because an
  empty re-created table would hand out numbers again.
* Experiment references are stripped of surrounding whitespace everywhere.
* Schema 1 has two kinds of tables. ``meta``, ``cores``, ``stereos``,
  ``batches`` and ``events`` are permanent and required: an existing registry
  that lacks one of them is damaged. ``names``, ``spectra``, ``yields`` and
  ``reactions`` are annotation tables: their rows may change or disappear,
  every change appends an event, and a registry that lacks one of them (or one
  of their columns listed in ``_ANNOTATION_COLUMNS``) gets it on open.
* ``names`` holds at most one name (meant for the IUPAC name) per stereo
  variant, with its ``source`` (``user`` or ``chemdraw``, see
  ``protocol.NAME_SOURCES``). Events: ``name_set``, ``name_removed``.
* ``spectra`` holds what the MestReNova add-on recorded for a batch: one row
  per measured spectrum (``spectrum_key`` names the dataset), with a JSON
  document (multiplet report, acquisition parameters, file paths). Rows may
  be replaced. Event: ``spectrum_recorded``.
* ``yields`` holds the isolated and analytical yield (percent) of a batch,
  mirrored from the ELN: at most one row per batch, removed when the ELN
  shows no yield any more. Events: ``yield_set``, ``yield_removed``.
* ``reactions`` holds what an experiment page shows about its reaction
  (reactants, reagents, solvents, temperature, reference amount; see
  protocol.py, "Reactions"), one row per experiment (``experiment_ref``) as
  canonical JSON (keys sorted, no spaces), mirrored from the ELN and replaced
  as a whole when it differs. Event: ``reaction_set``, which names the
  experiment and the SHA-256 of the stored JSON, not the JSON. Its columns
  ``limiting_row`` / ``limiting_name`` hold the starting material the user
  chose for the experiment (``set_limiting``; both NULL = automatic); they are
  never touched by ``set_reaction`` (a choice survives every refresh of the
  reaction), and every change appends a ``limiting_set`` event.
* Opening reads first and writes only when something has to be done. A
  registry that is complete is opened inside one read transaction (structure,
  meta values and the numbering check), so its file is not modified and a
  read-only file can be opened for reading. A new file, a missing index,
  trigger, annotation table or column, a missing ``registry_id`` and a schema
  upgrade take the write lock; everything is then read again inside that
  transaction, because another program may have done the work meanwhile.
* ``meta.schema_version`` is the version of the permanent tables and their
  guards. A file with a HIGHER version is refused ("Update the program"). A
  file with a LOWER version is upgraded on open: the database is copied
  first (``<name>-before-schema-<N>-<UTC>.sqlite`` in the backup folder),
  then the steps of :data:`_MIGRATIONS` run in order inside the open's write
  transaction, ``schema_version`` is raised and a ``migrate`` event is
  appended. If a step fails, nothing is changed and the copy is removed.
* ``meta.registry_id`` names the registry itself (32 hex digits), so that
  journal lines and backup files in a folder shared with another registry
  can be told apart. A new registry gets a random one. A registry made
  before the value existed gets one derived from its creation time and ID
  format (:func:`derived_registry_id`), so that every copy and every older
  backup of that registry has the same identity. It never changes.
* ``meta.unstamped_journal`` says whose the lines of the issued-ID journal
  are that name no registry (see :mod:`compound_registry.journal` and
  :attr:`SqliteRepository.unstamped_journal_own`): ``own`` in a database
  created in a home folder whose journal has such lines, ``stamped`` once
  the journal holds a copy of each with the ``registry_id``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from . import __version__
from .config import Config
from .errors import (
    ChemistryError,
    FormatMismatchError,
    IdOverflowError,
    NotFoundError,
    RegistryError,
    SlotConflictError,
    StorageError,
)
from .files import same_path as _same_file
from .idformat import IdFormat
from .protocol import NAME_SOURCES
from .models import (
    STEREO_MODES,
    BatchRecord,
    CoreRecord,
    ExperimentRef,
    LookupResult,
    NameRecord,
    Normalized,
    RegistrationResult,
    StereoRecord,
    YieldRecord,
)

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 30_000
SOURCES = ("extension", "cli", "backfill")
IMMUTABLE_META_KEYS = ("schema_version", "id_format", "created_at")
#: Meta key of the registry's own identity (see :func:`registry_identity`).
REGISTRY_ID_KEY = "registry_id"
#: Meta keys that ``set_meta`` / ``record_event`` never change.
FIXED_META_KEYS = IMMUTABLE_META_KEYS + (REGISTRY_ID_KEY,)
#: Meta key: whose the journal lines without a ``registry_id`` are.
UNSTAMPED_JOURNAL_KEY = "unstamped_journal"
UNSTAMPED_OWN = "own"
UNSTAMPED_STAMPED = "stamped"

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# --------------------------------------------------------------------------- timestamps

def format_timestamp(moment: datetime) -> str:
    """ISO 8601 UTC, seconds precision, trailing ``Z``. Naive datetimes are taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_TS_FORMAT)


def parse_timestamp(text: str) -> datetime:
    """Inverse of :func:`format_timestamp` (returns an aware UTC datetime)."""
    return datetime.strptime(text, _TS_FORMAT).replace(tzinfo=timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- schema

_META_KEYS_SQL = ", ".join(f"'{k}'" for k in IMMUTABLE_META_KEYS)

# Batch columns that may never change after insert (everything but the two
# retirement columns).
_BATCH_FIXED_COLUMNS = (
    "batch_id", "core_no", "stereo_no", "batch_no", "full_id", "experiment_ref",
    "experiment_code", "experiment_url", "product_index", "molfile", "smiles",
    "inchikey", "inchi", "detected_mode", "mode_overridden", "input_format",
    "source", "created_at", "rdkit_version", "inchi_version",
)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS cores (
    core_no     INTEGER PRIMARY KEY CHECK (core_no >= 1),
    core_key    TEXT NOT NULL UNIQUE CHECK (length(core_key) > 0),
    inchikey    TEXT NOT NULL,
    formula     TEXT NOT NULL,
    created_at  TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS stereos (
    core_no         INTEGER NOT NULL REFERENCES cores(core_no),
    stereo_no       INTEGER NOT NULL CHECK (typeof(stereo_no) = 'integer' AND stereo_no >= 1),
    core_stereo_id  TEXT NOT NULL UNIQUE,
    stereo_key      TEXT NOT NULL,
    stereo_mode     TEXT NOT NULL CHECK (length(stereo_mode) > 0),
    inchikey        TEXT NOT NULL,
    smiles          TEXT NOT NULL,
    molfile         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (core_no, stereo_no),
    UNIQUE (core_no, stereo_key, stereo_mode)
)""",
    """CREATE TABLE IF NOT EXISTS batches (
    batch_id         INTEGER PRIMARY KEY,
    core_no          INTEGER NOT NULL,
    stereo_no        INTEGER NOT NULL,
    batch_no         INTEGER NOT NULL CHECK (typeof(batch_no) = 'integer' AND batch_no >= 1),
    full_id          TEXT NOT NULL UNIQUE,
    experiment_ref   TEXT NOT NULL CHECK (length(experiment_ref) > 0),
    experiment_code  TEXT,
    experiment_url   TEXT,
    product_index    INTEGER NOT NULL CHECK (typeof(product_index) = 'integer' AND product_index >= 1),
    molfile          TEXT NOT NULL,
    smiles           TEXT NOT NULL,
    inchikey         TEXT NOT NULL,
    inchi            TEXT NOT NULL,
    detected_mode    TEXT NOT NULL,
    mode_overridden  INTEGER NOT NULL CHECK (mode_overridden IN (0, 1)),
    input_format     TEXT NOT NULL,
    source           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    rdkit_version    TEXT NOT NULL,
    inchi_version    TEXT NOT NULL,
    retired_at       TEXT,
    retired_reason   TEXT,
    FOREIGN KEY (core_no, stereo_no) REFERENCES stereos(core_no, stereo_no),
    UNIQUE (core_no, stereo_no, batch_no),
    CHECK ((retired_at IS NULL AND retired_reason IS NULL)
           OR (retired_at IS NOT NULL AND length(trim(retired_reason)) > 0))
)""",
    """CREATE TABLE IF NOT EXISTS events (
    event_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    event     TEXT NOT NULL,
    full_id   TEXT,
    details   TEXT NOT NULL CHECK (json_valid(details))
)""",
    # --- annotation tables (not in _TABLES: created on open when missing) -----
    """CREATE TABLE IF NOT EXISTS names (
    core_stereo_id  TEXT PRIMARY KEY REFERENCES stereos(core_stereo_id),
    name            TEXT NOT NULL CHECK (length(trim(name)) > 0),
    updated_at      TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'user'
)""",
    """CREATE TABLE IF NOT EXISTS spectra (
    spectrum_id   INTEGER PRIMARY KEY,
    full_id       TEXT NOT NULL REFERENCES batches(full_id),
    spectrum_key  TEXT NOT NULL CHECK (length(trim(spectrum_key)) > 0),
    data          TEXT NOT NULL CHECK (json_valid(data)),
    updated_at    TEXT NOT NULL,
    UNIQUE (full_id, spectrum_key)
)""",
    """CREATE TABLE IF NOT EXISTS yields (
    full_id     TEXT PRIMARY KEY REFERENCES batches(full_id),
    isolated    REAL CHECK (isolated IS NULL OR isolated >= 0),
    analytical  REAL CHECK (analytical IS NULL OR analytical >= 0),
    updated_at  TEXT NOT NULL,
    CHECK (isolated IS NOT NULL OR analytical IS NOT NULL)
)""",
    """CREATE TABLE IF NOT EXISTS reactions (
    experiment_ref   TEXT PRIMARY KEY CHECK (length(experiment_ref) > 0),
    experiment_code  TEXT,
    experiment_url   TEXT,
    data             TEXT NOT NULL CHECK (json_valid(data)),
    updated_at       TEXT NOT NULL,
    limiting_row     INTEGER,
    limiting_name    TEXT
)""",
    # --- indexes -------------------------------------------------------------
    """CREATE UNIQUE INDEX IF NOT EXISTS batches_active_slot
    ON batches(experiment_ref, product_index) WHERE retired_at IS NULL""",
    """CREATE UNIQUE INDEX IF NOT EXISTS batches_active_compound
    ON batches(experiment_ref, core_no, stereo_no) WHERE retired_at IS NULL""",
    """CREATE INDEX IF NOT EXISTS batches_by_experiment
    ON batches(experiment_ref, product_index)""",
    """CREATE INDEX IF NOT EXISTS events_by_full_id ON events(full_id)""",
    # --- triggers: cores -----------------------------------------------------
    # The insert guards also block INSERT OR REPLACE, which would otherwise
    # delete the conflicting row without firing the DELETE triggers.
    """CREATE TRIGGER IF NOT EXISTS cores_insert_guard BEFORE INSERT ON cores
WHEN NEW.core_no IS NOT (SELECT coalesce(max(core_no), 0) + 1 FROM cores)
  OR EXISTS (SELECT 1 FROM cores WHERE core_key = NEW.core_key)
BEGIN
    SELECT RAISE(ABORT, 'registry: a new core must use the next CORE number and a new core_key');
END""",
    """CREATE TRIGGER IF NOT EXISTS cores_no_update BEFORE UPDATE ON cores
BEGIN
    SELECT RAISE(ABORT, 'registry: cores are permanent and cannot be changed');
END""",
    """CREATE TRIGGER IF NOT EXISTS cores_no_delete BEFORE DELETE ON cores
BEGIN
    SELECT RAISE(ABORT, 'registry: cores are permanent and cannot be deleted');
END""",
    # --- triggers: stereos ---------------------------------------------------
    """CREATE TRIGGER IF NOT EXISTS stereos_insert_guard BEFORE INSERT ON stereos
WHEN NEW.stereo_no IS NOT (SELECT coalesce(max(stereo_no), 0) + 1 FROM stereos WHERE core_no = NEW.core_no)
  OR EXISTS (SELECT 1 FROM stereos WHERE core_no = NEW.core_no
             AND stereo_key = NEW.stereo_key AND stereo_mode = NEW.stereo_mode)
  OR EXISTS (SELECT 1 FROM stereos WHERE core_stereo_id = NEW.core_stereo_id)
BEGIN
    SELECT RAISE(ABORT, 'registry: a new stereo variant must use the next STEREO number and a new key');
END""",
    """CREATE TRIGGER IF NOT EXISTS stereos_no_update BEFORE UPDATE ON stereos
BEGIN
    SELECT RAISE(ABORT, 'registry: stereo variants are permanent and cannot be changed');
END""",
    """CREATE TRIGGER IF NOT EXISTS stereos_no_delete BEFORE DELETE ON stereos
BEGIN
    SELECT RAISE(ABORT, 'registry: stereo variants are permanent and cannot be deleted');
END""",
    # --- triggers: batches ---------------------------------------------------
    """CREATE TRIGGER IF NOT EXISTS batches_insert_guard BEFORE INSERT ON batches
WHEN NEW.batch_no IS NOT (SELECT coalesce(max(batch_no), 0) + 1 FROM batches
                          WHERE core_no = NEW.core_no AND stereo_no = NEW.stereo_no)
  OR NEW.retired_at IS NOT NULL OR NEW.retired_reason IS NOT NULL
  OR EXISTS (SELECT 1 FROM batches WHERE batch_id = NEW.batch_id OR full_id = NEW.full_id)
  OR EXISTS (SELECT 1 FROM batches WHERE retired_at IS NULL AND experiment_ref = NEW.experiment_ref
             AND (product_index = NEW.product_index
                  OR (core_no = NEW.core_no AND stereo_no = NEW.stereo_no)))
BEGIN
    SELECT RAISE(ABORT, 'registry: a new batch must use the next BATCH number, a new ID and a free experiment slot');
END""",
    "CREATE TRIGGER IF NOT EXISTS batches_update_guard BEFORE UPDATE ON batches\n"
    "WHEN OLD.retired_at IS NOT NULL\n"
    "  OR NEW.retired_at IS NULL\n"
    "  OR NEW.retired_reason IS NULL OR length(trim(NEW.retired_reason)) = 0\n  OR "
    + "\n  OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _BATCH_FIXED_COLUMNS)
    + "\nBEGIN\n    SELECT RAISE(ABORT, 'registry: a batch can only be retired once; "
    "no other column may change');\nEND",
    """CREATE TRIGGER IF NOT EXISTS batches_no_delete BEFORE DELETE ON batches
BEGIN
    SELECT RAISE(ABORT, 'registry: batches are permanent and cannot be deleted (retire them instead)');
END""",
    # --- triggers: events (append-only) --------------------------------------
    """CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'registry: the event log is append-only');
END""",
    """CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'registry: the event log is append-only');
END""",
)

# The guards of the fixed meta keys. A schema upgrade drops them around the
# change of ``schema_version`` and creates them again (see _set_schema_version).
_META_GUARD_TRIGGERS = ("meta_fixed_no_replace", "meta_fixed_no_update", "meta_fixed_no_delete")
_META_GUARD_STATEMENTS: tuple[str, ...] = (
    f"""CREATE TRIGGER IF NOT EXISTS meta_fixed_no_replace BEFORE INSERT ON meta
WHEN NEW.key IN ({_META_KEYS_SQL}) AND EXISTS (SELECT 1 FROM meta WHERE key = NEW.key)
BEGIN
    SELECT RAISE(ABORT, 'registry: schema_version, id_format and created_at are fixed');
END""",
    f"""CREATE TRIGGER IF NOT EXISTS meta_fixed_no_update BEFORE UPDATE ON meta
WHEN OLD.key IN ({_META_KEYS_SQL}) OR NEW.key IN ({_META_KEYS_SQL})
BEGIN
    SELECT RAISE(ABORT, 'registry: schema_version, id_format and created_at are fixed');
END""",
    f"""CREATE TRIGGER IF NOT EXISTS meta_fixed_no_delete BEFORE DELETE ON meta
WHEN OLD.key IN ({_META_KEYS_SQL})
BEGIN
    SELECT RAISE(ABORT, 'registry: schema_version, id_format and created_at are fixed');
END""",
)

# The registry's identity is as permanent as the IDs it issues. (Guards of
# their own, so that a registry that already has the meta_fixed_* guards
# gets them on open.)
_REGISTRY_ID_GUARD_STATEMENTS: tuple[str, ...] = (
    f"""CREATE TRIGGER IF NOT EXISTS meta_registry_id_no_replace BEFORE INSERT ON meta
WHEN NEW.key = '{REGISTRY_ID_KEY}' AND EXISTS (SELECT 1 FROM meta WHERE key = NEW.key)
BEGIN
    SELECT RAISE(ABORT, 'registry: registry_id is fixed');
END""",
    f"""CREATE TRIGGER IF NOT EXISTS meta_registry_id_no_update BEFORE UPDATE ON meta
WHEN OLD.key = '{REGISTRY_ID_KEY}' OR NEW.key = '{REGISTRY_ID_KEY}'
BEGIN
    SELECT RAISE(ABORT, 'registry: registry_id is fixed');
END""",
    f"""CREATE TRIGGER IF NOT EXISTS meta_registry_id_no_delete BEFORE DELETE ON meta
WHEN OLD.key = '{REGISTRY_ID_KEY}'
BEGIN
    SELECT RAISE(ABORT, 'registry: registry_id is fixed');
END""",
)

SCHEMA_STATEMENTS = SCHEMA_STATEMENTS + _META_GUARD_STATEMENTS + _REGISTRY_ID_GUARD_STATEMENTS

_OBJECT_NAME = re.compile(r"CREATE (?:UNIQUE )?(?:TABLE|INDEX|TRIGGER) IF NOT EXISTS (\w+)")
#: The name of every table, index and trigger of the schema.
SCHEMA_OBJECTS = frozenset(_OBJECT_NAME.match(statement).group(1)  # type: ignore[union-attr]
                           for statement in SCHEMA_STATEMENTS)

# Permanent tables: a registry that lacks one of them has lost data.
_TABLES = ("meta", "cores", "stereos", "batches", "events")
_SCHEMA_TABLES = frozenset(_TABLES) | {"names", "spectra", "yields", "reactions"}

NAME_EVENTS = ("name_set", "name_removed")
SPECTRUM_EVENT = "spectrum_recorded"
YIELD_EVENTS = ("yield_set", "yield_removed")
_YIELD_SELECT = "SELECT full_id, isolated, analytical, updated_at FROM yields"
REACTION_EVENT = "reaction_set"
_REACTION_SELECT = ("SELECT experiment_ref, experiment_code, experiment_url, data, updated_at "
                    "FROM reactions")
LIMITING_EVENT = "limiting_set"


def canonical_json(data: Any) -> str:
    """The stored text of a JSON document: keys sorted, no spaces, non-ASCII
    kept as is. Equal documents give equal texts."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


# Columns of the annotation tables that a registry file may lack, as
# (table, column, statement that adds it). A name stored without a source was
# typed by the user.
_ANNOTATION_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("names", "source", "ALTER TABLE names ADD COLUMN source TEXT NOT NULL DEFAULT 'user'"),
    ("reactions", "limiting_row", "ALTER TABLE reactions ADD COLUMN limiting_row INTEGER"),
    ("reactions", "limiting_name", "ALTER TABLE reactions ADD COLUMN limiting_name TEXT"),
)

#: Schema upgrades: ``_MIGRATIONS[n]`` turns a schema ``n - 1`` database into
#: schema ``n``. A step gets the connection inside the open's write
#: transaction and only changes tables, indexes and triggers (a step that
#: changes a guard drops and re-creates that trigger itself);
#: ``schema_version`` and the ``migrate`` event are written by the caller.
#: Every version from 2 to :data:`SCHEMA_VERSION` needs a step.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}

_REGISTRY_ID_NAMESPACE = uuid.UUID("6f0f2f0e-5d0c-4d0b-9b57-0c6f3f1f8a11")


def new_registry_id() -> str:
    """A random identity for a new registry (32 hex digits)."""
    return uuid.uuid4().hex


def derived_registry_id(created_at: str, id_format: str) -> str:
    """The identity of a registry whose file does not store one: derived from
    its creation time and ID format, which never change, so every copy and
    every older backup of that registry gives the same value."""
    return uuid.uuid5(_REGISTRY_ID_NAMESPACE, f"{created_at}\n{id_format}").hex


def registry_identity(meta: dict[str, str]) -> str:
    """The identity of a registry from its meta values: the stored
    ``registry_id``, else :func:`derived_registry_id`."""
    stored = meta.get(REGISTRY_ID_KEY)
    if stored:
        return stored
    return derived_registry_id(meta.get("created_at", ""), meta.get("id_format", ""))


def read_registry_identity(conn: sqlite3.Connection) -> str | None:
    """:func:`registry_identity` of an open database, or None when it is not
    a registry (no ``meta`` table or no ``id_format``). Raises ``sqlite3.Error``."""
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
    if not has_meta:
        return None
    meta = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}
    if "id_format" not in meta:
        return None
    return registry_identity(meta)


def unstamped_journal_own(meta: dict[str, str]) -> bool:
    """Whether journal lines that name no registry are this registry's own:
    the database says so (:data:`UNSTAMPED_JOURNAL_KEY`), or it says nothing
    and its identity is the derived one, i.e. the file was made before
    ``registry_id`` existed and so was its journal."""
    state = meta.get(UNSTAMPED_JOURNAL_KEY)
    if state is not None:
        return state == UNSTAMPED_OWN
    return registry_identity(meta) == derived_registry_id(
        meta.get("created_at", ""), meta.get("id_format", ""))


_NAME_SELECT = "SELECT core_stereo_id, name, updated_at, source FROM names"

_BATCH_SELECT = """
SELECT b.full_id, b.core_no, b.stereo_no, b.batch_no, s.core_stereo_id,
       b.experiment_ref, b.experiment_code, b.experiment_url, b.product_index,
       b.molfile, b.smiles, b.inchikey, s.stereo_mode, b.source, b.created_at,
       b.rdkit_version, b.inchi_version, b.retired_at, b.retired_reason
FROM batches AS b
JOIN stereos AS s ON s.core_no = b.core_no AND s.stereo_no = b.stereo_no
"""

_STEREO_SELECT = """
SELECT s.core_no, s.stereo_no, s.core_stereo_id, s.stereo_key, s.stereo_mode,
       s.inchikey, s.smiles, s.molfile, s.created_at,
       (SELECT count(*) FROM batches AS b
        WHERE b.core_no = s.core_no AND b.stereo_no = s.stereo_no) AS batch_count
FROM stereos AS s
"""

_CORE_SELECT = "SELECT core_no, core_key, inchikey, formula, created_at FROM cores"


def _invalid(message: str) -> RegistryError:
    return RegistryError(message, code="invalid_argument")


def _clean_ref(ref: object) -> object:
    """Strip surrounding whitespace from an experiment reference, so that
    ``"4180"`` and ``"4180 "`` are the same experiment. Non-strings are
    returned unchanged (validation rejects them)."""
    return ref.strip() if isinstance(ref, str) else ref


def _check_timestamp(name: str, text: object) -> str:
    """``text`` if it is a timestamp in the registry's exact format, else
    ``RegistryError(code="invalid_argument")``."""
    if not isinstance(text, str):
        raise _invalid(f"{name} must be a timestamp string, got {text!r}")
    try:
        ok = format_timestamp(parse_timestamp(text)) == text
    except ValueError:
        ok = False
    if not ok:
        raise _invalid(f"{name} must look like 2026-11-17T15:04:05Z, got {text!r}")
    return text


@dataclass
class _SchemaState:
    """What a registry file holds, read inside one transaction of the open."""

    objects: set[str]                 # names of its tables, indexes and triggers
    tables: set[str]
    meta: dict[str, str]
    version: int | None               # meta.schema_version; None for a new, empty file
    user_version: int
    missing_columns: list[tuple[str, str, str]]   # entries of _ANNOTATION_COLUMNS

    @property
    def exists(self) -> bool:
        return bool(self.tables)

    @property
    def needs_upgrade(self) -> bool:
        return self.version is not None and self.version < SCHEMA_VERSION

    @property
    def readable_as_is(self) -> bool:
        """True when every read works without changing the file: only
        indexes, guards, ``registry_id`` or ``user_version`` may be missing."""
        return (self.exists and not self.needs_upgrade and not self.missing_columns
                and all(name in self.tables for name in SCHEMA_OBJECTS
                        if name in _SCHEMA_TABLES))

    @property
    def complete(self) -> bool:
        return (self.readable_as_is and SCHEMA_OBJECTS <= self.objects
                and bool(self.meta.get(REGISTRY_ID_KEY))
                and self.user_version == SCHEMA_VERSION)


@dataclass(frozen=True)
class ReplayRegister:
    """One registration to re-create with :meth:`SqliteRepository.replay`."""

    norm: Normalized
    experiment: ExperimentRef
    product_index: int
    source: str
    created_at: str
    expect_full_id: str


@dataclass(frozen=True)
class ReplayRetire:
    """One retirement to re-create with :meth:`SqliteRepository.replay`."""

    full_id: str
    reason: str
    retired_at: str


class SqliteRepository:
    """The registry stored in one SQLite file.

    ``id_format``: the format the caller expects. For a new database it is
    stored (default :attr:`IdFormat.DEFAULT` if ``None``). For an existing
    database a different template raises ``FormatMismatchError``; ``None``
    adopts the stored format.

    ``clock``: returns the current time (aware datetime); injectable for tests.

    ``upgrade_backup_dir``: where the copy made before a schema upgrade goes
    (default: the folder of the database).

    ``new_registry_id``: the identity a NEW database gets instead of a random
    one (a database re-created for a registry whose journal survived).
    ``unstamped_journal_own``: a NEW database reads the journal lines that
    name no registry as its own (the journal of its home folder has such
    lines). Both are ignored for an existing database.

    After opening, :attr:`read_only` tells whether the file was found to be
    read-only (reading works, every write is refused), :attr:`upgrade` what a schema upgrade did
    (``{"from", "to", "backup"}``) or None, and :attr:`identity_added`
    whether this open gave an existing file its ``registry_id``.
    """

    def __init__(
        self,
        db_path: Path,
        id_format: IdFormat | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        upgrade_backup_dir: Path | None = None,
        new_registry_id: str | None = None,
        unstamped_journal_own: bool = False,
    ):
        self._path = Path(os.path.abspath(Path(db_path).expanduser()))
        self._clock: Callable[[], datetime] = clock or _utc_now
        self._conn: sqlite3.Connection | None = None
        self._upgrade_backup_dir = (Path(os.path.abspath(Path(upgrade_backup_dir).expanduser()))
                                    if upgrade_backup_dir is not None else self._path.parent)
        self._registry_id = ""
        self._new_registry_id = new_registry_id
        self._new_unstamped_own = bool(unstamped_journal_own)
        self._unstamped_own = False
        self._schema_version = SCHEMA_VERSION
        self.read_only = False
        self.identity_added = False
        self.upgrade: dict[str, Any] | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Cannot create the folder for {self._path}: {exc}") from None
        try:
            conn = sqlite3.connect(
                str(self._path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Cannot open the registry database {self._path}: {exc}") from None
        self._conn = conn
        try:
            conn.row_factory = sqlite3.Row
            self._configure(conn)
            self._fmt = self._init_schema(conn, id_format)
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------------ setup
    def _configure(self, conn: sqlite3.Connection) -> None:
        with self._guard("configure"):
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            # Asking is a read; setting the mode writes to the file, so it is
            # only done when the file is in another mode.
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "delete":
                mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                raise StorageError(f"Could not set journal_mode=DELETE on {self._path} (got {mode})")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA recursive_triggers = ON")

    def _init_schema(self, conn: sqlite3.Connection, wanted: IdFormat | None) -> IdFormat:
        with self._guard("initialise"):
            # 1. Read only. A complete registry is opened without any write.
            with self._transaction(immediate=False) as c:
                state = self._inspect(c)
                if state.complete:
                    self.read_only = not os.access(self._path, os.W_OK)
                    return self._adopt(c, state, wanted)
            # 2. Something has to be created or brought up to date.
            backup = None
            if state.needs_upgrade:
                self._check_upgrade_steps(state.version)  # type: ignore[arg-type]
                if not os.access(self._path, os.W_OK):
                    raise self._not_writable("the file is read-only")
                backup = self._backup_before_upgrade(state.version)  # type: ignore[arg-type]
            try:
                with self._transaction() as c:
                    return self._bring_up_to_date(c, wanted, backup)
            except sqlite3.OperationalError as exc:
                self.upgrade = None
                if "readonly" not in str(exc).lower().replace("-", "").replace(" ", ""):
                    raise
                if not state.readable_as_is:
                    raise self._not_writable(str(exc)) from None
            except BaseException:
                self.upgrade = None
                raise
            finally:
                if backup is not None and self.upgrade is None:
                    # Nothing was upgraded, so the copy is of no use; one
                    # more would be left behind by every further attempt.
                    try:
                        os.remove(backup)
                    except OSError:
                        pass
            # 3. A file that cannot be written but can be read as it is.
            with self._transaction(immediate=False) as c:
                state = self._inspect(c)
                if not state.readable_as_is:
                    raise StorageError(
                        f"The registry database {self._path} cannot be written and cannot be "
                        "read as it is.")
                self.read_only = True
                return self._adopt(c, state, wanted)

    def _not_writable(self, reason: str) -> StorageError:
        return StorageError(
            f"The registry database {self._path} must be brought up to date by "
            f"this program version, but the file cannot be written ({reason}). Make "
            "the file and its folder writable, or open a copy of it.")

    def _inspect(self, c: sqlite3.Connection) -> _SchemaState:
        """Read what the file holds. Refuses (``StorageError``) a database
        that is not a registry, a damaged registry and a registry written by
        a newer program; changes nothing."""
        rows = c.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        objects = {row[1] for row in rows}
        tables = {row[1] for row in rows if row[0] == "table"}
        user_version = int(c.execute("PRAGMA user_version").fetchone()[0])
        if not tables:
            return _SchemaState(objects, tables, {}, None, user_version, [])
        if "meta" not in tables:
            raise StorageError(
                f"{self._path} is an SQLite database but not a compound registry "
                f"(tables: {', '.join(sorted(tables))}); refusing to modify it."
            )
        # An existing registry: a missing permanent table means data was lost.
        # Re-creating it empty would silently reuse numbers, so refuse.
        missing = [t for t in _TABLES if t not in tables]
        if missing:
            raise StorageError(
                f"The registry database {self._path} is damaged: table(s) "
                f"{', '.join(missing)} are missing. Re-creating them would reuse ID "
                f"numbers that were already issued, so the registry was not opened and "
                f"nothing was changed. Restore the latest backup (see 'compound-registry "
                f"backup --list'), then run 'compound-registry recover'.",
                code="registry_damaged",
            )
        meta = {row[0]: row[1] for row in c.execute("SELECT key, value FROM meta")}
        if "id_format" not in meta or "schema_version" not in meta:
            raise StorageError(
                f"{self._path} has no id_format/schema_version in its meta table; "
                "it is not a usable registry."
            )
        try:
            version = int(meta["schema_version"])
        except ValueError:
            raise StorageError(f"{self._path}: invalid schema_version {meta['schema_version']!r}") from None
        if version > SCHEMA_VERSION:
            raise StorageError(
                f"{self._path} was written by a newer program version "
                f"(schema {version}, this program understands {SCHEMA_VERSION}). Update the program."
            )
        missing_columns = []
        for table in sorted({entry[0] for entry in _ANNOTATION_COLUMNS} & tables):
            columns = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
            missing_columns.extend(entry for entry in _ANNOTATION_COLUMNS
                                   if entry[0] == table and entry[1] not in columns)
        return _SchemaState(objects, tables, meta, version, user_version, missing_columns)

    def _stored_format(self, state: _SchemaState, wanted: IdFormat | None) -> IdFormat:
        stored = state.meta["id_format"]
        if wanted is not None and wanted.template != stored:
            raise FormatMismatchError(
                f"The registry database {self._path} was created with ID format "
                f"{stored!r}, but the configuration asks for {wanted.template!r}. "
                f"IDs already issued must stay in their format: set [id] format "
                f"back to {stored!r} in config.toml."
            )
        return wanted or IdFormat(stored)

    def _adopt(self, c: sqlite3.Connection, state: _SchemaState,
               wanted: IdFormat | None) -> IdFormat:
        """Take an existing registry as it is (no write)."""
        fmt = self._stored_format(state, wanted)
        self._check_consistency(c)
        self._registry_id = registry_identity(state.meta)
        self._unstamped_own = unstamped_journal_own(state.meta)
        self._schema_version = state.version  # type: ignore[assignment]
        return fmt

    def _bring_up_to_date(self, c: sqlite3.Connection, wanted: IdFormat | None,
                          upgrade_backup: Path | None) -> IdFormat:
        """Inside the open's write transaction: create a new registry, or
        upgrade the schema of an existing one and add what it lacks. The file
        is read again first: another program may have done it meanwhile."""
        state = self._inspect(c)
        now = self._timestamp()
        if state.exists:
            if state.needs_upgrade:
                self._upgrade_schema(c, state.version, upgrade_backup, now)  # type: ignore[arg-type]
            fmt = self._stored_format(state, wanted)
        else:
            fmt = wanted or IdFormat()
            # The format is permanent from here on, so it must be one whose
            # IDs can also be folder and file names.
            fmt.require_file_safe()
        # Idempotent: a new schema, or the indexes, guards and annotation
        # tables an existing registry lacks (its permanent tables were checked).
        for statement in SCHEMA_STATEMENTS:
            c.execute(statement)
        for table, column, statement in _ANNOTATION_COLUMNS:
            if column not in {row[1] for row in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(statement)
        if state.exists:
            self._check_consistency(c)
            registry_id = state.meta.get(REGISTRY_ID_KEY) or ""
            if not registry_id:
                registry_id = registry_identity(state.meta)
                c.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
                          (REGISTRY_ID_KEY, registry_id))
                self._insert_event(c, now, "registry_id_set", None, {"registry_id": registry_id})
                self.identity_added = True
            unstamped_own = unstamped_journal_own({**state.meta, REGISTRY_ID_KEY: registry_id})
        else:
            registry_id = self._new_registry_id or new_registry_id()
            unstamped_own = self._new_unstamped_own
            c.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("id_format", fmt.template),
                    ("created_at", now),
                    (REGISTRY_ID_KEY, registry_id),
                ] + ([(UNSTAMPED_JOURNAL_KEY, UNSTAMPED_OWN)] if unstamped_own else []),
            )
            self._insert_event(c, now, "create", None, {
                "id_format": fmt.template, "schema_version": SCHEMA_VERSION,
                "registry_id": registry_id, "program_version": __version__})
        if state.user_version != SCHEMA_VERSION or state.needs_upgrade:
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._registry_id = registry_id
        self._unstamped_own = unstamped_own
        self._schema_version = SCHEMA_VERSION
        return fmt

    # ------------------------------------------------------------------ schema upgrade
    def _check_upgrade_steps(self, version: int) -> None:
        missing = [v for v in range(version + 1, SCHEMA_VERSION + 1) if v not in _MIGRATIONS]
        if missing:
            raise StorageError(
                f"{self._path} has schema version {version}, which this program version "
                f"(schema {SCHEMA_VERSION}) cannot bring up to date. Nothing was changed."
            )

    def _backup_before_upgrade(self, version: int) -> Path:
        """Copy the database (still at schema ``version``) before its upgrade;
        returns the copy. Without a verified copy nothing is upgraded."""
        name = (f"{self._path.stem}-before-schema-{SCHEMA_VERSION}-"
                f"{self._clock().astimezone(timezone.utc):%Y%m%d-%H%M%S}.sqlite")
        final = self._upgrade_backup_dir / name
        number = 1
        while final.exists():
            number += 1
            final = final.with_name(f"{name[:-len('.sqlite')]}-{number}.sqlite")
        partial = final.with_name(f".{final.name}.partial")
        try:
            self.backup_to(partial)
            with sqlite3.connect(str(partial)) as check:
                rows = [str(r[0]) for r in check.execute("PRAGMA integrity_check").fetchall()]
            check.close()
            if rows != ["ok"]:
                raise StorageError("the copy is damaged: " + "; ".join(rows[:3]))
            os.replace(partial, final)
        except (RegistryError, sqlite3.Error, OSError) as exc:
            try:
                os.remove(partial)
            except OSError:
                pass
            reason = getattr(exc, "message", None) or str(exc)
            raise StorageError(
                f"The registry database {self._path} (schema {version}) must be upgraded to "
                f"schema {SCHEMA_VERSION}, but the copy that is made first could not be "
                f"written to {final.parent} ({reason}). Nothing was changed."
            ) from None
        return final

    def _upgrade_schema(self, c: sqlite3.Connection, version: int, backup: Path | None,
                        now: str) -> None:
        self._check_upgrade_steps(version)
        for target in range(version + 1, SCHEMA_VERSION + 1):
            try:
                _MIGRATIONS[target](c)
            except Exception as exc:  # noqa: BLE001 - whatever a step raises, the open is refused
                reason = getattr(exc, "message", None) or str(exc) or type(exc).__name__
                raise StorageError(
                    f"The registry database {self._path} (schema {version}) could not be "
                    f"upgraded to schema {target} ({reason}). Nothing was changed."
                ) from None
        self._set_schema_version(c, SCHEMA_VERSION)
        details = {"from": version, "to": SCHEMA_VERSION, "program_version": __version__,
                   "backup": str(backup) if backup is not None else None}
        self._insert_event(c, now, "migrate", None, details)
        self.upgrade = {"from": version, "to": SCHEMA_VERSION,
                        "backup": str(backup) if backup is not None else None}

    @staticmethod
    def _set_schema_version(c: sqlite3.Connection, version: int) -> None:
        """The only place that changes ``meta.schema_version``: its guards
        are dropped around the change and created again."""
        for name in _META_GUARD_TRIGGERS:
            c.execute(f"DROP TRIGGER IF EXISTS {name}")
        c.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(version),))
        for statement in _META_GUARD_STATEMENTS:
            c.execute(statement)

    def _check_consistency(self, c: sqlite3.Connection) -> None:
        """Cheap structural check of the numbering, run on every open.

        Because rows are never deleted, numbers must be gap-free:
        ``max(core_no) == count(cores)``, per core ``max(stereo_no) ==
        count(stereos)``, per stereo variant ``max(batch_no) == count(batches)``,
        and every batch has exactly one ``register`` event. A failure means
        rows were lost (e.g. a damaged file or a manual edit); continuing could
        re-issue numbers, so ``StorageError(code="registry_damaged")`` is raised.
        """
        problems: list[str] = []
        max_core, n_cores = c.execute(
            "SELECT coalesce(max(core_no), 0), count(*) FROM cores").fetchone()
        if max_core != n_cores:
            problems.append(f"the highest CORE number is {max_core} but there are {n_cores} cores")
        rows = c.execute(
            "SELECT core_no, max(stereo_no), count(*) FROM stereos GROUP BY core_no "
            "HAVING max(stereo_no) != count(*) ORDER BY core_no LIMIT 5").fetchall()
        for core_no, highest, count in rows:
            problems.append(f"core {core_no}: highest STEREO number {highest} but {count} stereo variant(s)")
        rows = c.execute(
            "SELECT core_no, stereo_no, max(batch_no), count(*) FROM batches "
            "GROUP BY core_no, stereo_no HAVING max(batch_no) != count(*) "
            "ORDER BY core_no, stereo_no LIMIT 5").fetchall()
        for core_no, stereo_no, highest, count in rows:
            problems.append(f"core {core_no} stereo {stereo_no}: highest BATCH number {highest} "
                            f"but {count} batch(es)")
        n_batches = c.execute("SELECT count(*) FROM batches").fetchone()[0]
        n_events = c.execute("SELECT count(*) FROM events WHERE event = 'register'").fetchone()[0]
        if n_events != n_batches:
            problems.append(f"{n_batches} batch(es) but {n_events} 'register' event(s)")
        if problems:
            raise StorageError(
                f"The registry database {self._path} is inconsistent: {'; '.join(problems)}. "
                "Records appear to be missing, and continuing could issue an ID a second time, "
                "so the registry was not opened. Restore the latest backup (see "
                "'compound-registry backup --list'), then run 'compound-registry recover'.",
                code="registry_damaged",
            )

    # ------------------------------------------------------------------ plumbing
    @property
    def path(self) -> Path:
        """Absolute path of the database file."""
        return self._path

    @property
    def id_format(self) -> IdFormat:
        """The ID format of this registry."""
        return self._fmt

    @property
    def registry_id(self) -> str:
        """The identity of this registry (see :func:`registry_identity`)."""
        return self._registry_id

    @property
    def schema_version(self) -> int:
        """The schema version of the open file."""
        return self._schema_version

    @property
    def unstamped_journal_own(self) -> bool:
        """Whether lines of the issued-ID journal that name no registry are
        this registry's own (see :func:`unstamped_journal_own`). False for a
        registry created in a folder without such lines, and again after
        :meth:`journal_stamped`."""
        return self._unstamped_own

    def journal_stamped(self) -> None:
        """Record that the journal now holds a copy with ``registry_id`` of
        every line of this registry: from here on a line that names no
        registry and has no such copy is another registry's."""
        self.set_meta(UNSTAMPED_JOURNAL_KEY, UNSTAMPED_STAMPED)
        self._unstamped_own = False

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError(f"The registry {self._path} is closed")
        return self._conn

    @contextmanager
    def _guard(self, action: str) -> Iterator[None]:
        """Translate ``sqlite3.Error`` into ``StorageError``."""
        try:
            yield
        except sqlite3.OperationalError as exc:
            text = str(exc)
            if "locked" in text or "busy" in text:
                raise StorageError(
                    f"The registry database {self._path} is busy (another program is writing "
                    f"to it). Try again in a moment. [{action}: {text}]"
                ) from None
            if "readonly" in text:
                raise StorageError(
                    f"The registry database {self._path} cannot be written: the file or its "
                    f"folder is read-only. Nothing was changed. [{action}: {text}]"
                ) from None
            raise StorageError(f"Database error while trying to {action} ({self._path}): {text}") from None
        except sqlite3.Error as exc:
            raise StorageError(f"Database error while trying to {action} ({self._path}): {exc}") from None

    @contextmanager
    def _transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` (write) or ``BEGIN`` (consistent read) ... ``COMMIT``;
        ``ROLLBACK`` on any exception."""
        conn = self._require()
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

    def _timestamp(self) -> str:
        return format_timestamp(self._clock())

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection, at: str, event: str, full_id: str | None, details: dict[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO events(at, event, full_id, details) VALUES (?, ?, ?, ?)",
            (at, event, full_id, json.dumps(details, sort_keys=True, ensure_ascii=False)),
        )

    # ------------------------------------------------------------------ row helpers
    @staticmethod
    def _one_batch(conn: sqlite3.Connection, where: str, params: tuple) -> BatchRecord | None:
        row = conn.execute(f"{_BATCH_SELECT} {where} ORDER BY b.batch_id LIMIT 1", params).fetchone()
        return BatchRecord(**dict(row)) if row is not None else None

    @staticmethod
    def _batches(conn: sqlite3.Connection, where: str, params: tuple, order: str) -> list[BatchRecord]:
        rows = conn.execute(f"{_BATCH_SELECT} {where} ORDER BY {order}", params).fetchall()
        return [BatchRecord(**dict(r)) for r in rows]

    @staticmethod
    def _core(conn: sqlite3.Connection, core_key: str) -> CoreRecord | None:
        row = conn.execute(f"{_CORE_SELECT} WHERE core_key = ?", (core_key,)).fetchone()
        return CoreRecord(**dict(row)) if row is not None else None

    @staticmethod
    def _stereo(conn: sqlite3.Connection, core_no: int, stereo_key: str, mode: str) -> StereoRecord | None:
        row = conn.execute(
            f"{_STEREO_SELECT} WHERE s.core_no = ? AND s.stereo_key = ? AND s.stereo_mode = ?",
            (core_no, stereo_key, mode),
        ).fetchone()
        return StereoRecord(**dict(row)) if row is not None else None

    # ------------------------------------------------------------------ validation
    @staticmethod
    def _validate(norm: Normalized, experiment: ExperimentRef, product_index: int, source: str) -> None:
        if not isinstance(norm.core_key, str) or not norm.core_key.strip():
            raise _invalid("The structure has no core key")
        if not isinstance(norm.stereo_key, str):
            raise _invalid("The structure has no stereo key")
        if norm.stereo_mode not in STEREO_MODES:
            raise ChemistryError(
                f"Unknown stereo mode {norm.stereo_mode!r} (allowed: {', '.join(STEREO_MODES)})",
                code="invalid_mode",
            )
        if not isinstance(experiment.ref, str) or not experiment.ref.strip():
            raise _invalid("An experiment reference is required")
        if isinstance(product_index, bool) or not isinstance(product_index, int) or product_index < 1:
            raise _invalid(f"product_index must be an integer >= 1, got {product_index!r}")
        if source not in SOURCES:
            raise _invalid(f"source must be one of {', '.join(SOURCES)}, got {source!r}")

    def _next_numbers(
        self, conn: sqlite3.Connection, core: CoreRecord | None, stereo: StereoRecord | None
    ) -> tuple[int, int, int]:
        if core is None:
            core_no = conn.execute("SELECT coalesce(max(core_no), 0) + 1 FROM cores").fetchone()[0]
            stereo_no, batch_no = 1, 1
        elif stereo is None:
            core_no = core.core_no
            stereo_no = conn.execute(
                "SELECT coalesce(max(stereo_no), 0) + 1 FROM stereos WHERE core_no = ?", (core_no,)
            ).fetchone()[0]
            batch_no = 1
        else:
            core_no, stereo_no = stereo.core_no, stereo.stereo_no
            batch_no = conn.execute(
                "SELECT coalesce(max(batch_no), 0) + 1 FROM batches WHERE core_no = ? AND stereo_no = ?",
                (core_no, stereo_no),
            ).fetchone()[0]
        fmt = self._fmt
        for label, value, maximum in (
            ("CORE", core_no, fmt.max_core),
            ("STEREO", stereo_no, fmt.max_stereo),
            ("BATCH", batch_no, fmt.max_batch),
        ):
            if value > maximum:
                raise IdOverflowError(
                    f"No {label} number left: {value} does not fit the ID format "
                    f"{fmt.template!r} (maximum {maximum}). Nothing was registered."
                )
        return core_no, stereo_no, batch_no

    # ------------------------------------------------------------------ protocol
    def lookup(
        self,
        norm: Normalized,
        experiment_ref: str | None = None,
        product_index: int | None = None,
    ) -> LookupResult:
        """Read-only view of what is registered for ``norm``.

        ``slot_conflict`` mirrors :meth:`register`: it is only reported when
        ``existing_batch`` is None (an existing batch of this compound in the
        experiment wins over the slot check). ``experiment_ref`` is stripped of
        surrounding whitespace, as in :meth:`register`.
        """
        experiment_ref = _clean_ref(experiment_ref)  # type: ignore[assignment]
        with self._guard("look up a structure"), self._transaction(immediate=False) as c:
            core = self._core(c, norm.core_key)
            stereo = self._stereo(c, core.core_no, norm.stereo_key, norm.stereo_mode) if core else None
            batches: list[BatchRecord] = []
            others: list[StereoRecord] = []
            if core is not None:
                rows = c.execute(f"{_STEREO_SELECT} WHERE s.core_no = ? ORDER BY s.stereo_no",
                                 (core.core_no,)).fetchall()
                others = [
                    StereoRecord(**dict(r)) for r in rows
                    if stereo is None or r["stereo_no"] != stereo.stereo_no
                ]
            if stereo is not None:
                batches = self._batches(c, "WHERE b.core_no = ? AND b.stereo_no = ?",
                                        (stereo.core_no, stereo.stereo_no), "b.batch_no")
            existing = None
            if experiment_ref is not None and stereo is not None:
                existing = self._one_batch(
                    c,
                    "WHERE b.experiment_ref = ? AND b.core_no = ? AND b.stereo_no = ? AND b.retired_at IS NULL",
                    (experiment_ref, stereo.core_no, stereo.stereo_no),
                )
            conflict = None
            if experiment_ref is not None and product_index is not None and existing is None:
                conflict = self._one_batch(
                    c, "WHERE b.experiment_ref = ? AND b.product_index = ? AND b.retired_at IS NULL",
                    (experiment_ref, product_index),
                )
        return LookupResult(
            core=core,
            stereo=stereo,
            batches=tuple(batches),
            other_stereos=tuple(others),
            existing_batch=existing,
            slot_conflict=conflict,
        )

    def register(
        self,
        norm: Normalized,
        experiment: ExperimentRef,
        product_index: int,
        source: str = "cli",
        *,
        created_at: str | None = None,
        expect_full_id: str | None = None,
    ) -> RegistrationResult:
        """See :meth:`RegistryRepository.register`. One ``BEGIN IMMEDIATE`` transaction.

        ``experiment.ref`` is stripped of surrounding whitespace and stored
        stripped. SQLite-only keyword arguments (used to recover registrations
        from the issued-ID journal):

        ``created_at``      store this timestamp instead of the current time.
        ``expect_full_id``  the ID this registration must receive. If the
                            allocated (or returned) ID differs, the transaction
                            is rolled back and ``RegistryError(code="id_mismatch")``
                            is raised, so nothing is committed.
        """
        experiment = dataclasses.replace(experiment, ref=_clean_ref(experiment.ref))
        self._validate(norm, experiment, product_index, source)
        if created_at is not None:
            _check_timestamp("created_at", created_at)
        with self._guard("register a compound"), self._transaction() as c:
            return self._register_in(c, norm, experiment, product_index, source,
                                     created_at=created_at, expect_full_id=expect_full_id)

    def _register_in(
        self,
        c: sqlite3.Connection,
        norm: Normalized,
        experiment: ExperimentRef,
        product_index: int,
        source: str,
        *,
        created_at: str | None = None,
        expect_full_id: str | None = None,
        event_details: dict[str, Any] | None = None,
    ) -> RegistrationResult:
        """The body of :meth:`register`, inside an open write transaction.
        Arguments must already be validated (and the reference stripped)."""
        now = created_at if created_at is not None else self._timestamp()
        core = self._core(c, norm.core_key)
        stereo = self._stereo(c, core.core_no, norm.stereo_key, norm.stereo_mode) if core else None

        # 1. Same compound already active in this experiment -> return it.
        if stereo is not None:
            existing = self._one_batch(
                c,
                "WHERE b.experiment_ref = ? AND b.core_no = ? AND b.stereo_no = ? AND b.retired_at IS NULL",
                (experiment.ref, stereo.core_no, stereo.stereo_no),
            )
            if existing is not None:
                self._check_expected(expect_full_id, existing.full_id, "already holds")
                warnings: tuple[str, ...] = ()
                if existing.product_index != product_index:
                    warnings = (
                        f"This compound is already registered in this experiment as product "
                        f"{existing.product_index}; returning that ID.",
                    )
                repeat_at = now if created_at is None else self._timestamp()
                self._insert_event(c, repeat_at, "register_repeat", existing.full_id, {
                    "experiment_ref": experiment.ref,
                    "product_index": product_index,
                    "source": source,
                })
                return RegistrationResult(batch=existing, created=False, warnings=warnings)

        # 2. Slot taken by a different compound -> refuse.
        conflict = self._one_batch(
            c, "WHERE b.experiment_ref = ? AND b.product_index = ? AND b.retired_at IS NULL",
            (experiment.ref, product_index),
        )
        if conflict is not None:
            raise SlotConflictError(
                f"Product {product_index} of experiment {experiment.code or experiment.ref} is "
                f"already registered as {conflict.full_id}, a different compound. Retire "
                f"{conflict.full_id} first if it was wrong.",
                existing=conflict,
            )

        # 3. Allocate and store.
        core_no, stereo_no, batch_no = self._next_numbers(c, core, stereo)
        full_id = self._fmt.format(core_no, stereo_no, batch_no)
        self._check_expected(expect_full_id, full_id, "would allocate")
        core_stereo_id = self._fmt.format_core_stereo(core_no, stereo_no)
        if core is None:
            c.execute(
                "INSERT INTO cores(core_no, core_key, inchikey, formula, created_at) VALUES (?, ?, ?, ?, ?)",
                (core_no, norm.core_key, norm.core_inchikey, norm.formula, now),
            )
        if stereo is None:
            c.execute(
                "INSERT INTO stereos(core_no, stereo_no, core_stereo_id, stereo_key, stereo_mode,"
                " inchikey, smiles, molfile, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (core_no, stereo_no, core_stereo_id, norm.stereo_key, norm.stereo_mode,
                 norm.inchikey, norm.smiles, norm.molblock, now),
            )
        c.execute(
            "INSERT INTO batches(core_no, stereo_no, batch_no, full_id, experiment_ref,"
            " experiment_code, experiment_url, product_index, molfile, smiles, inchikey, inchi,"
            " detected_mode, mode_overridden, input_format, source, created_at, rdkit_version,"
            " inchi_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (core_no, stereo_no, batch_no, full_id, experiment.ref, experiment.code,
             experiment.url, product_index, norm.molblock, norm.smiles, norm.inchikey,
             norm.inchi, norm.detected_mode or norm.stereo_mode, int(bool(norm.mode_overridden)),
             norm.input_format, source, now, norm.rdkit_version, norm.inchi_version),
        )
        details: dict[str, Any] = {
            "experiment_ref": experiment.ref,
            "experiment_code": experiment.code,
            "product_index": product_index,
            "source": source,
            "core_created": core is None,
            "stereo_created": stereo is None,
            "stereo_mode": norm.stereo_mode,
            "detected_mode": norm.detected_mode,
            "mode_overridden": bool(norm.mode_overridden),
            "inchikey": norm.inchikey,
            "chem_warnings": list(norm.warnings),
        }
        details.update(event_details or {})
        self._insert_event(c, now, "register", full_id, details)
        batch = self._one_batch(c, "WHERE b.full_id = ?", (full_id,))
        assert batch is not None
        return RegistrationResult(
            batch=batch, created=True, core_created=core is None, stereo_created=stereo is None
        )

    @staticmethod
    def _check_expected(expected: str | None, actual: str, verb: str) -> None:
        if expected is not None and expected != actual:
            raise RegistryError(
                f"Expected ID {expected}, but the registry {verb} {actual} for this registration. "
                "Nothing was registered.",
                code="id_mismatch",
            )

    def retire(self, full_id: str, reason: str, *, retired_at: str | None = None) -> BatchRecord:
        """Mark a batch retired. Unknown ID -> ``NotFoundError``; already
        retired -> ``RegistryError(code="already_retired")``; empty reason ->
        ``RegistryError(code="invalid_argument")``.

        ``retired_at`` (SQLite-only, for recovery from the journal): store this
        timestamp instead of the current time.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise _invalid("A reason is required to retire a batch")
        reason = reason.strip()
        full_id = full_id.strip() if isinstance(full_id, str) else full_id
        if retired_at is not None:
            _check_timestamp("retired_at", retired_at)
        with self._guard("retire a batch"), self._transaction() as c:
            return self._retire_in(c, full_id, reason, retired_at=retired_at)

    def _retire_in(
        self,
        c: sqlite3.Connection,
        full_id: str,
        reason: str,
        *,
        retired_at: str | None = None,
        event_details: dict[str, Any] | None = None,
    ) -> BatchRecord:
        """The body of :meth:`retire`, inside an open write transaction."""
        batch = self._one_batch(c, "WHERE b.full_id = ?", (full_id,))
        if batch is None:
            raise NotFoundError(f"No batch with ID {full_id!r} exists")
        if batch.retired:
            raise RegistryError(
                f"{full_id} was already retired at {batch.retired_at}: {batch.retired_reason}",
                code="already_retired",
            )
        now = retired_at if retired_at is not None else self._timestamp()
        c.execute(
            "UPDATE batches SET retired_at = ?, retired_reason = ? WHERE full_id = ? AND retired_at IS NULL",
            (now, reason, full_id),
        )
        details: dict[str, Any] = {"reason": reason}
        details.update(event_details or {})
        self._insert_event(c, now, "retire", full_id, details)
        updated = self._one_batch(c, "WHERE b.full_id = ?", (full_id,))
        assert updated is not None
        return updated

    def replay(self, operations: Sequence[ReplayRegister | ReplayRetire]) -> list[str]:
        """Re-create journalled registrations and retirements, in order, in ONE
        transaction (SQLite-only; used by ``RegistryService.recover``).

        Each registration keeps its original ``created_at`` and must receive
        exactly ``expect_full_id``; each retirement keeps its ``retired_at``
        and reason. If any operation fails (an ID that cannot be reproduced, a
        slot conflict, an unknown ID, ...) everything is rolled back and the
        error is raised, so the database is either fully recovered or unchanged.
        Returns the IDs touched, in order.
        """
        prepared: list[ReplayRegister | ReplayRetire] = []
        for op in operations:
            if isinstance(op, ReplayRegister):
                experiment = dataclasses.replace(op.experiment, ref=_clean_ref(op.experiment.ref))
                self._validate(op.norm, experiment, op.product_index, op.source)
                _check_timestamp("created_at", op.created_at)
                prepared.append(dataclasses.replace(op, experiment=experiment))
            elif isinstance(op, ReplayRetire):
                if not isinstance(op.reason, str) or not op.reason.strip():
                    raise _invalid(f"A reason is required to retire {op.full_id}")
                _check_timestamp("retired_at", op.retired_at)
                prepared.append(dataclasses.replace(op, reason=op.reason.strip()))
            else:
                raise _invalid(f"Unknown replay operation {op!r}")
        done: list[str] = []
        with self._guard("recover registrations"), self._transaction() as c:
            recovered_at = self._timestamp()
            for op in prepared:
                op_id = op.expect_full_id if isinstance(op, ReplayRegister) else op.full_id
                try:
                    if isinstance(op, ReplayRegister):
                        result = self._register_in(
                            c, op.norm, op.experiment, op.product_index, op.source,
                            created_at=op.created_at, expect_full_id=op.expect_full_id,
                            event_details={"recovered_at": recovered_at},
                        )
                        if not result.created:
                            raise RegistryError(
                                f"{op.expect_full_id} is already registered; it cannot be "
                                "re-created a second time.", code="id_mismatch")
                        done.append(result.batch.full_id)
                    else:
                        batch = self._retire_in(c, op.full_id, op.reason, retired_at=op.retired_at,
                                                event_details={"recovered_at": recovered_at})
                        done.append(batch.full_id)
                except RegistryError as exc:
                    action = "registration" if isinstance(op, ReplayRegister) else "retirement"
                    error = RegistryError(f"Could not re-create the {action} of {op_id}: "
                                          f"{exc.message}", code=exc.code)
                    error.failed_id = op_id  # type: ignore[attr-defined]
                    raise error from None
        return done

    def registration_details(self, full_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Everything needed to re-create each batch without chemistry
        (SQLite-only; used for the issued-ID journal), oldest first.

        Each dict has the batch fields (``id``, ``at``, ``experiment_*``,
        ``product_index``, ``source``, ``retired_at``, ``retired_reason``) and the
        :class:`Normalized` content as it was stored (``core_key``,
        ``stereo_key``, ``stereo_mode``, ``inchikey``, ``core_inchikey``,
        ``smiles``, ``inchi``, ``formula``, ``detected_mode``,
        ``mode_overridden``, ``molblock``, ``input_format``, ``rdkit_version``,
        ``inchi_version``), plus ``register_seq`` / ``retire_seq``: the event-log
        positions of the registration and retirement (to order journal entries).
        ``full_ids`` limits the result to those IDs.
        """
        sql = """
SELECT b.full_id AS id, b.created_at AS at, b.experiment_ref, b.experiment_code,
       b.experiment_url, b.product_index, b.source, c.core_key, s.stereo_key, s.stereo_mode,
       b.inchikey, c.inchikey AS core_inchikey, b.smiles, b.inchi, c.formula, b.detected_mode,
       b.mode_overridden, b.molfile AS molblock, b.input_format, b.rdkit_version,
       b.inchi_version, b.retired_at, b.retired_reason,
       (SELECT min(e.event_id) FROM events AS e
        WHERE e.full_id = b.full_id AND e.event = 'register') AS register_seq,
       (SELECT min(e.event_id) FROM events AS e
        WHERE e.full_id = b.full_id AND e.event = 'retire') AS retire_seq
FROM batches AS b
JOIN stereos AS s ON s.core_no = b.core_no AND s.stereo_no = b.stereo_no
JOIN cores AS c ON c.core_no = b.core_no
ORDER BY b.batch_id"""
        wanted = None if full_ids is None else set(full_ids)
        with self._guard("read registrations"), self._transaction(immediate=False) as c:
            rows = c.execute(sql).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            if wanted is not None and d["id"] not in wanted:
                continue
            d["mode_overridden"] = bool(d["mode_overridden"])
            out.append(d)
        return out

    def get_batch(self, full_id: str) -> BatchRecord | None:
        with self._guard("read a batch"):
            return self._one_batch(self._require(), "WHERE b.full_id = ?", (full_id,))

    def find_batches(self, experiment_ref: str) -> tuple[BatchRecord, ...]:
        """All batches of one experiment (retired included), by product index
        then age. ``experiment_ref`` is stripped of surrounding whitespace."""
        experiment_ref = _clean_ref(experiment_ref)  # type: ignore[assignment]
        with self._guard("read batches"):
            return tuple(self._batches(self._require(), "WHERE b.experiment_ref = ?",
                                       (experiment_ref,), "b.product_index, b.batch_id"))

    def iter_batches(self, include_retired: bool = True) -> Iterator[BatchRecord]:
        """Every batch ordered by (core_no, stereo_no, batch_no).

        Rows are read in one go before yielding, so a slow consumer never holds
        a read lock that would block other writers.
        """
        where = "" if include_retired else "WHERE b.retired_at IS NULL"
        with self._guard("read batches"):
            rows = self._batches(self._require(), where, (), "b.core_no, b.stereo_no, b.batch_no")
        yield from rows

    def backup_to(self, destination: Path) -> None:
        """Consistent copy of the whole database via the SQLite online backup API."""
        dest = Path(os.path.abspath(Path(destination).expanduser()))
        if _same_file(dest, self._path):
            raise StorageError(f"Refusing to back up {self._path} onto itself")
        conn = self._require()
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Cannot create the backup folder {dest.parent}: {exc}") from None
        with self._guard(f"back up to {dest}"):
            target = sqlite3.connect(str(dest), isolation_level=None)
            try:
                conn.backup(target)
            finally:
                target.close()

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "SqliteRepository":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ extras
    def get_meta(self, key: str) -> str | None:
        with self._guard("read meta"):
            row = self._require().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        """Set a mutable meta value (e.g. ``last_backup_at``). The fixed keys
        ``schema_version``, ``id_format``, ``created_at`` and ``registry_id``
        cannot be changed."""
        if key in FIXED_META_KEYS:
            raise _invalid(f"meta key {key!r} is fixed and cannot be changed")
        if not isinstance(value, str):
            raise _invalid(f"meta values must be strings, got {value!r}")
        with self._guard("write meta"), self._transaction() as c:
            c.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def record_event(
        self,
        event: str,
        full_id: str | None = None,
        details: dict[str, Any] | None = None,
        *,
        meta: dict[str, str] | None = None,
    ) -> None:
        """Append an event (and optionally set mutable meta values) in one transaction."""
        for key in meta or {}:
            if key in FIXED_META_KEYS:
                raise _invalid(f"meta key {key!r} is fixed and cannot be changed")
        with self._guard("write the event log"), self._transaction() as c:
            now = self._timestamp()
            self._insert_event(c, now, event, full_id, details or {})
            for key, value in (meta or {}).items():
                c.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def stats(self) -> dict[str, int]:
        """Counts: ``cores``, ``stereos``, ``batches`` (retired included), ``retired``."""
        with self._guard("count records"), self._transaction(immediate=False) as c:
            return {
                "cores": c.execute("SELECT count(*) FROM cores").fetchone()[0],
                "stereos": c.execute("SELECT count(*) FROM stereos").fetchone()[0],
                "batches": c.execute("SELECT count(*) FROM batches").fetchone()[0],
                "retired": c.execute(
                    "SELECT count(*) FROM batches WHERE retired_at IS NOT NULL").fetchone()[0],
            }

    def integrity_check(self) -> str:
        """``"ok"``, or the problems reported by ``PRAGMA integrity_check`` /
        ``PRAGMA foreign_key_check``."""
        with self._guard("check integrity"):
            conn = self._require()
            rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
            if rows != ["ok"]:
                return "\n".join(rows)
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                return "\n".join(
                    f"foreign key violation in {r[0]} rowid {r[1]} -> {r[2]}" for r in fk
                )
            return "ok"

    # ------------------------------------------------------------------ listings (read-only)
    def list_cores(self) -> list[CoreRecord]:
        """Every CORE, by CORE number."""
        with self._guard("list cores"):
            rows = self._require().execute(f"{_CORE_SELECT} ORDER BY core_no").fetchall()
        return [CoreRecord(**dict(r)) for r in rows]

    def list_stereos(self, core_nos: Iterable[int] | None = None, *,
                     with_molfile: bool = True) -> list[StereoRecord]:
        """Every stereo variant (or those of ``core_nos``), by CORE then STEREO
        number, with ``batch_count``. Without ``with_molfile`` the molfile is
        not read and ``molfile`` is the empty string (a cheap listing)."""
        molfile = "s.molfile" if with_molfile else "'' AS molfile"
        select = _STEREO_SELECT.replace("s.molfile", molfile, 1)
        wanted = None if core_nos is None else sorted(set(core_nos))
        out: list[StereoRecord] = []
        with self._guard("list stereo variants"), self._transaction(immediate=False) as c:
            if wanted is None:
                rows = c.execute(f"{select} ORDER BY s.core_no, s.stereo_no").fetchall()
                out = [StereoRecord(**dict(r)) for r in rows]
            else:
                for chunk in _chunks(wanted):
                    marks = ", ".join("?" * len(chunk))
                    rows = c.execute(f"{select} WHERE s.core_no IN ({marks}) "
                                     "ORDER BY s.core_no, s.stereo_no", chunk).fetchall()
                    out.extend(StereoRecord(**dict(r)) for r in rows)
        return out

    def batch_summaries(self) -> list[dict[str, Any]]:
        """Every batch without its structure: ``core_no``, ``stereo_no``,
        ``batch_no``, ``full_id``, ``experiment_ref``, ``experiment_code``,
        ``experiment_url``, ``created_at``, ``retired`` (bool); oldest first
        (by ``created_at``, then CORE, STEREO and BATCH number)."""
        sql = ("SELECT core_no, stereo_no, batch_no, full_id, experiment_ref, experiment_code, "
               "experiment_url, created_at, retired_at IS NOT NULL AS retired FROM batches "
               "ORDER BY created_at, core_no, stereo_no, batch_no")
        with self._guard("read batches"):
            rows = self._require().execute(sql).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["retired"] = bool(d["retired"])
            out.append(d)
        return out

    def stereo_inchis(self, core_stereo_ids: Iterable[str] | None = None) -> dict[str, str]:
        """``{core_stereo_id: standard InChI as drawn}`` of each stereo variant
        (or of ``core_stereo_ids``): the InChI stored with its first batch,
        whose molfile is the variant's molfile."""
        sql = ("SELECT s.core_stereo_id, b.inchi FROM stereos AS s JOIN batches AS b "
               "ON b.core_no = s.core_no AND b.stereo_no = s.stereo_no AND b.batch_no = 1")
        out: dict[str, str] = {}
        with self._guard("read InChIs"), self._transaction(immediate=False) as c:
            if core_stereo_ids is None:
                out.update((r[0], r[1]) for r in c.execute(sql).fetchall())
            else:
                for chunk in _chunks(sorted(set(core_stereo_ids))):
                    marks = ", ".join("?" * len(chunk))
                    out.update((r[0], r[1]) for r in c.execute(
                        f"{sql} WHERE s.core_stereo_id IN ({marks})", chunk).fetchall())
        return out

    # ------------------------------------------------------------------ names
    @staticmethod
    def _name_row(conn: sqlite3.Connection, core_stereo_id: str) -> NameRecord | None:
        row = conn.execute(f"{_NAME_SELECT} WHERE core_stereo_id = ?",
                           (core_stereo_id,)).fetchone()
        return NameRecord(row[0], row[1], row[2], row[3]) if row is not None else None

    @staticmethod
    def _require_stereo(conn: sqlite3.Connection, core_stereo_id: object) -> str:
        if not isinstance(core_stereo_id, str) or not core_stereo_id.strip():
            raise _invalid("A CORE_STEREO ID is required.")
        core_stereo_id = core_stereo_id.strip()
        found = conn.execute("SELECT 1 FROM stereos WHERE core_stereo_id = ?",
                             (core_stereo_id,)).fetchone()
        if found is None:
            raise NotFoundError(f"No stereo variant {core_stereo_id!r} is registered.")
        return core_stereo_id

    def get_names(self) -> dict[str, NameRecord]:
        """Every stored name, by ``core_stereo_id``."""
        with self._guard("read names"):
            rows = self._require().execute(_NAME_SELECT).fetchall()
        return {r[0]: NameRecord(r[0], r[1], r[2], r[3]) for r in rows}

    def get_name(self, core_stereo_id: str) -> NameRecord | None:
        """The name stored for this stereo variant, or None. An unknown
        ``core_stereo_id`` raises ``NotFoundError``."""
        with self._guard("read a name"), self._transaction(immediate=False) as c:
            core_stereo_id = self._require_stereo(c, core_stereo_id)
            return self._name_row(c, core_stereo_id)

    def set_name(self, core_stereo_id: str, name: str | None,
                 source: str = "user") -> tuple[NameRecord | None, bool]:
        """Store (``name`` text) or remove (``name`` None) the name of a stereo
        variant. ``source`` (one of ``protocol.NAME_SOURCES``) says where the
        text came from; it is ignored when removing. Returns ``(the name now
        stored or None, changed)``; nothing is written when the stored pair
        (text, source) already was exactly that. Every change appends a
        ``name_set`` event (``details``: ``core_stereo_id``, ``name``,
        ``previous``, ``source``) or a ``name_removed`` event (``details``:
        ``core_stereo_id``, ``name`` None, ``previous``). The text is stored
        stripped; the caller validates it further (one line, length). Unknown
        ``core_stereo_id`` -> ``NotFoundError``; blank text or an unknown
        source -> ``invalid_argument``."""
        if source not in NAME_SOURCES:
            raise _invalid(f"Unknown name source {source!r} (allowed: {', '.join(NAME_SOURCES)}).")
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise _invalid("A name must be non-blank text (or None to remove it).")
            name = name.strip()
        with self._guard("store a name"), self._transaction() as c:
            core_stereo_id = self._require_stereo(c, core_stereo_id)
            old = self._name_row(c, core_stereo_id)
            if name is None and old is None:
                return None, False
            if name is not None and old is not None and (old.text, old.source) == (name, source):
                return old, False
            now = self._timestamp()
            details: dict[str, Any] = {"core_stereo_id": core_stereo_id, "name": name,
                                       "previous": old.text if old is not None else None}
            if name is None:
                c.execute("DELETE FROM names WHERE core_stereo_id = ?", (core_stereo_id,))
            else:
                c.execute("INSERT INTO names(core_stereo_id, name, updated_at, source) "
                          "VALUES (?, ?, ?, ?) ON CONFLICT(core_stereo_id) DO UPDATE SET "
                          "name = excluded.name, updated_at = excluded.updated_at, "
                          "source = excluded.source", (core_stereo_id, name, now, source))
                details["source"] = source
            self._insert_event(c, now, NAME_EVENTS[0] if name is not None else NAME_EVENTS[1],
                               None, details)
            new = self._name_row(c, core_stereo_id)
        return new, True

    # ------------------------------------------------------------------ spectra
    @staticmethod
    def _spectrum_dict(row: Sequence[Any]) -> dict[str, Any]:
        return {"full_id": row[0], "spectrum_key": row[1], "data": json.loads(row[2]),
                "updated_at": row[3]}

    def record_spectrum(self, full_id: str, spectrum_key: str,
                        data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Store what was measured for one spectrum of a batch, replacing what
        was stored for the same ``spectrum_key``. ``data`` is a JSON object
        (the caller decides its content). Returns ``({"full_id",
        "spectrum_key", "data", "updated_at"}, changed)``; nothing is written
        when exactly that data was already stored. A change appends a
        ``spectrum_recorded`` event (``details``: ``full_id``,
        ``spectrum_key``, ``replaced``). Unknown ``full_id`` ->
        ``NotFoundError``; a blank key or data that is not an object ->
        ``invalid_argument``."""
        if not isinstance(full_id, str) or not full_id.strip():
            raise _invalid("An ID is required.")
        if not isinstance(spectrum_key, str) or not spectrum_key.strip():
            raise _invalid("A spectrum key is required.")
        if not isinstance(data, dict):
            raise _invalid("The spectrum data must be an object.")
        full_id, spectrum_key = full_id.strip(), spectrum_key.strip()
        text = json.dumps(data, sort_keys=True, ensure_ascii=False)
        select = ("SELECT full_id, spectrum_key, data, updated_at FROM spectra "
                  "WHERE full_id = ? AND spectrum_key = ?")
        with self._guard("store a spectrum"), self._transaction() as c:
            if c.execute("SELECT 1 FROM batches WHERE full_id = ?", (full_id,)).fetchone() is None:
                raise NotFoundError(f"No batch {full_id!r} is registered.")
            old = c.execute(select, (full_id, spectrum_key)).fetchone()
            if old is not None and old[2] == text:
                return self._spectrum_dict(old), False
            now = self._timestamp()
            c.execute("INSERT INTO spectra(full_id, spectrum_key, data, updated_at) "
                      "VALUES (?, ?, ?, ?) ON CONFLICT(full_id, spectrum_key) DO UPDATE SET "
                      "data = excluded.data, updated_at = excluded.updated_at",
                      (full_id, spectrum_key, text, now))
            self._insert_event(c, now, SPECTRUM_EVENT, None, {
                "full_id": full_id, "spectrum_key": spectrum_key, "replaced": old is not None})
            return self._spectrum_dict(c.execute(select, (full_id, spectrum_key)).fetchone()), True

    def get_spectra(self, full_id: str | None = None,
                    core_stereo_id: str | None = None) -> list[dict[str, Any]]:
        """The recorded spectra of one batch, of every batch of one stereo
        variant, or (no argument) of the whole registry, ordered by ID and
        key. Unknown IDs simply give an empty list."""
        sql = ("SELECT sp.full_id, sp.spectrum_key, sp.data, sp.updated_at FROM spectra AS sp "
               "JOIN batches AS b ON b.full_id = sp.full_id "
               "JOIN stereos AS s ON s.core_no = b.core_no AND s.stereo_no = b.stereo_no")
        where, params = [], []
        if full_id is not None:
            where.append("sp.full_id = ?")
            params.append(str(full_id).strip())
        if core_stereo_id is not None:
            where.append("s.core_stereo_id = ?")
            params.append(str(core_stereo_id).strip())
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY b.batch_id, sp.spectrum_key"
        with self._guard("read spectra"):
            rows = self._require().execute(sql, params).fetchall()
        return [self._spectrum_dict(r) for r in rows]

    def get_stereo(self, core_stereo_id: str) -> StereoRecord | None:
        """The stereo variant with this ``CORE_STEREO`` (with its molfile and
        ``batch_count``), or None."""
        if not isinstance(core_stereo_id, str):
            return None
        with self._guard("read a stereo variant"):
            row = self._require().execute(f"{_STEREO_SELECT} WHERE s.core_stereo_id = ?",
                                          (core_stereo_id.strip(),)).fetchone()
        return StereoRecord(**dict(row)) if row is not None else None

    # ------------------------------------------------------------------ yields
    @staticmethod
    def _yield_row(row: Sequence[Any]) -> YieldRecord:
        return YieldRecord(full_id=row[0], isolated=row[1], analytical=row[2], updated_at=row[3])

    @staticmethod
    def _yield_value(name: str, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _invalid(f"The {name} yield must be a number of percent or None, got {value!r}.")
        number = float(value)
        if not 0 <= number < float("inf"):        # also refuses NaN
            raise _invalid(f"The {name} yield must be a finite number >= 0, got {value!r}.")
        return number

    def set_yield(self, full_id: str, isolated: float | None,
                  analytical: float | None) -> tuple[YieldRecord | None, bool]:
        """Store the isolated and analytical yield (percent) of a batch, or
        remove its row when both are None. Returns ``(the yield now stored or
        None, changed)``; nothing is written when exactly these values are
        already stored. Every change appends a ``yield_set`` or
        ``yield_removed`` event (``details``: ``full_id``, ``isolated``,
        ``analytical``, ``previous_isolated``, ``previous_analytical``). The
        values are stored as given; the caller validates the range and
        rounds. Unknown ``full_id`` -> ``NotFoundError``; a value that is not
        a finite number >= 0 -> ``invalid_argument``."""
        if not isinstance(full_id, str) or not full_id.strip():
            raise _invalid("An ID is required.")
        full_id = full_id.strip()
        isolated = self._yield_value("isolated", isolated)
        analytical = self._yield_value("analytical", analytical)
        remove = isolated is None and analytical is None
        select = f"{_YIELD_SELECT} WHERE full_id = ?"
        with self._guard("store a yield"), self._transaction() as c:
            if c.execute("SELECT 1 FROM batches WHERE full_id = ?", (full_id,)).fetchone() is None:
                raise NotFoundError(f"No batch {full_id!r} is registered.")
            row = c.execute(select, (full_id,)).fetchone()
            old = self._yield_row(row) if row is not None else None
            if remove and old is None:
                return None, False
            if old is not None and (old.isolated, old.analytical) == (isolated, analytical):
                return old, False
            now = self._timestamp()
            if remove:
                c.execute("DELETE FROM yields WHERE full_id = ?", (full_id,))
            else:
                c.execute("INSERT INTO yields(full_id, isolated, analytical, updated_at) "
                          "VALUES (?, ?, ?, ?) ON CONFLICT(full_id) DO UPDATE SET "
                          "isolated = excluded.isolated, analytical = excluded.analytical, "
                          "updated_at = excluded.updated_at",
                          (full_id, isolated, analytical, now))
            self._insert_event(c, now, YIELD_EVENTS[1] if remove else YIELD_EVENTS[0], None, {
                "full_id": full_id, "isolated": isolated, "analytical": analytical,
                "previous_isolated": old.isolated if old is not None else None,
                "previous_analytical": old.analytical if old is not None else None})
            row = c.execute(select, (full_id,)).fetchone()
        return (self._yield_row(row) if row is not None else None), True

    def get_yields(self, full_ids: Iterable[str] | None = None) -> dict[str, YieldRecord]:
        """The stored yields, by full ID: of every batch, or of ``full_ids``
        (unknown IDs and batches without a yield are simply absent)."""
        out: dict[str, YieldRecord] = {}
        with self._guard("read yields"), self._transaction(immediate=False) as c:
            if full_ids is None:
                rows = c.execute(_YIELD_SELECT).fetchall()
                out.update((r[0], self._yield_row(r)) for r in rows)
            else:
                for chunk in _chunks(sorted({str(i) for i in full_ids})):
                    marks = ", ".join("?" * len(chunk))
                    rows = c.execute(f"{_YIELD_SELECT} WHERE full_id IN ({marks})",
                                     chunk).fetchall()
                    out.update((r[0], self._yield_row(r)) for r in rows)
        return out

    def batch_yields(self) -> list[dict[str, Any]]:
        """Every batch without its structure, with its experiment, the name of
        its stereo variant and its stored yield, in one query (oldest first):
        ``full_id``, ``core_stereo_id``, ``experiment_ref``,
        ``experiment_code``, ``experiment_url``, ``product_index``,
        ``created_at``, ``retired`` (bool), ``name`` (text or None) and
        ``yield`` (:class:`YieldRecord` or None)."""
        sql = ("SELECT b.full_id, s.core_stereo_id, b.experiment_ref, b.experiment_code, "
               "b.experiment_url, b.product_index, b.created_at, "
               "b.retired_at IS NOT NULL AS retired, n.name, "
               "y.full_id AS y_id, y.isolated, y.analytical, y.updated_at AS y_at "
               "FROM batches AS b "
               "JOIN stereos AS s ON s.core_no = b.core_no AND s.stereo_no = b.stereo_no "
               "LEFT JOIN names AS n ON n.core_stereo_id = s.core_stereo_id "
               "LEFT JOIN yields AS y ON y.full_id = b.full_id "
               "ORDER BY b.batch_id")
        with self._guard("read batches"):
            rows = self._require().execute(sql).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            y_id, iso, ana, at = d.pop("y_id"), d.pop("isolated"), d.pop("analytical"), d.pop("y_at")
            d["retired"] = bool(d["retired"])
            d["yield"] = YieldRecord(y_id, iso, ana, at) if y_id is not None else None
            out.append(d)
        return out

    # ------------------------------------------------------------------ reactions
    @staticmethod
    def _reaction_dict(row: Sequence[Any], with_data: bool = True) -> dict[str, Any]:
        out = {"experiment_ref": row[0], "experiment_code": row[1], "experiment_url": row[2],
               "updated_at": row[4],
               "digest": hashlib.sha256(row[3].encode("utf-8")).hexdigest()}
        if with_data:
            out["data"] = json.loads(row[3])
        return out

    @staticmethod
    def _reaction_text(data: object) -> str:
        """Canonical JSON of ``data`` (an object, or JSON text of one)."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError as exc:
                raise _invalid(f"The reaction data is not valid JSON: {exc}.") from None
        if not isinstance(data, dict):
            raise _invalid("The reaction data must be a JSON object.")
        try:
            return canonical_json(data)
        except (TypeError, ValueError) as exc:
            raise _invalid(f"The reaction data cannot be stored as JSON: {exc}.") from None

    def get_reaction(self, experiment_ref: str) -> dict[str, Any] | None:
        """The reaction stored for this experiment (internal key), or None:
        ``{"experiment_ref", "experiment_code", "experiment_url", "data",
        "updated_at", "digest"}`` with ``data`` the parsed JSON object and
        ``digest`` the SHA-256 (hex) of the stored text."""
        if not isinstance(experiment_ref, str) or not experiment_ref.strip():
            return None
        with self._guard("read a reaction"):
            row = self._require().execute(f"{_REACTION_SELECT} WHERE experiment_ref = ?",
                                          (experiment_ref.strip(),)).fetchone()
        return self._reaction_dict(row) if row is not None else None

    def list_reactions(self, with_data: bool = False) -> list[dict[str, Any]]:
        """Every stored reaction, by internal key, as :meth:`get_reaction`
        gives it (without ``data`` unless ``with_data``)."""
        with self._guard("read reactions"):
            rows = self._require().execute(
                f"{_REACTION_SELECT} ORDER BY experiment_ref").fetchall()
        return [self._reaction_dict(r, with_data) for r in rows]

    def set_reaction(self, experiment_ref: str, experiment_code: str | None,
                     experiment_url: str | None, data: object) -> tuple[dict[str, Any], bool]:
        """Store the reaction of an experiment, replacing what was stored for
        it. ``data`` is a JSON object (or JSON text of one); the caller
        decides its content. It is stored as canonical JSON
        (:func:`canonical_json`). ``experiment_code`` / ``experiment_url``
        None keep what is stored. Returns ``(the record now stored, as
        :meth:`get_reaction` gives it, changed)``: nothing is written when the
        same data, ELN number and address are stored already. A change
        appends a ``reaction_set`` event (``details``: ``experiment_ref``,
        ``code``, ``digest``, ``replaced``). The experiment is not checked
        against the batches here (the service does that). A blank key or
        data that is not an object -> ``invalid_argument``."""
        if not isinstance(experiment_ref, str) or not experiment_ref.strip():
            raise _invalid("An experiment reference is required.")
        ref = experiment_ref.strip()
        code = (experiment_code.strip() or None) if isinstance(experiment_code, str) else None
        url = (experiment_url.strip() or None) if isinstance(experiment_url, str) else None
        text = self._reaction_text(data)
        select = f"{_REACTION_SELECT} WHERE experiment_ref = ?"
        with self._guard("store a reaction"), self._transaction() as c:
            old = c.execute(select, (ref,)).fetchone()
            if old is not None:
                code = code if code is not None else old[1]
                url = url if url is not None else old[2]
                if (old[1], old[2], old[3]) == (code, url, text):
                    return self._reaction_dict(old), False
            now = self._timestamp()
            c.execute("INSERT INTO reactions(experiment_ref, experiment_code, experiment_url, "
                      "data, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(experiment_ref) "
                      "DO UPDATE SET experiment_code = excluded.experiment_code, "
                      "experiment_url = excluded.experiment_url, data = excluded.data, "
                      "updated_at = excluded.updated_at", (ref, code, url, text, now))
            record = self._reaction_dict(c.execute(select, (ref,)).fetchone())
            self._insert_event(c, now, REACTION_EVENT, None, {
                "experiment_ref": ref, "code": code, "digest": record["digest"],
                "replaced": old is not None})
        return record, True

    def get_limiting(self, experiment_ref: str) -> tuple[int, str | None] | None:
        """The starting material chosen for this experiment: ``(row, name
        recorded at that time)``, or None (automatic, or no reaction stored)."""
        if not isinstance(experiment_ref, str) or not experiment_ref.strip():
            return None
        with self._guard("read a starting-material choice"):
            row = self._require().execute(
                "SELECT limiting_row, limiting_name FROM reactions WHERE experiment_ref = ?",
                (experiment_ref.strip(),)).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0]), row[1]

    def set_limiting(self, experiment_ref: str, row: int | None,
                     name: str | None = None) -> bool:
        """Record the starting material chosen for an experiment whose
        reaction is stored: reactant ``row`` with its ``name`` at this moment
        (``row`` None: automatic again, the name is dropped). Returns
        ``changed``: nothing is written when exactly this is stored already.
        A change appends a ``limiting_set`` event (``details``:
        ``experiment_ref``, ``code``, ``row``, ``name``). The row is not
        checked against the reaction here (the service does that).
        ``NotFoundError`` when no reaction is stored for the experiment; a
        blank key or a row that is not a whole number from 1 ->
        ``invalid_argument``."""
        if not isinstance(experiment_ref, str) or not experiment_ref.strip():
            raise _invalid("An experiment reference is required.")
        if row is not None and (isinstance(row, bool) or not isinstance(row, int) or row < 1):
            raise _invalid(f"The reactant row must be a whole number from 1, got {row!r}.")
        ref = experiment_ref.strip()
        if row is None:
            name = None
        elif name is not None:
            name = str(name).strip() or None
        with self._guard("store a starting-material choice"), self._transaction() as c:
            old = c.execute("SELECT experiment_code, limiting_row, limiting_name FROM reactions "
                            "WHERE experiment_ref = ?", (ref,)).fetchone()
            if old is None:
                raise NotFoundError(f"No reaction is stored for the experiment {ref!r}.")
            if (old[1], old[2]) == (row, name):
                return False
            c.execute("UPDATE reactions SET limiting_row = ?, limiting_name = ? "
                      "WHERE experiment_ref = ?", (row, name, ref))
            self._insert_event(c, self._timestamp(), LIMITING_EVENT, None, {
                "experiment_ref": ref, "code": old[0], "row": row, "name": name})
        return True


def _chunks(values: Sequence[Any], size: int = 500) -> Iterator[list[Any]]:
    for start in range(0, len(values), size):
        yield list(values[start:start + size])


def open_repository(config: Config, new_registry_id: str | None = None,
                    unstamped_journal_own: bool = False) -> SqliteRepository:
    """Open (or create) the registry described by ``config``.
    ``new_registry_id``, ``unstamped_journal_own``: see :class:`SqliteRepository`."""
    return SqliteRepository(config.db_path, IdFormat(config.id_format),
                            upgrade_backup_dir=config.backup_dir,
                            new_registry_id=new_registry_id,
                            unstamped_journal_own=unstamped_journal_own)
