"""The issued-ID journal: an append-only record of every ID, kept OUTSIDE the database.

Why
---
IDs are written into a lab notebook, so an ID must never be issued twice. If
the live database is ever replaced by an older backup, it "forgets" the IDs
issued after that backup and would hand the same numbers out again. The
journal remembers them independently of the database:

* ``<home>/issued-ids.jsonl``         - the main journal, next to config.toml;
* ``<backup folder>/issued-ids.jsonl`` - a best-effort mirror (the backup
  folder may be synced to the cloud, so it survives the loss of the home folder).

Format
------
UTF-8 text, one JSON object per line, appended (never rewritten) AFTER the
database transaction committed; each append is flushed and ``fsync``-ed.

* ``{"event": "register", "id": ..., "at": <created_at>, "experiment_ref": ...,
  "experiment_code": ..., "experiment_url": ..., "product_index": ...,
  "source": ..., <every Normalized field in REGISTER_NORM_FIELDS>,
  "accepted_ignored_stereo": [...]}`` - enough to re-create the registration
  without recomputing any chemistry;
* ``{"event": "retire", "id": ..., "at": <retired_at>, "reason": ...}``.

Every line also carries ``"registry_id"``, the identity of the registry that
wrote it (``meta.registry_id`` of its database). A journal file belongs to
one registry, but nothing stops two registries from being pointed at the same
backup folder (or two databases at the same home folder). When reading for a
registry, a line with ANOTHER ``registry_id`` is foreign: it is left out of
the entries and counted per registry in ``JournalFile.foreign``, so that it
is never taken for an ID this database has lost.

A line without ``registry_id`` names nobody. Only a registry that says so
(``unstamped_own``: its database comes from a file or a home folder whose
journal has such lines, see
:attr:`compound_registry.sqlite_repo.SqliteRepository.unstamped_journal_own`)
reads such lines as its own. It then appends a copy of each with its
``registry_id`` to the file (:attr:`JournalFile.unstamped`), so that every
line of that registry names it. For every other reader a line without
``registry_id`` is its own only when the journal also holds the same line
with the reader's ``registry_id``; otherwise it is foreign and counted under
:data:`UNNAMED_REGISTRY`.

Reading is tolerant: a missing file is an empty journal, a line that is not
valid JSON (typically a last line cut short by a crash) is skipped and
counted, and duplicate lines (the same event for the same ID) count once.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .files import same_path
from .models import Normalized

JOURNAL_FILENAME = "issued-ids.jsonl"
EVENTS = ("register", "retire")
#: Key of a line that names the registry which wrote it.
REGISTRY_ID_FIELD = "registry_id"
#: Stands in ``JournalFile.foreign`` for the writer of the lines that name no
#: registry and are not the reader's.
UNNAMED_REGISTRY = "unnamed"

#: Normalized fields stored in a ``register`` entry (all that ``register`` needs).
REGISTER_NORM_FIELDS = (
    "core_key", "stereo_key", "stereo_mode", "inchikey", "core_inchikey", "smiles", "inchi",
    "formula", "detected_mode", "mode_overridden", "molblock", "input_format",
    "rdkit_version", "inchi_version",
)
#: Fields compared between a journalled registration and the database row with
#: the same ID. Any difference means the ID now names something else.
IDENTITY_FIELDS = ("at", "experiment_ref", "product_index", "inchikey")


def journal_paths(config: Config) -> tuple[Path, Path]:
    """``(main journal, mirror)`` for a configuration."""
    return Path(config.home) / JOURNAL_FILENAME, Path(config.backup_dir) / JOURNAL_FILENAME


def entry_key(entry: dict[str, Any]) -> tuple[str, str]:
    """``(event, id)``: two lines with the same key describe the same change."""
    return entry["event"], entry["id"]


def register_entry(
    full_id: str,
    at: str,
    *,
    experiment_ref: str,
    experiment_code: str | None,
    experiment_url: str | None,
    product_index: int,
    source: str,
    norm: Normalized | dict[str, Any],
    accepted_ignored_stereo: Iterable[str] = (),
    registry_id: str | None = None,
) -> dict[str, Any]:
    """A ``register`` entry. ``norm`` is a :class:`Normalized` or a dict with
    the :data:`REGISTER_NORM_FIELDS` (e.g. from ``registration_details``).
    ``registry_id``: the registry that issued the ID."""
    entry: dict[str, Any] = {
        "event": "register",
        "id": full_id,
        "at": at,
        "experiment_ref": experiment_ref,
        "experiment_code": experiment_code,
        "experiment_url": experiment_url,
        "product_index": product_index,
        "source": source,
    }
    for name in REGISTER_NORM_FIELDS:
        value = getattr(norm, name) if isinstance(norm, Normalized) else norm[name]
        entry[name] = bool(value) if name == "mode_overridden" else value
    entry["accepted_ignored_stereo"] = list(accepted_ignored_stereo)
    if registry_id:
        entry[REGISTRY_ID_FIELD] = registry_id
    return entry


def retire_entry(full_id: str, at: str, reason: str,
                 registry_id: str | None = None) -> dict[str, Any]:
    """A ``retire`` entry."""
    entry = {"event": "retire", "id": full_id, "at": at, "reason": reason}
    if registry_id:
        entry[REGISTRY_ID_FIELD] = registry_id
    return entry


def normalized_from_entry(entry: dict[str, Any]) -> Normalized:
    """Rebuild the :class:`Normalized` of a ``register`` entry (no chemistry).
    Raises ``KeyError`` / ``TypeError`` for an incomplete entry."""
    values = {name: entry[name] for name in REGISTER_NORM_FIELDS}
    for name in REGISTER_NORM_FIELDS:
        if name == "mode_overridden":
            if not isinstance(values[name], bool):
                raise TypeError(f"{name} must be true or false")
        elif not isinstance(values[name], str):
            raise TypeError(f"{name} must be text")
    return Normalized(**values)


def _valid_entry(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("event") not in EVENTS:
        return False
    return isinstance(obj.get("id"), str) and bool(obj["id"]) and isinstance(obj.get("at"), str)


@dataclass
class JournalFile:
    """The readable content of one journal file."""

    path: Path
    entries: list[dict[str, Any]] = field(default_factory=list)  # deduplicated, file order
    unreadable_lines: int = 0
    exists: bool = False
    error: str | None = None  # the file exists but could not be read
    #: Lines written by other registries: ``{registry_id: number of lines}``
    #: (:data:`UNNAMED_REGISTRY` for foreign lines that name no registry).
    foreign: dict[str, int] = field(default_factory=dict)
    #: The reader's own lines that name no registry and have no copy with its
    #: ``registry_id`` in this file (deduplicated, file order).
    unstamped: list[dict[str, Any]] = field(default_factory=list)
    #: Every valid line as written, in file order.
    lines: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def keys(self) -> set[tuple[str, str]]:
        return {entry_key(e) for e in self.entries}


def _writer(line: dict[str, Any]) -> str | None:
    """The registry a line names, or None."""
    writer = line.get(REGISTRY_ID_FIELD)
    return writer if isinstance(writer, str) and writer else None


def _content(line: dict[str, Any]) -> str:
    """What a line says, whoever wrote it (to compare a line without
    ``registry_id`` with its stamped copy)."""
    return json.dumps({k: v for k, v in line.items() if k != REGISTRY_ID_FIELD},
                      sort_keys=True, ensure_ascii=True)


def stamped(line: dict[str, Any], registry_id: str) -> dict[str, Any]:
    """A copy of ``line`` that names ``registry_id`` as its writer."""
    return {**line, REGISTRY_ID_FIELD: registry_id}


def _load(path: Path) -> JournalFile:
    """The valid lines of one journal file; never raises."""
    result = JournalFile(path=Path(path))
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return result
    except OSError as exc:
        result.exists = True
        result.error = f"{path} could not be read: {exc}"
        return result
    result.exists = True
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            result.unreadable_lines += 1
            continue
        if not _valid_entry(obj):
            result.unreadable_lines += 1
            continue
        result.lines.append(obj)
    return result


def _sort_lines(files: list[JournalFile], registry_id: str | None, unstamped_own: bool) -> None:
    """Fill ``entries``, ``foreign`` and ``unstamped`` of loaded files that
    are read together for the registry ``registry_id``."""
    own_stamped = [{_content(line) for line in jf.lines
                    if registry_id and _writer(line) == registry_id} for jf in files]
    anywhere: set[str] = set().union(*own_stamped)
    for jf, here in zip(files, own_stamped):
        seen: set[tuple[str, str]] = set()
        unstamped_seen: set[str] = set()
        for line in jf.lines:
            writer = _writer(line)
            if registry_id and writer is None:
                content = _content(line)
                if not unstamped_own and content not in anywhere:
                    jf.foreign[UNNAMED_REGISTRY] = jf.foreign.get(UNNAMED_REGISTRY, 0) + 1
                    continue
                if content not in here and content not in unstamped_seen:
                    unstamped_seen.add(content)
                    jf.unstamped.append(line)
            elif registry_id and writer is not None and writer != registry_id:
                jf.foreign[writer] = jf.foreign.get(writer, 0) + 1
                continue
            key = entry_key(line)
            if key in seen:
                continue
            seen.add(key)
            jf.entries.append(line)


def read_journals(paths: Iterable[Path], registry_id: str | None = None,
                  unstamped_own: bool = True) -> list[JournalFile]:
    """Read the journal files of one registry (main journal and mirror)
    together; never raises (problems are reported in the results).

    ``registry_id``: the registry that reads. Lines that name another
    registry are counted in ``foreign`` and left out of ``entries``. Without
    it every line is an entry.

    ``unstamped_own``: whether lines that name no registry are the reader's.
    If not, such a line is still the reader's when one of the files holds the
    same line with the reader's ``registry_id``; else it is foreign."""
    files = [_load(Path(p)) for p in paths]
    _sort_lines(files, registry_id, unstamped_own)
    return files


