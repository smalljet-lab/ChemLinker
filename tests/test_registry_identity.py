"""Every registry has an identity: journal lines and backups of two
registries that meet in one folder are told apart (real SQLite in tmp_path)."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from compound_registry import backup as backup_mod
from compound_registry import sqlite_repo
from compound_registry.backup import (
    backup_now,
    backup_registry_id,
    list_backups,
    newest_backup,
    registry_id_from_backups,
    verify_backup,
)
from compound_registry.config import load_config
from compound_registry.errors import RegistryError
from compound_registry.journal import (
    JOURNAL_FILENAME,
    UNNAMED_REGISTRY,
    journal_origin,
    journal_registry_id,
    read_journal,
    read_journals,
    register_entry,
    retire_entry,
    stamped,
)
from compound_registry.models import ExperimentRef, Normalized
from compound_registry.service import RegistryService, create_registry, upgrade_registry
from compound_registry.sqlite_repo import (
    REGISTRY_ID_KEY,
    UNSTAMPED_JOURNAL_KEY,
    SqliteRepository,
    derived_registry_id,
    read_registry_identity,
)

ETHANOL = "CCO"
ACETIC_ACID = "CC(=O)O"
BENZENE = "c1ccccc1"
T0 = datetime(2026, 11, 17, 8, 30, 0, tzinfo=timezone.utc)
HEX32 = re.compile(r"^[0-9a-f]{32}$")
ID_GUARDS = ("meta_registry_id_no_replace", "meta_registry_id_no_update",
             "meta_registry_id_no_delete")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


def norm(core: str = "A") -> Normalized:
    return Normalized(core_key=f"InChI=1S/{core}", stereo_key="{}", stereo_mode="abs",
                      inchikey=f"IK-{core}", smiles="C", molblock="M  END\n")


def write_config(home: Path, *, backup: Path | None = None, database: Path | None = None,
                 keep: int | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    lines = []
    if database is not None:
        lines += ["[storage]", f'database = "{database.as_posix()}"']
    if backup is not None or keep is not None:
        lines.append("[backup]")
        if backup is not None:
            lines.append(f'folder = "{backup.as_posix()}"')
        if keep is not None:
            lines.append(f"keep = {keep}")
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def journal_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def meta_value(db: Path, key: str) -> str | None:
    with closing(sqlite3.connect(str(db))) as c:
        row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def events(db: Path, kind: str) -> list[dict]:
    with closing(sqlite3.connect(str(db))) as c:
        rows = c.execute("SELECT details FROM events WHERE event = ? ORDER BY event_id",
                         (kind,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def batch_count(db: Path) -> int:
    with closing(sqlite3.connect(str(db))) as c:
        return c.execute("SELECT count(*) FROM batches").fetchone()[0]


def make_legacy(home: Path) -> None:
    """Turn the registry in ``home`` into one written before registries had
    an identity: no ``registry_id`` in the database, none in the journals."""
    config = load_config(home, create=False)
    with closing(sqlite3.connect(str(config.db_path), isolation_level=None)) as c:
        for name in ID_GUARDS:
            c.execute(f"DROP TRIGGER {name}")
        c.execute("DELETE FROM meta WHERE key = ?", (REGISTRY_ID_KEY,))
    for path in (home / JOURNAL_FILENAME, Path(config.backup_dir) / JOURNAL_FILENAME):
        if path.is_file():
            stripped = [{k: v for k, v in line.items() if k != "registry_id"}
                        for line in journal_lines(path)]
            path.write_text("".join(json.dumps(line) + "\n" for line in stripped),
                            encoding="utf-8")


# --------------------------------------------------------------------------- the identity itself

def test_a_new_registry_gets_a_random_permanent_identity(tmp_path):
    with RegistryService.open(tmp_path / "a") as a, RegistryService.open(tmp_path / "b") as b:
        id_a, id_b = a.repo.registry_id, b.repo.registry_id
        assert HEX32.match(id_a) and HEX32.match(id_b) and id_a != id_b
        assert a.repo.get_meta(REGISTRY_ID_KEY) == id_a
        assert a.doctor()["registry_id"] == id_a
        with pytest.raises(RegistryError) as info:
            a.repo.set_meta(REGISTRY_ID_KEY, "0" * 32)
        assert info.value.code == "invalid_argument"
        with pytest.raises(RegistryError):
            a.repo.record_event("x", meta={REGISTRY_ID_KEY: "0" * 32})
        db = a.config.db_path
    (created,) = events(db, "create")
    assert created["registry_id"] == id_a
    with closing(sqlite3.connect(str(db), isolation_level=None)) as c:
        for statement in ("UPDATE meta SET value = 'x' WHERE key = 'registry_id'",
                          "DELETE FROM meta WHERE key = 'registry_id'",
                          "INSERT OR REPLACE INTO meta(key, value) VALUES ('registry_id', 'x')"):
            with pytest.raises(sqlite3.DatabaseError, match="registry_id is fixed"):
                c.execute(statement)
    with RegistryService.open(tmp_path / "a") as again:
        assert again.repo.registry_id == id_a


def test_journal_lines_and_the_backup_event_name_the_registry(tmp_path):
    home = tmp_path / "home"
    with RegistryService.open(home) as svc:
        rid = svc.repo.registry_id
        first = svc.register(smiles=ETHANOL, experiment_ref="E1")
        svc.retire(first["id"], "test")
        assert svc.backup()["performed"]
        config = svc.config
    for path in (home / JOURNAL_FILENAME, Path(config.backup_dir) / JOURNAL_FILENAME):
        lines = journal_lines(path)
        assert [line["event"] for line in lines] == ["register", "retire"]
        assert all(line["registry_id"] == rid for line in lines)
    assert all(e["registry_id"] == rid for e in events(config.db_path, "backup"))
    for path in list_backups(config):
        assert backup_registry_id(path) == rid


def test_entries_without_an_identity_are_written_without_the_key():
    entry = register_entry("000001_01_001", "2026-11-17T10:00:00Z", experiment_ref="E1",
                           experiment_code=None, experiment_url=None, product_index=1,
                           source="cli", norm=norm())
    assert "registry_id" not in entry
    assert "registry_id" not in retire_entry("000001_01_001", "2026-11-17T10:00:00Z", "x")
    assert retire_entry("000001_01_001", "2026-11-17T10:00:00Z", "x", "abc")["registry_id"] == "abc"


def test_read_journal_separates_the_lines_of_other_registries(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    lines = [
        {"event": "retire", "id": "000001_01_001", "at": "t", "reason": "r"},                 # old line
        {"event": "retire", "id": "000002_01_001", "at": "t", "reason": "r", "registry_id": "mine"},
        {"event": "retire", "id": "000002_01_001", "at": "t", "reason": "r", "registry_id": "other"},
        {"event": "retire", "id": "000003_01_001", "at": "t", "reason": "r", "registry_id": "other"},
        {"event": "retire", "id": "000004_01_001", "at": "t", "reason": "r", "registry_id": "third"},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    mine = read_journal(path, "mine")
    assert [e["id"] for e in mine.entries] == ["000001_01_001", "000002_01_001"]
    assert mine.foreign == {"other": 2, "third": 1} and mine.unreadable_lines == 0
    everything = read_journal(path)
    assert len(everything.entries) == 4 and everything.foreign == {}   # (event, id) counts once


# --------------------------------------------------------------------------- a registry from before

def test_a_registry_without_an_identity_gets_one_on_open_and_keeps_its_journal(tmp_path):
    home = tmp_path / "home"
    with RegistryService.open(home) as svc:
        svc.register(smiles=ETHANOL, experiment_ref="E1")
        svc.register(smiles=ACETIC_ACID, experiment_ref="E2")
        assert svc.backup()["performed"]
        config = svc.config
    make_legacy(home)
    old_backup = list_backups(config)[0]
    shutil.copy2(config.db_path, old_backup)            # a backup written before, too
    assert meta_value(config.db_path, REGISTRY_ID_KEY) is None
    expected = derived_registry_id(meta_value(config.db_path, "created_at"),
                                   meta_value(config.db_path, "id_format"))

    with RegistryService.open(home) as svc:
        assert svc.repo.registry_id == expected and HEX32.match(expected)
        assert svc.repo.read_only is False
        # Its journal lines carry no identity: they are its own.
        status = svc.journal_status()
        assert status["other_registries"] == [] and status["missing_in_database"] == []
        third = svc.register(smiles=BENZENE, experiment_ref="E3")
        assert third["id"] == "000003_01_001"
        # The backup made before has the same identity: it is listed and counted.
        assert backup_registry_id(old_backup) == expected
        assert old_backup in [Path(b["path"]) for b in svc.list_backups()]
    assert meta_value(config.db_path, REGISTRY_ID_KEY) == expected
    assert events(config.db_path, "registry_id_set") == [{"registry_id": expected}]
    # The open that gave the file its identity also made the journal name
    # it: a copy of each old line with the identity, then the new line.
    for path in (home / JOURNAL_FILENAME, Path(config.backup_dir) / JOURNAL_FILENAME):
        lines = journal_lines(path)
        assert ["registry_id" in line for line in lines] == [False, False, True, True, True]
        assert lines[2:4] == [stamped(line, expected) for line in lines[:2]]
        assert lines[-1]["registry_id"] == expected and lines[-1]["id"] == "000003_01_001"
    assert journal_registry_id(config) == expected
    assert meta_value(config.db_path, UNSTAMPED_JOURNAL_KEY) == "stamped"

    with RegistryService.open(home) as svc:             # nothing more happens later
        assert svc.repo.registry_id == expected
        assert svc.repo.unstamped_journal_own is False
        assert svc.register(smiles="CCCC", experiment_ref="E4")["created"]
    assert len(events(config.db_path, "registry_id_set")) == 1
    assert len(journal_lines(home / JOURNAL_FILENAME)) == 6
    with closing(sqlite3.connect(str(config.db_path), isolation_level=None)) as c:
        with pytest.raises(sqlite3.DatabaseError, match="registry_id is fixed"):
            c.execute("UPDATE meta SET value = 'x' WHERE key = 'registry_id'")


def test_an_old_backup_restored_over_the_database_is_still_the_same_registry(tmp_path):
    home = tmp_path / "home"
    with RegistryService.open(home) as svc:
        svc.register(smiles=ETHANOL, experiment_ref="E1")
        config = svc.config
    make_legacy(home)
    old_copy = tmp_path / "old-copy.sqlite"
    shutil.copy2(config.db_path, old_copy)
    with RegistryService.open(home) as svc:
        rid = svc.repo.registry_id
        svc.register(smiles=ACETIC_ACID, experiment_ref="E2")
    shutil.copy2(old_copy, config.db_path)              # the restore
    with RegistryService.open(home) as svc:
        assert svc.repo.registry_id == rid
        with pytest.raises(RegistryError) as info:
            svc.register(smiles=BENZENE, experiment_ref="E3")
        assert info.value.code == "database_older_than_journal"
        assert svc.recover()["recovered"] == ["000002_01_001"]
        assert svc.register(smiles=BENZENE, experiment_ref="E3")["id"] == "000003_01_001"


def test_read_registry_identity_of_other_files(tmp_path):
    other = tmp_path / "other.sqlite"
    with closing(sqlite3.connect(str(other))) as c:
        c.execute("CREATE TABLE t (x)")
        c.commit()
        assert read_registry_identity(c) is None
    assert backup_registry_id(other) is None
    assert backup_registry_id(tmp_path / "missing.sqlite") is None
    (tmp_path / "text.sqlite").write_text("not a database", encoding="utf-8")
    assert backup_registry_id(tmp_path / "text.sqlite") is None


# --------------------------------------------------------------------------- one backup folder, two registries

@pytest.fixture
def shared(tmp_path):
    """Registry A with two IDs, and a new registry B with its own home whose
    [backup] folder is A's."""
    backups = tmp_path / "group-share" / "backups"
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    write_config(home_a, backup=backups)
    write_config(home_b, backup=backups)
    with RegistryService.open(home_a) as a:
        a.register(smiles=ETHANOL, experiment_ref="E1")
        a.register(smiles=ACETIC_ACID, experiment_ref="E2")
        id_a = a.repo.registry_id
    return home_a, home_b, backups, id_a


