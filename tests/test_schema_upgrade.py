"""Opening a registry file: reading without writing, a read-only file, and
the upgrade of an older schema (real SQLite in tmp_path)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

from compound_registry import __version__, sqlite_repo
from compound_registry.errors import ConfigError, FormatMismatchError, StorageError
from compound_registry.idformat import IdFormat
from compound_registry.models import ExperimentRef, Normalized
from compound_registry.service import RegistryService, upgrade_registry
from compound_registry.sqlite_repo import SqliteRepository

from test_sqlite_repo import StepClock

META_GUARDS = ("meta_fixed_no_replace", "meta_fixed_no_update", "meta_fixed_no_delete")
ID_GUARDS = ("meta_registry_id_no_replace", "meta_registry_id_no_update",
             "meta_registry_id_no_delete")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))


def norm(core: str = "A") -> Normalized:
    return Normalized(core_key=f"InChI=1S/{core}", stereo_key="{}", stereo_mode="abs",
                      inchikey=f"IK-{core}", smiles="C", molblock="M  END\n")


@pytest.fixture
def db(tmp_path) -> Path:
    """A registry file with two IDs, closed."""
    path = tmp_path / "reg" / "registry.sqlite"
    with SqliteRepository(path) as repo:
        repo.register(norm("A"), ExperimentRef("E1"), 1)
        repo.register(norm("B"), ExperimentRef("E2"), 1)
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), isolation_level=None)


def meta(path: Path) -> dict[str, str]:
    with closing(raw(path)) as c:
        return dict(c.execute("SELECT key, value FROM meta").fetchall())


def events(path: Path, kind: str) -> list[dict]:
    with closing(raw(path)) as c:
        rows = c.execute("SELECT details FROM events WHERE event = ? ORDER BY event_id",
                         (kind,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def user_version(path: Path) -> int:
    with closing(raw(path)) as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def set_stored_version(path: Path, version: int) -> None:
    with closing(raw(path)) as c:
        for name in META_GUARDS:
            c.execute(f"DROP TRIGGER {name}")
        c.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(version),))
        for statement in sqlite_repo._META_GUARD_STATEMENTS:
            c.execute(statement)
        c.execute(f"PRAGMA user_version = {version}")


@pytest.fixture
def read_only_file():
    """Makes files read-only and writable again afterwards."""
    done: list[Path] = []

    def make(path: Path) -> Path:
        os.chmod(path, stat.S_IREAD)
        done.append(path)
        return path

    yield make
    for path in done:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


# --------------------------------------------------------------------------- reading does not write

def test_opening_and_reading_leaves_the_file_as_it_is(db):
    before, mtime = digest(db), db.stat().st_mtime_ns
    with SqliteRepository(db) as repo:
        assert repo.read_only is False and repo.upgrade is None
        assert repo.schema_version == 1
        assert repo.get_batch("000001_01_001") is not None
        assert repo.stats()["batches"] == 2
        assert repo.integrity_check() == "ok"
    assert digest(db) == before and db.stat().st_mtime_ns == mtime


def test_reading_through_the_service_leaves_the_file_as_it_is(tmp_path):
    home = tmp_path / "home"
    with RegistryService.open(home) as svc:
        svc.register(smiles="CCO", experiment_ref="E1")
        db_path = svc.config.db_path
    before = digest(db_path)
    with RegistryService.open(home) as svc:
        assert svc.get("000001_01_001")["full_id"] == "000001_01_001"
        assert svc.analyze(smiles="CCO")["status"] == "known_compound"
        doctor = svc.doctor()
        assert doctor["integrity"] == "ok" and doctor["read_only"] is False
    assert digest(db_path) == before


def test_the_numbering_check_still_runs_on_a_read_only_open(db):
    with closing(raw(db)) as c:
        c.execute("DROP TRIGGER batches_no_delete")
        c.execute("DELETE FROM batches WHERE full_id = '000002_01_001'")
        c.execute(next(s for s in sqlite_repo.SCHEMA_STATEMENTS if "batches_no_delete" in s))
    with pytest.raises(StorageError) as info:
        SqliteRepository(db)
    assert info.value.code == "registry_damaged"


def test_a_missing_guard_is_put_back_on_open(db):
    with closing(raw(db)) as c:
        c.execute("DROP TRIGGER batches_no_delete")
        c.execute("DROP INDEX batches_active_slot")
    SqliteRepository(db).close()
    with closing(raw(db)) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
        assert sqlite_repo.SCHEMA_OBJECTS <= names
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM batches")


def test_a_file_without_the_annotation_tables_and_columns_gets_them(db):
    with closing(raw(db)) as c:
        c.execute("DROP TABLE yields")
        c.execute("DROP TABLE spectra")
        c.execute("ALTER TABLE names DROP COLUMN source")
        c.execute("ALTER TABLE reactions DROP COLUMN limiting_row")
        c.execute("ALTER TABLE reactions DROP COLUMN limiting_name")
    with SqliteRepository(db) as repo:
        assert repo.upgrade is None and repo.schema_version == 1
        assert repo.stats()["batches"] == 2
    with closing(raw(db)) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"yields", "spectra"} <= tables
        assert "source" in {r[1] for r in c.execute("PRAGMA table_info(names)")}
        assert {"limiting_row", "limiting_name"} <= {
            r[1] for r in c.execute("PRAGMA table_info(reactions)")}
    assert events(db, "migrate") == []
    before = digest(db)
    SqliteRepository(db).close()
    assert digest(db) == before


def test_a_new_file_has_every_column_in_its_table_definitions(db):
    with closing(raw(db)) as c:
        sql = dict(c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"))
    assert "limiting_row" in sql["reactions"] and "limiting_name" in sql["reactions"]
    assert "source" in sql["names"]
    for table, column, statement in sqlite_repo._ANNOTATION_COLUMNS:
        assert statement.startswith(f"ALTER TABLE {table} ADD COLUMN {column} ")


# --------------------------------------------------------------------------- a read-only file

def test_a_read_only_file_can_be_read_and_refuses_writes(db, read_only_file):
    before = digest(db)
    read_only_file(db)
    with SqliteRepository(db) as repo:
        assert repo.read_only is True
        assert repo.get_batch("000002_01_001").experiment_ref == "E2"
        assert repo.stats()["batches"] == 2
        with pytest.raises(StorageError) as info:
            repo.register(norm("C"), ExperimentRef("E3"), 1)
        assert "cannot be written" in info.value.message
        assert "Nothing was changed" in info.value.message
    assert digest(db) == before


def test_doctor_says_that_the_file_is_read_only(tmp_path, read_only_file):
    home = tmp_path / "home"
    with RegistryService.open(home) as svc:
        svc.register(smiles="CCO", experiment_ref="E1")
        db_path = Path(svc.config.db_path)
    read_only_file(db_path)
    with RegistryService.open(home) as svc:
        assert svc.get("000001_01_001")["full_id"] == "000001_01_001"
        doctor = svc.doctor()
        assert doctor["read_only"] is True and doctor["integrity"] == "ok"
        assert any("cannot be written" in w for w in doctor["warnings"])


def test_a_read_only_file_without_an_identity_is_read_with_the_derived_one(db, read_only_file):
    with closing(raw(db)) as c:
        for name in ID_GUARDS:
            c.execute(f"DROP TRIGGER {name}")
        c.execute("DELETE FROM meta WHERE key = 'registry_id'")
    values = meta(db)
    before = digest(db)
    read_only_file(db)
    with SqliteRepository(db) as repo:
        assert repo.read_only is True
        assert repo.registry_id == sqlite_repo.derived_registry_id(values["created_at"],
                                                                   values["id_format"])
        assert repo.stats()["batches"] == 2
    assert digest(db) == before


def test_a_read_only_file_that_lacks_a_table_is_refused_with_the_reason(db, read_only_file):
    with closing(raw(db)) as c:
        c.execute("DROP TABLE yields")
    read_only_file(db)
    with pytest.raises(StorageError) as info:
        SqliteRepository(db)
    assert "must be brought up to date" in info.value.message
    assert "open a copy" in info.value.message


# --------------------------------------------------------------------------- the ID format of a new file

def test_a_new_file_refuses_a_format_that_cannot_be_a_file_name(tmp_path):
    path = tmp_path / "reg" / "registry.sqlite"
    with pytest.raises(ConfigError) as info:
        SqliteRepository(path, IdFormat("{core:04d}/{stereo:02d}/{batch:03d}"))
    assert "folder and file names" in info.value.message
    if path.exists():                                   # nothing of a registry was written
        with closing(raw(path)) as c:
            assert c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    with SqliteRepository(path) as repo:                # and the default still works there
        assert repo.id_format.template == IdFormat.DEFAULT


def test_an_existing_file_with_such_a_format_still_opens(db):
    odd = "{core:04d}:{stereo:02d}:{batch:03d}"
    with closing(raw(db)) as c:
        for name in META_GUARDS:
            c.execute(f"DROP TRIGGER {name}")
        c.execute("UPDATE meta SET value = ? WHERE key = 'id_format'", (odd,))
        for statement in sqlite_repo._META_GUARD_STATEMENTS:
            c.execute(statement)
    with SqliteRepository(db) as repo:
        assert repo.id_format.template == odd
    with SqliteRepository(db, IdFormat(odd)) as repo:
        assert repo.id_format.template == odd
    with pytest.raises(FormatMismatchError):
        SqliteRepository(db, IdFormat())


# --------------------------------------------------------------------------- schema upgrade

@pytest.fixture
def schema_2(monkeypatch):
    """The program as it would be with a schema 2 that adds a table."""
    calls: list[int] = []

    def step(conn: sqlite3.Connection) -> None:
        calls.append(2)
        conn.execute("CREATE TABLE added_in_2 (x TEXT)")
        # A step that changes a guard replaces that trigger itself.
        conn.execute("DROP TRIGGER events_no_delete")
        conn.execute("""CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'registry: the event log is append-only (2)');
