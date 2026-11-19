"""Timestamped, verified backups of the registry database.

A backup is written to a hidden temporary name in the backup folder
(``.registry-YYYYMMDD-HHMMSS.sqlite.<random>.partial``), checked with
``PRAGMA integrity_check`` and only then renamed to
``registry-YYYYMMDD-HHMMSS.sqlite`` (UTC). A sync client watching the folder
therefore never sees a half-written file. An existing file is never replaced:
when the name is taken (two backups in the same second), the new file gets
``-2``, ``-3`` ... before ``.sqlite``.

A backup folder should belong to one registry, but nothing stops two
registries from being pointed at the same folder. Every backup holds the
identity of its registry (``meta.registry_id``, see
:func:`compound_registry.sqlite_repo.registry_identity`), and a registry only
lists, counts and deletes its own backups.

Backups run after an ID has already been committed, so a failing backup must
never fail the registration: :func:`backup_now` and :func:`maybe_backup` never
raise and report problems as ``BackupOutcome.warning``.

Restoring = copying a backup file to the configured database path while no
program is using the registry, then running ``compound-registry recover``: a
backup is older than the IDs issued after it, and the issued-ID journal
(:mod:`compound_registry.journal`) blocks new registrations until those IDs
have been re-created.

Retention never deletes the backup that was just written, even when the clock
went backwards and its name sorts as the oldest one.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .files import same_path as _same_file
from .sqlite_repo import (
    SqliteRepository,
    format_timestamp,
    parse_timestamp,
    read_registry_identity,
)

#: ``registry-YYYYMMDD-HHMMSS.sqlite``, or with ``-N`` (N >= 2) before
#: ``.sqlite`` for a further backup of the same second.
BACKUP_NAME_RE = re.compile(r"^registry-(\d{8})-(\d{6})(?:-([1-9]\d{0,5}))?\.sqlite$")
# How many names of one second are tried before a backup gives up.
_MAX_SAME_SECOND = 1000
LAST_BACKUP_KEY = "last_backup_at"


@dataclass(frozen=True)
class BackupOutcome:
    """``performed``: a new backup file exists at ``path``. ``warning``: a
    problem to show the user (backup failed, or done but with a side issue).
    ``removed``: old backups deleted by the retention rule."""

    performed: bool
    path: Path | None = None
    warning: str | None = None
    removed: tuple[Path, ...] = ()


def _utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def backup_name(moment: datetime) -> str:
    """``registry-YYYYMMDD-HHMMSS.sqlite`` for a moment (converted to UTC)."""
    return f"registry-{_utc(moment):%Y%m%d-%H%M%S}.sqlite"


def _is_unc(text: str) -> bool:
    """True for ``\\\\server\\share\\...`` (also ``//server/share`` and the
    ``\\\\?\\UNC\\`` long form)."""
    return text.startswith(("\\\\", "//"))


def readonly_target(path: Path) -> tuple[str, bool]:
    """How to open ``path`` read-only: ``(target, is_uri)``.

    * A local drive path (``C:\\...``) or POSIX path becomes a well-formed
      ``file:`` URI with ``mode=ro`` (``file:///C:/...?mode=ro``).
    * A UNC path (``\\\\server\\share\\...``) cannot be written as a SQLite
      URI (``file://server/share/...`` names an authority, which SQLite
      rejects), so the plain path is returned and the caller must make the
      connection read-only with ``PRAGMA query_only = ON``.
    """
    text = os.path.abspath(str(path))
    if _is_unc(text):
        return text, False
    return Path(text).as_uri() + "?mode=ro", True


def open_readonly(path: Path) -> sqlite3.Connection:
    """A connection that cannot modify ``path`` (see :func:`readonly_target`).
    The file must exist (a plain connection would otherwise create it)."""
    target, is_uri = readonly_target(path)
    if is_uri:
        return sqlite3.connect(target, uri=True)
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    return conn


def verify_backup(path: Path) -> str:
    """``"ok"`` if ``path`` is an intact registry database, else a description
    of the problem (never raises). Works for local and UNC paths."""
    path = Path(path)
    if not path.is_file():
        return f"{path} does not exist"
    try:
        with closing(open_readonly(path)) as conn:
            rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
            if rows != ["ok"]:
                return "\n".join(rows)
            has_meta = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
            ).fetchone()
            if not has_meta or conn.execute(
                "SELECT 1 FROM meta WHERE key = 'id_format'"
            ).fetchone() is None:
                return f"{path} is an SQLite file but not a compound registry"
            return "ok"
    except (sqlite3.Error, OSError, ValueError) as exc:
        return f"{path} could not be read: {exc}"


def backup_registry_id(path: Path) -> str | None:
    """The identity of the registry a backup file belongs to, or None when
    the file cannot be read or is not a registry. Never raises."""
    try:
        with closing(open_readonly(Path(path))) as conn:
            return read_registry_identity(conn)
    except (sqlite3.Error, OSError, ValueError):
        return None


def _name_order(name: str) -> tuple[str, str, int]:
    m = BACKUP_NAME_RE.match(name)
    assert m is not None
    return m.group(1), m.group(2), int(m.group(3) or 1)


def list_backups(config: Config, registry_id: str | None = None) -> list[Path]:
    """Finished backup files in ``config.backup_dir``, oldest first. With
    ``registry_id`` only the backups of that registry (every file is opened
    read-only to find out; one that cannot be read is left out)."""
    folder = Path(config.backup_dir)
    try:
        names = [
            entry.name for entry in os.scandir(folder)
            if BACKUP_NAME_RE.match(entry.name) and entry.is_file()
        ]
    except OSError:
        return []
    paths = [folder / name for name in sorted(names, key=_name_order)]
    if registry_id is None:
        return paths
    return [path for path in paths if backup_registry_id(path) == registry_id]


def newest_backup(config: Config, registry_id: str | None = None) -> Path | None:
    """The newest finished backup in ``config.backup_dir`` (with
    ``registry_id``: of that registry), or None. The files are asked newest
    first, so that normally only one is opened."""
    for path in reversed(list_backups(config)):
        if registry_id is None or backup_registry_id(path) == registry_id:
            return path
    return None


def registry_id_from_backups(config: Config,
                             registrations: dict[str, tuple[object, ...]]) -> str | None:
    """The identity of the registry whose issued IDs are ``registrations``
    (``{full_id: (created_at, experiment_ref, product_index, inchikey)}``),
    read from its backups: that of the newest backup in ``config.backup_dir``
    which holds at least one ID and only IDs of ``registrations``, each with
    the same four values. None when there is no such backup. Never raises."""
    for path in reversed(list_backups(config)):
        try:
            with closing(open_readonly(path)) as conn:
                identity = read_registry_identity(conn)
                if identity is None:
                    continue
                rows = conn.execute(
                    "SELECT full_id, created_at, experiment_ref, product_index, inchikey "
                    "FROM batches").fetchall()
        except (sqlite3.Error, OSError, ValueError):
            continue
        if rows and all(registrations.get(row[0]) == tuple(row[1:]) for row in rows):
            return identity
    return None


def _apply_retention(config: Config, protect: Path | None = None,
                     registry_id: str | None = None) -> tuple[tuple[Path, ...], str | None]:
    """Delete the oldest backups so that at most ``config.backup_keep`` remain.

    ``protect`` (the backup just written) is never deleted, whatever its name
    says about its age: if the clock went backwards, the newest file can sort
    as the oldest one.

    ``registry_id``: the registry that cleans up. Only its own backups are
    counted and deleted; the backups another registry keeps in the same
    folder are left alone.
    """
    keep = config.backup_keep
    if keep <= 0:
        return (), None
    backups = list_backups(config, registry_id)
    excess = max(0, len(backups) - keep)
    if protect is not None:
        backups = [b for b in backups if not _same_file(b, protect)]
    removed: list[Path] = []
    problems: list[str] = []
    for old in backups[:excess]:
        try:
            os.remove(old)
            removed.append(old)
        except OSError as exc:
            problems.append(f"{old.name}: {exc}")
    warning = f"Could not delete old backup(s): {'; '.join(problems)}" if problems else None
    return tuple(removed), warning


def _remove_quietly(path: Path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# A ``.partial`` file older than this was left behind by a backup that was
# interrupted, and is removed by the next backup.
_STALE_PARTIAL_SECONDS = 24 * 3600
_PARTIAL_NAME_RE = re.compile(r"^\.registry-\d{8}-\d{6}\.sqlite\.(?:[0-9a-f]+\.)?partial$")


def _remove_stale_partials(folder: Path) -> None:
    try:
        limit = time.time() - _STALE_PARTIAL_SECONDS
        for entry in os.scandir(folder):
            if _PARTIAL_NAME_RE.match(entry.name) and entry.stat().st_mtime < limit:
                _remove_quietly(Path(entry.path))
    except OSError:
        pass


def _place(partial: Path, folder: Path, name: str) -> Path:
    """Give the finished file ``partial`` its backup name in ``folder``
    without ever replacing a file: ``name``, else ``name`` with ``-2``,
    ``-3`` ... Returns the final path. Raises ``OSError``."""
    stem = name[:-len(".sqlite")]
    for number in range(1, _MAX_SAME_SECOND + 1):
        final = folder / (name if number == 1 else f"{stem}-{number}.sqlite")
        try:
            # A hard link fails when the name is taken, also when another
            # program takes it at this very moment.
            os.link(partial, final)
        except FileExistsError:
            continue
        except OSError:
            # No hard links here (some network and removable drives).
            if final.exists():
                continue
            os.replace(partial, final)
            return final
        _remove_quietly(partial)
        return final
    raise OSError(f"{folder} already holds {_MAX_SAME_SECOND} backups named {stem}")


def backup_now(repo: SqliteRepository, config: Config, now: datetime | None = None) -> BackupOutcome:
    """Write a verified backup now. Never raises."""
    partial: Path | None = None
    registry_id = getattr(repo, "registry_id", None) or None
    try:
        moment = _utc(now)
        folder = Path(os.path.abspath(Path(config.backup_dir).expanduser()))
        name = backup_name(moment)
        final = folder / name
        db_path = getattr(repo, "path", config.db_path)
        if _same_file(final, db_path) or _same_file(final, config.db_path):
            return BackupOutcome(
                performed=False,
                warning=f"Backup skipped: {final} would overwrite the live database.",
            )
        folder.mkdir(parents=True, exist_ok=True)
        _remove_stale_partials(folder)
        partial = folder / f".{name}.{uuid.uuid4().hex[:8]}.partial"
        repo.backup_to(partial)
        result = verify_backup(partial)
        if result != "ok":
            _remove_quietly(partial)
            return BackupOutcome(performed=False, warning=f"Backup failed verification: {result}")
        final = _place(partial, folder, name)
        partial = None
    except Exception as exc:  # noqa: BLE001 - a backup must never break the caller
        if partial is not None:
            _remove_quietly(partial)
        message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        return BackupOutcome(performed=False, warning=f"Backup failed: {message}")

    warnings: list[str] = []
    try:
        repo.record_event(
            "backup",
            details={"path": str(final), "bytes": final.stat().st_size,
                     "registry_id": registry_id},
            meta={LAST_BACKUP_KEY: format_timestamp(moment)},
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Backup written to {final}, but it could not be recorded: {exc}")
    try:
        removed, retention_warning = _apply_retention(config, protect=final,
                                                      registry_id=registry_id)
    except Exception as exc:  # noqa: BLE001
        removed, retention_warning = (), f"Retention cleanup failed: {exc}"
    if retention_warning:
        warnings.append(retention_warning)
    return BackupOutcome(
        performed=True, path=final, warning=" ".join(warnings) or None, removed=removed
    )


def maybe_backup(repo: SqliteRepository, config: Config, now: datetime | None = None) -> BackupOutcome:
    """Back up if no backup was recorded yet, or the last one is at least
    ``config.backup_interval_hours`` old (or lies in the future, i.e. the clock
    went backwards). Never raises."""
    try:
        moment = _utc(now)
        last_text = repo.get_meta(LAST_BACKUP_KEY)
        if last_text:
            try:
                last = parse_timestamp(last_text)
            except ValueError:
                last = None
            if last is not None:
                elapsed = moment - last
                interval = timedelta(hours=config.backup_interval_hours)
                if timedelta(0) <= elapsed < interval:
                    return BackupOutcome(performed=False)
    except Exception as exc:  # noqa: BLE001
        message = getattr(exc, "message", None) or str(exc)
        return BackupOutcome(performed=False, warning=f"Backup check failed: {message}")
    return backup_now(repo, config, now=moment)