def test_a_shared_backup_folder_blocks_writing_and_never_offers_recover(shared):
    home_a, home_b, backups, id_a = shared
    mirror_before = (backups / JOURNAL_FILENAME).read_bytes()
    with RegistryService.open(home_b) as b:
        id_b = b.repo.registry_id
        assert id_b != id_a
        assert b.analyze(smiles=ETHANOL)["status"] == "new"       # reading works
        for write in (lambda: b.register(smiles=BENZENE, experiment_ref="E9"),
                      lambda: b.retire("000001_01_001", "x"),
                      b.check_writable,
                      b.recover):
            with pytest.raises(RegistryError) as info:
                write()
            assert info.value.code == "journal_shared"
        message = info.value.message
        assert "Give each registry its own backup folder" in message
        assert str(backups) in message and id_a in message and id_b in message
        assert "Do not run 'compound-registry recover'" in message
        status = b.journal_status()
        assert status["other_registries"] == [id_a]
        assert status["missing_in_database"] == []                # nothing is "lost"
        assert b.recover_plan()["to_register"] == []
        doctor = b.doctor()
        assert any("own backup folder" in w for w in doctor["warnings"])
        assert doctor["journal"]["other_registries"] == [id_a]
        db_b = b.config.db_path
    assert batch_count(db_b) == 0
    assert (backups / JOURNAL_FILENAME).read_bytes() == mirror_before
    # A is blocked as soon as B's lines are there, not before.
    with RegistryService.open(home_a) as a:
        assert a.register(smiles=BENZENE, experiment_ref="E3")["id"] == "000003_01_001"