def read_journal(path: Path, registry_id: str | None = None,
                 unstamped_own: bool = True) -> JournalFile:
    """Read one journal file on its own (see :func:`read_journals`)."""
    return read_journals([path], registry_id, unstamped_own)[0]


@dataclass(frozen=True)
class JournalOrigin:
    """Who wrote the main journal of a home folder (see :func:`journal_origin`)."""

    #: The registry its lines name, when they name exactly one; else None.
    registry_id: str | None
    #: The lines that name no registry and have no copy naming one.
    unstamped: list[dict[str, Any]]


def journal_origin(config: Config) -> JournalOrigin:
    """Who wrote the main journal of ``config`` (the one in the home folder).
    Used when a database is created in a home folder whose journal survived,
    so that the new database continues that registry.

    The mirror in the backup folder is deliberately not asked: a backup
    folder may wrongly be shared, and a new registry must never take the
    identity of the other registry that writes there."""
    lines = _load(journal_paths(config)[0]).lines
    ids = {writer for writer in map(_writer, lines) if writer}
    copied = {_content(line) for line in lines if _writer(line)}
    unstamped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        content = _content(line)
        if _writer(line) is None and content not in copied and content not in seen:
            seen.add(content)
            unstamped.append(line)
    return JournalOrigin(ids.pop() if len(ids) == 1 else None, unstamped)