END""")

    monkeypatch.setattr(sqlite_repo, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(sqlite_repo, "_MIGRATIONS", {2: step})
    return calls


def test_a_newer_file_is_refused(db, monkeypatch):
    set_stored_version(db, 2)
    before = digest(db)
    with pytest.raises(StorageError) as info:
        SqliteRepository(db)
    assert "newer program version" in info.value.message
    assert "Update the program" in info.value.message
    assert digest(db) == before


def test_an_older_file_is_copied_and_upgraded_in_order(db, schema_2, tmp_path):
    copies = tmp_path / "copies" / "made-when-needed"
    with SqliteRepository(db, clock=StepClock(), upgrade_backup_dir=copies) as repo:
        assert repo.schema_version == 2
        (copy,) = list(copies.iterdir())
        assert repo.upgrade == {"from": 1, "to": 2, "backup": str(copy)}
        assert repo.stats()["batches"] == 2             # the data is still there
        assert repo.register(norm("C"), ExperimentRef("E3"), 1).batch.full_id == "000003_01_001"
    assert schema_2 == [2]
    assert copy.name.startswith("registry-before-schema-2-") and copy.suffix == ".sqlite"
    assert meta(copy)["schema_version"] == "1"
    values = meta(db)
    assert values["schema_version"] == "2" and user_version(db) == 2
    assert events(db, "migrate") == [{"from": 1, "to": 2, "program_version": __version__,
                                      "backup": str(copy)}]
    with closing(raw(db)) as c:
        assert c.execute("SELECT count(*) FROM added_in_2").fetchone()[0] == 0
        with pytest.raises(sqlite3.DatabaseError, match="are fixed"):
            c.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        with pytest.raises(sqlite3.DatabaseError, match=r"append-only \(2\)"):
            c.execute("DELETE FROM events")

    with SqliteRepository(db, upgrade_backup_dir=copies) as repo:   # once only
        assert repo.upgrade is None and repo.schema_version == 2
    assert schema_2 == [2] and len(list(copies.iterdir())) == 1
    assert len(events(db, "migrate")) == 1


def test_a_failing_step_changes_nothing(db, monkeypatch, tmp_path):
    def step(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE added_in_2 (x TEXT)")
        raise sqlite3.OperationalError("step failed")

    monkeypatch.setattr(sqlite_repo, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(sqlite_repo, "_MIGRATIONS", {2: step})
    before = digest(db)
    with pytest.raises(StorageError) as info:
        SqliteRepository(db, upgrade_backup_dir=tmp_path / "copies")
    assert "step failed" in info.value.message
    assert digest(db) == before
    assert meta(db)["schema_version"] == "1" and events(db, "migrate") == []
    monkeypatch.undo()
    with SqliteRepository(db) as repo:                  # the old program still opens it
        assert repo.stats()["batches"] == 2


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyError("boom"),
                                   sqlite3.OperationalError("boom")])
def test_whatever_a_step_raises_is_a_storage_error_and_no_copy_is_left(db, monkeypatch,
                                                                       tmp_path, error):
    def step(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE added_in_2 (x TEXT)")
        conn.execute("DELETE FROM names")
        raise error

    monkeypatch.setattr(sqlite_repo, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(sqlite_repo, "_MIGRATIONS", {2: step})
    before = digest(db)
    copies = tmp_path / "copies"
    for _ in range(3):                                  # every attempt, e.g. every host message
        with pytest.raises(StorageError) as info:
            SqliteRepository(db, upgrade_backup_dir=copies)
        assert "could not be upgraded to schema 2" in info.value.message
        assert "boom" in info.value.message and "Nothing was changed" in info.value.message
        assert digest(db) == before
        assert list(copies.iterdir()) == []


def test_a_failure_after_the_steps_leaves_no_copy_and_reports_no_upgrade(db, schema_2,
                                                                         monkeypatch, tmp_path):
    def fail(self, conn):
        raise StorageError("numbering check failed")

    monkeypatch.setattr(SqliteRepository, "_check_consistency", fail)
    before = digest(db)
    copies = tmp_path / "copies"
    with pytest.raises(StorageError, match="numbering check failed"):
        SqliteRepository(db, upgrade_backup_dir=copies)
    assert digest(db) == before and list(copies.iterdir()) == []


def test_a_read_only_file_that_needs_an_upgrade_is_refused_before_any_copy(db, schema_2,
                                                                           tmp_path,
                                                                           read_only_file):
    read_only_file(db)
    before = digest(db)
    copies = tmp_path / "copies"
    with pytest.raises(StorageError) as info:
        SqliteRepository(db, upgrade_backup_dir=copies)
    assert "cannot be written" in info.value.message
    assert schema_2 == [] and digest(db) == before and not copies.exists()


def test_without_a_copy_nothing_is_upgraded(db, schema_2, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    before = digest(db)
    with pytest.raises(StorageError) as info:
        SqliteRepository(db, upgrade_backup_dir=blocker / "copies")
    assert "Nothing was changed" in info.value.message
    assert schema_2 == [] and digest(db) == before


def test_a_version_without_a_step_is_refused(db, monkeypatch, tmp_path):
    monkeypatch.setattr(sqlite_repo, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(sqlite_repo, "_MIGRATIONS", {3: lambda conn: None})    # no step 2
    before = digest(db)
    copies = tmp_path / "copies"
    with pytest.raises(StorageError) as info:
        SqliteRepository(db, upgrade_backup_dir=copies)
    assert "cannot bring up to date" in info.value.message
    assert digest(db) == before and not copies.exists()


def test_a_file_that_claims_an_unknown_older_version_is_not_opened_silently(db):
    set_stored_version(db, 0)
    before = digest(db)
    with pytest.raises(StorageError) as info:
        SqliteRepository(db)
    assert "schema version 0" in info.value.message
    assert digest(db) == before


def test_the_steps_cover_every_version():
    assert sorted(sqlite_repo._MIGRATIONS) == list(range(2, sqlite_repo.SCHEMA_VERSION + 1))


def test_the_service_copies_into_the_backup_folder_and_reports_the_upgrade(tmp_path, schema_2,
                                                                          monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(sqlite_repo, "SCHEMA_VERSION", 1)
    with RegistryService.open(home) as svc:
        svc.register(smiles="CCO", experiment_ref="E1")
        config = svc.config
    monkeypatch.setattr(sqlite_repo, "SCHEMA_VERSION", 2)
    result = upgrade_registry(home)
    assert result["status"] == "current" and result["schema_version"] == 2
    upgrade = result["schema_upgrade"]
    assert (upgrade["from"], upgrade["to"]) == (1, 2)
    assert Path(upgrade["backup"]).parent == Path(config.backup_dir)
    assert Path(upgrade["backup"]).is_file()
    assert "upgraded from schema 1 to 2" in result["message"]
    assert upgrade["backup"] in result["message"]
    again = upgrade_registry(home)
    assert again["schema_upgrade"] is None and "upgraded" not in again["message"]
    with RegistryService.open(home) as svc:
        assert svc.doctor()["schema_version"] == 2
        assert svc.register(smiles="CC(=O)O", experiment_ref="E2")["id"] == "000002_01_001"
        # The copy is not one of the regular backups.
        assert upgrade["backup"] not in [b["path"] for b in svc.list_backups()]