def test_a_backup_folder_of_its_own_ends_the_block(shared, tmp_path):
    home_a, home_b, backups, id_a = shared
    write_config(home_b, backup=tmp_path / "b-backups")
    with RegistryService.open(home_b) as b:
        assert b.register(smiles=BENZENE, experiment_ref="E9")["id"] == "000001_01_001"
    with RegistryService.open(home_a) as a:
        assert a.repo.stats()["batches"] == 2
        assert a.journal_status()["other_registries"] == []


def test_two_registries_that_both_wrote_into_one_mirror_are_both_blocked(tmp_path):
    backups = tmp_path / "backups"
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    with RegistryService.open(home_a) as a, RegistryService.open(home_b) as b:
        a.register(smiles=ETHANOL, experiment_ref="E1")
        b.register(smiles=ETHANOL, experiment_ref="E1")           # the same ID in both
        b.register(smiles=ACETIC_ACID, experiment_ref="E2")
    write_config(home_a, backup=backups)
    write_config(home_b, backup=backups)
    with RegistryService.open(home_a) as a:
        a.register(smiles=BENZENE, experiment_ref="E3")           # catches the mirror up with A
    with RegistryService.open(home_b) as b:
        with pytest.raises(RegistryError) as info:
            b.register(smiles=BENZENE, experiment_ref="E3")
        assert info.value.code == "journal_shared"
        assert b.repo.stats()["batches"] == 2