def journal_registry_id(config: Config) -> str | None:
    """The registry the main journal of ``config`` belongs to, when its
    lines name exactly one. None when no line names a registry or the lines
    name several (see :func:`journal_origin`)."""
    return journal_origin(config).registry_id


def merge(files: Iterable[JournalFile]) -> list[dict[str, Any]]:
    """Deduplicated union of several journals: every entry of the first file in
    its order, then the entries only the next files have, in their order."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for jf in files:
        for entry in jf.entries:
            key = entry_key(entry)
            if key not in seen:
                seen.add(key)
                out.append(entry)
    return out


def encode(entry: dict[str, Any]) -> bytes:
    """One journal line (ASCII JSON, sorted keys, trailing newline)."""
    return (json.dumps(entry, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def append_entries(path: Path, entries: Iterable[dict[str, Any]]) -> None:
    """Append entries to ``path`` (created with its folder if missing), then
    flush and ``fsync``. If the file does not end with a newline (a previous
    append was cut short), a newline is written first, so the cut line stays
    on its own and the new lines remain readable. Raises ``OSError``."""
    payload = b"".join(encode(e) for e in entries)
    if not payload:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab+") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                payload = b"\n" + payload
            fh.seek(0, os.SEEK_END)
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())


class Journal:
    """The main journal plus its mirror.

    :meth:`append` writes to both and never raises: the database already holds
    the change, so a journal problem is a warning (the missing lines are added
    by the next write's catch-up).
    """

    def __init__(self, path: Path, mirror_path: Path | None = None):
        self.path = Path(path)
        self.mirror_path = Path(mirror_path) if mirror_path is not None else None

    @classmethod
    def for_config(cls, config: Config) -> "Journal":
        main, mirror = journal_paths(config)
        return cls(main, None if same_path(main, mirror) else mirror)

    def files(self) -> list[Path]:
        """The journal files, main first."""
        return [self.path] + ([self.mirror_path] if self.mirror_path is not None else [])

    def read(self, registry_id: str | None = None,
             unstamped_own: bool = True) -> list[JournalFile]:
        """Every journal file (missing ones are empty), read together for
        the registry ``registry_id`` (see :func:`read_journals`)."""
        return read_journals(self.files(), registry_id, unstamped_own)

    def append(self, entries: list[dict[str, Any]], *, mirror_entries: list[dict[str, Any]] | None = None
               ) -> "AppendOutcome":
        """Append ``entries`` to the main journal and ``mirror_entries``
        (default: the same) to the mirror. Never raises."""
        warnings: list[str] = []
        main_ok = True
        if entries:
            try:
                append_entries(self.path, entries)
            except OSError as exc:
                main_ok = False
                ids = ", ".join(e["id"] for e in entries[:5])
                warnings.append(
                    f"Could not write the issued-ID journal {self.path} ({exc}); {ids} "
                    "is registered in the database and will be added to the journal at the "
                    "next registration. Check that the folder is writable."
                )
        mirrored = entries if mirror_entries is None else mirror_entries
        if self.mirror_path is not None and mirrored:
            try:
                append_entries(self.mirror_path, mirrored)
            except OSError as exc:
                warnings.append(f"Could not write the journal mirror {self.mirror_path}: {exc}")
        return AppendOutcome(main_ok=main_ok, warnings=warnings)


@dataclass(frozen=True)
class AppendOutcome:
    """``main_ok``: the main journal holds the entries. ``warnings``: problems
    to show the user (main journal or mirror not written)."""

    main_ok: bool
    warnings: list[str]