def test_a_database_of_another_registry_in_the_home_folder_is_blocked(tmp_path):
    home_x, home_y = tmp_path / "x", tmp_path / "y"
    with RegistryService.open(home_x) as x, RegistryService.open(home_y) as y:
        x.register(smiles=ETHANOL, experiment_ref="E1")
        y.register(smiles=ACETIC_ACID, experiment_ref="E1")
        id_x, db_y = x.repo.registry_id, y.config.db_path
    # x's config.toml now names y's database: two registries, one home journal.
    write_config(home_x, database=db_y, backup=tmp_path / "x2-backups")
    with RegistryService.open(home_x) as svc:
        with pytest.raises(RegistryError) as info:
            svc.register(smiles=BENZENE, experiment_ref="E2")
        assert info.value.code == "journal_shared"
        assert "its own home folder" in info.value.message and id_x in info.value.message
        with pytest.raises(RegistryError) as info:
            svc.recover()
        assert info.value.code == "journal_shared"
    assert batch_count(db_y) == 1


# --------------------------------------------------------------------------- a database made anew

def test_a_database_made_anew_continues_the_registry_of_the_home_journal(tmp_path):
    home = tmp_path / "home"
    with RegistryService.open(home) as svc:
        svc.register(smiles=ETHANOL, experiment_ref="E1")
        rid, config = svc.repo.registry_id, svc.config
    Path(config.db_path).unlink()
    assert journal_registry_id(config) == rid
    with RegistryService.open(home) as svc:
        assert svc.repo.registry_id == rid
        with pytest.raises(RegistryError) as info:
            svc.register(smiles=ACETIC_ACID, experiment_ref="E2")
        assert info.value.code == "database_older_than_journal"
        assert svc.recover()["recovered"] == ["000001_01_001"]
        assert svc.register(smiles=ACETIC_ACID, experiment_ref="E2")["id"] == "000002_01_001"


def test_a_new_registry_never_takes_its_identity_from_the_backup_folder(shared):
    home_a, home_b, backups, id_a = shared
    config_b = load_config(home_b, create=False)
    assert journal_registry_id(config_b) is None
    assert journal_registry_id(load_config(home_a, create=False)) == id_a


def test_journal_registry_id_needs_one_single_identity(tmp_path):
    config = load_config(tmp_path / "home", create=False)
    assert journal_registry_id(config) is None                    # no journal
    path = Path(config.home) / JOURNAL_FILENAME
    path.parent.mkdir(parents=True)
    lines = [{"event": "retire", "id": "000001_01_001", "at": "t", "reason": "r"}]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    assert journal_registry_id(config) is None                    # lines without identity
    lines.append({"event": "retire", "id": "000002_01_001", "at": "t", "reason": "r",
                  "registry_id": "one"})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    assert journal_registry_id(config) == "one"
    lines.append({"event": "retire", "id": "000003_01_001", "at": "t", "reason": "r",
                  "registry_id": "two"})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    assert journal_registry_id(config) is None


# --------------------------------------------------------------------------- journal lines that name no registry

def legacy_registry(home: Path, backup: Path | None = None, backups: int = 1) -> tuple:
    """A registry as an older program version left it (two IDs, one of them
    retired, ``backups`` backups; no identity in the database, the journals
    or the backups). Returns ``(config, the identity it will get)``."""
    if backup is not None:
        write_config(home, backup=backup)
    with RegistryService.open(home) as svc:
        svc.register(smiles=ETHANOL, experiment_ref="E1")
        svc.register(smiles=ACETIC_ACID, experiment_ref="E2")
        svc.retire("000002_01_001", "wrong")
        config = svc.config
    make_legacy(home)
    for path in list_backups(config):                   # written with an identity
        path.unlink()
    for number in range(backups):
        shutil.copy2(config.db_path,
                     Path(config.backup_dir) / f"registry-2026111{number}-080000.sqlite")
    return config, derived_registry_id(meta_value(config.db_path, "created_at"),
                                       meta_value(config.db_path, "id_format"))


def test_read_journals_takes_unnamed_lines_only_for_a_reader_that_owns_them(tmp_path):
    def line(number: int) -> dict:
        return {"event": "retire", "id": f"00000{number}_01_001", "at": "t", "reason": "r"}

    main, mirror = tmp_path / "main.jsonl", tmp_path / "mirror.jsonl"
    main.write_text("".join(json.dumps(x) + "\n" for x in (
        line(1), line(2), stamped(line(2), "me"), stamped(line(3), "me"))), encoding="utf-8")
    mirror.write_text("".join(json.dumps(x) + "\n" for x in (
        line(1), line(2), line(4), stamped(line(5), "other"))), encoding="utf-8")

    own_main, own_mirror = read_journals([main, mirror], "me", unstamped_own=True)
    assert [e["id"][:6] for e in own_main.entries] == ["000001", "000002", "000003"]
    assert own_main.unstamped == [line(1)] and own_main.foreign == {}
    assert [e["id"][:6] for e in own_mirror.entries] == ["000001", "000002", "000004"]
    assert own_mirror.unstamped == [line(1), line(2), line(4)]
    assert own_mirror.foreign == {"other": 1}

    not_main, not_mirror = read_journals([main, mirror], "me", unstamped_own=False)
    # Line 2 has a copy that names the reader (in the main journal): it is
    # the reader's in both files. Lines 1 and 4 name nobody: foreign.
    assert [e["id"][:6] for e in not_main.entries] == ["000002", "000003"]
    assert not_main.foreign == {UNNAMED_REGISTRY: 1} and not_main.unstamped == []
    assert [e["id"][:6] for e in not_mirror.entries] == ["000002"]
    assert not_mirror.foreign == {UNNAMED_REGISTRY: 2, "other": 1}
    assert not_mirror.unstamped == [line(2)]
    # A line that says something else is not a copy, whatever its ID.
    main.write_text(json.dumps(line(1)) + "\n"
                    + json.dumps(stamped({**line(1), "at": "later"}, "me")) + "\n",
                    encoding="utf-8")
    assert read_journal(main, "me", unstamped_own=False).foreign == {UNNAMED_REGISTRY: 1}
    # Without a reader every line is an entry.
    assert len(read_journal(mirror).entries) == 4


def test_journal_origin_tells_the_lines_that_name_no_registry(tmp_path):
    config = load_config(tmp_path / "home", create=False)
    assert journal_origin(config).registry_id is None and journal_origin(config).unstamped == []
    path = Path(config.home) / JOURNAL_FILENAME
    path.parent.mkdir(parents=True)
    one = {"event": "retire", "id": "000001_01_001", "at": "t", "reason": "r"}
    two = {"event": "retire", "id": "000002_01_001", "at": "t", "reason": "r"}
    path.write_text("".join(json.dumps(x) + "\n" for x in (one, two, stamped(one, "me"))),
                    encoding="utf-8")
    origin = journal_origin(config)
    assert origin.registry_id == "me" and origin.unstamped == [two]


def test_a_new_registry_never_takes_the_journal_of_an_older_registry_for_lost_ids(tmp_path):
    backups = tmp_path / "group-share" / "backups"
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    config_a, id_a = legacy_registry(home_a, backups)   # never opened by this program
    write_config(home_b, backup=backups)
    create_registry(home_b)
    mirror_before = (backups / JOURNAL_FILENAME).read_bytes()
    files_before = sorted(p.name for p in backups.iterdir())
    with RegistryService.open(home_b) as b:
        id_b = b.repo.registry_id
        assert id_b != id_a and b.repo.unstamped_journal_own is False
        plan = b.recover_plan()
        assert plan["to_register"] == [] and plan["to_retire"] == []
        assert plan["other_registries"] == [UNNAMED_REGISTRY]
        doctor = b.doctor()
        assert doctor["journal"]["missing_in_database"] == []
        assert any("own backup folder" in w and "older program version" in w
                   for w in doctor["warnings"])
        assert not any("compound-registry recover' to" in w for w in doctor["warnings"])
        for call in (lambda: b.register(smiles=BENZENE, experiment_ref="E9"), b.recover):
            with pytest.raises(RegistryError) as info:
                call()
            assert info.value.code == "journal_shared"
            assert "older program version" in info.value.message
    assert batch_count(load_config(home_b, create=False).db_path) == 0
    assert (backups / JOURNAL_FILENAME).read_bytes() == mirror_before
    assert sorted(p.name for p in backups.iterdir()) == files_before

    # The older registry is not disturbed: it gets its identity and goes on.
    with RegistryService.open(home_a) as a:
        assert a.repo.registry_id == id_a
        assert a.journal_status()["other_registries"] == []
        assert a.register(smiles=BENZENE, experiment_ref="E3")["id"] == "000003_01_001"
    with RegistryService.open(home_b) as b:             # and B still does not take its lines
        assert b.journal_status()["other_registries"] == sorted([id_a, UNNAMED_REGISTRY])
        assert b.recover_plan()["to_register"] == []


def test_once_its_journal_names_it_a_registry_takes_no_unnamed_line_for_its_own(tmp_path):
    home = tmp_path / "home"
    config, rid = legacy_registry(home)
    result = upgrade_registry(home)                     # what an installation does
    assert result["registry_id"] == rid
    assert meta_value(config.db_path, UNSTAMPED_JOURNAL_KEY) == "stamped"
    mirror = Path(config.backup_dir) / JOURNAL_FILENAME
    stranger = {"event": "retire", "id": "000001_01_001", "at": "2020-01-01T00:00:00Z",
                "reason": "of another registry"}
    with open(mirror, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(stranger) + "\n")
    with RegistryService.open(home) as svc:
        status = svc.journal_status()
        assert status["other_registries"] == [UNNAMED_REGISTRY]
        assert status["retirements_missing_in_database"] == []
        with pytest.raises(RegistryError) as info:
            svc.register(smiles=BENZENE, experiment_ref="E3")
        assert info.value.code == "journal_shared"


def test_the_journal_is_not_called_stamped_while_a_file_could_not_be_written(tmp_path):
    home = tmp_path / "home"
    config, rid = legacy_registry(home, backups=0)
    mirror = Path(config.backup_dir) / JOURNAL_FILENAME
    kept = mirror.read_bytes()
    mirror.unlink()
    mirror.mkdir()                                      # cannot be read or appended to
    with RegistryService.open(home) as svc:
        assert svc.repo.registry_id == rid and svc.repo.unstamped_journal_own is True
    assert meta_value(config.db_path, UNSTAMPED_JOURNAL_KEY) is None
    assert journal_registry_id(config) == rid           # the main journal is stamped
    mirror.rmdir()
    mirror.write_bytes(kept)
    with RegistryService.open(home) as svc:
        assert svc.journal_status()["other_registries"] == []
        assert svc.stamp_journal() is True
        assert svc.repo.unstamped_journal_own is False
    assert ["registry_id" in line for line in journal_lines(mirror)] == [False] * 3 + [True] * 3


@pytest.mark.parametrize("opened_before_the_loss", [True, False])
def test_a_lost_database_of_an_older_registry_is_made_anew_with_its_identity(
        tmp_path, opened_before_the_loss):
    home = tmp_path / "home"
    config, rid = legacy_registry(home, backups=2)
    old_backups = list_backups(config)
    if opened_before_the_loss:
        with RegistryService.open(home) as svc:         # no registration: reading only
            assert svc.find_by_id("000001_01_001")
        assert journal_registry_id(config) == rid
    else:
        assert journal_registry_id(config) is None and len(journal_origin(config).unstamped) == 3
    Path(config.db_path).unlink()

    _, warnings = create_registry(home)
    assert any("compound-registry recover" in w for w in warnings)
    with RegistryService.open(home) as svc:
        assert svc.repo.registry_id == rid
        assert [Path(b["path"]) for b in svc.list_backups()] == old_backups
        assert not any("No backup exists yet" in w for w in svc.doctor()["warnings"])
        result = svc.recover()
        assert result["recovered"] == ["000001_01_001", "000002_01_001"]
        assert result["retired"] == ["000002_01_001"]
        assert svc.register(smiles=BENZENE, experiment_ref="E3")["id"] == "000003_01_001"
        assert svc.repo.unstamped_journal_own is False
    lines = journal_lines(home / JOURNAL_FILENAME)
    assert {line.get("registry_id") for line in lines} == {None, rid}
    assert [stamped(line, rid) in lines for line in lines[:3]] == [True] * 3

    shutil.copy2(old_backups[-1], config.db_path)       # later: an old backup is restored
    with RegistryService.open(home) as svc:
        assert svc.repo.registry_id == rid
        assert svc.journal_status()["other_registries"] == []
        with pytest.raises(RegistryError) as info:
            svc.register(smiles="CCCC", experiment_ref="E4")
        assert info.value.code == "database_older_than_journal"
        assert svc.recover()["recovered"] == ["000003_01_001"]
        assert svc.register(smiles="CCCC", experiment_ref="E4")["id"] == "000004_01_001"


def test_a_lost_database_without_any_backup_still_recovers_its_unnamed_journal(tmp_path):
    home = tmp_path / "home"
    config, rid = legacy_registry(home, backups=0)
    Path(config.db_path).unlink()
    create_registry(home)
    assert meta_value(config.db_path, UNSTAMPED_JOURNAL_KEY) == "own"
    with RegistryService.open(home) as svc:
        new_id = svc.repo.registry_id
        assert HEX32.match(new_id) and new_id != rid
        assert svc.repo.unstamped_journal_own is True
        assert svc.recover()["recovered"] == ["000001_01_001", "000002_01_001"]
        assert svc.repo.unstamped_journal_own is False
    assert meta_value(config.db_path, UNSTAMPED_JOURNAL_KEY) == "stamped"
    assert journal_registry_id(config) == new_id


def test_the_identity_is_only_taken_from_a_backup_of_the_same_ids(tmp_path):
    home = tmp_path / "home"
    config, rid = legacy_registry(home, backups=1)
    registrations = {
        line["id"]: (line["at"], line["experiment_ref"], line["product_index"], line["inchikey"])
        for line in journal_lines(home / JOURNAL_FILENAME) if line["event"] == "register"}
    assert registry_id_from_backups(config, registrations) == rid
    assert registry_id_from_backups(config, {}) is None
    first = dict(list(registrations.items())[:1])
    assert registry_id_from_backups(config, first) is None      # the backup holds more
    changed = {key: (value[0], "another experiment") + value[2:]
               for key, value in registrations.items()}
    assert registry_id_from_backups(config, changed) is None
    # A newer file that is no registry, and an empty registry, give nothing.
    folder = Path(config.backup_dir)
    (folder / "registry-20270101-000000.sqlite").write_text("not a database", encoding="utf-8")
    with SqliteRepository(folder / "registry-20270102-000000.sqlite"):
        pass
    assert registry_id_from_backups(config, registrations) == rid


# --------------------------------------------------------------------------- backups in one folder

@pytest.fixture
def two_repos(tmp_path):
    folder = tmp_path / "backups"
    config_a = replace(load_config(tmp_path / "a"), backup_dir=folder, backup_keep=2)
    config_b = replace(load_config(tmp_path / "b"), backup_dir=folder, backup_keep=2)
    repo_a, repo_b = SqliteRepository(config_a.db_path), SqliteRepository(config_b.db_path)
    repo_a.register(norm("A"), ExperimentRef("E1"), 1)
    repo_b.register(norm("B"), ExperimentRef("E1"), 1)
    yield (repo_a, config_a), (repo_b, config_b)
    repo_a.close()
    repo_b.close()


def test_backups_of_the_same_second_do_not_replace_each_other(two_repos):
    (repo_a, config_a), (repo_b, config_b) = two_repos
    first = backup_now(repo_a, config_a, now=T0)
    second = backup_now(repo_b, config_b, now=T0)
    third = backup_now(repo_a, config_a, now=T0)
    assert first.performed and second.performed and third.performed
    assert [o.path.name for o in (first, second, third)] == [
        "registry-20261117-083000.sqlite", "registry-20261117-083000-2.sqlite",
        "registry-20261117-083000-3.sqlite"]
    assert backup_registry_id(first.path) == repo_a.registry_id
    assert backup_registry_id(second.path) == repo_b.registry_id
    assert all(verify_backup(o.path) == "ok" for o in (first, second, third))
    assert list_backups(config_a) == [first.path, second.path, third.path]
    assert list_backups(config_a, repo_a.registry_id) == [first.path, third.path]
    assert list_backups(config_b, repo_b.registry_id) == [second.path]
    assert not [p for p in config_a.backup_dir.iterdir() if p.name.endswith(".partial")]


def test_retention_only_counts_and_deletes_the_registrys_own_backups(two_repos):
    (repo_a, config_a), (repo_b, config_b) = two_repos
    moments = [T0.replace(hour=h) for h in (1, 2, 3, 4, 5, 6)]
    of_b = [backup_now(repo_b, config_b, now=m).path for m in moments[:2]]
    of_a = []
    for moment in moments[2:]:
        outcome = backup_now(repo_a, config_a, now=moment)
        assert outcome.performed and outcome.warning is None
        of_a.append(outcome.path)
        assert all(p.is_file() for p in of_b)                     # never B's
    assert outcome.removed == (of_a[1],)
    assert list_backups(config_a, repo_a.registry_id) == of_a[2:]  # keep = 2
    assert list_backups(config_b, repo_b.registry_id) == of_b
    foreign = config_a.backup_dir / "registry-20200101-000000.sqlite"
    foreign.write_text("not a database", encoding="utf-8")
    assert backup_now(repo_a, config_a, now=T0.replace(hour=7)).performed
    assert foreign.is_file()                                      # unreadable: left alone


def test_doctor_does_not_open_every_backup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    with RegistryService.open(home) as svc:
        svc.register(smiles=ETHANOL, experiment_ref="E1")
        config, rid = svc.config, svc.repo.registry_id
    source = Path(config.db_path)
    for day in range(1, 21):
        shutil.copy2(source, Path(config.backup_dir) / f"registry-202601{day:02d}-080000.sqlite")
    newest = list_backups(config)[-1]
    assert newest_backup(config) == newest_backup(config, rid) == newest
    assert newest_backup(config, "another registry") is None
    opened: list[Path] = []
    real = backup_mod.backup_registry_id
    monkeypatch.setattr(backup_mod, "backup_registry_id",
                        lambda path: opened.append(path) or real(path))
    with RegistryService.open(home) as svc:
        doctor = svc.doctor()
    assert doctor["backups"] == len(list_backups(config)) >= 20
    assert not any("No backup exists yet" in w for w in doctor["warnings"])
    assert opened == [newest]


def test_the_service_lists_only_its_own_backups(shared):
    home_a, home_b, backups, id_a = shared
    with RegistryService.open(home_a) as a:
        assert a.backup()["performed"]
        own = [b["path"] for b in a.list_backups()]
        assert len(own) >= 1
    with RegistryService.open(home_b) as b:
        assert b.list_backups() == []
        doctor = b.doctor()
        assert doctor["backups"] == 0
        assert any("No backup exists yet" in w for w in doctor["warnings"])
