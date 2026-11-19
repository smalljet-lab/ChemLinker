"""Tests for compound_registry.sqlite_repo (no chemistry: Normalized objects are built by hand)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import compound_registry
from compound_registry.errors import (
    ChemistryError,
    FormatMismatchError,
    IdOverflowError,
    NotFoundError,
    RegistryError,
    SlotConflictError,
    StorageError,
)
from compound_registry.idformat import IdFormat
from compound_registry.models import BatchRecord, ExperimentRef, Normalized
from compound_registry.repository import RegistryRepository
from compound_registry.sqlite_repo import SqliteRepository, open_repository

T0 = datetime(2026, 11, 17, 15, 4, 5, 123456, tzinfo=timezone.utc)


class StepClock:
    """Deterministic clock: T0, T0+1s, T0+2s, ..."""

    def __init__(self, start: datetime = T0):
        self.now = start

    def __call__(self) -> datetime:
        value = self.now
        self.now += timedelta(seconds=1)
        return value


def norm(core: str = "A", stereo: str = "s1", mode: str = "abs", **extra) -> Normalized:
    """A hand-made Normalized. ``core`` and ``stereo`` stand in for real keys."""
    fields = dict(
        core_key=f"InChI=1S/{core}" + "/c" * 50,  # long text key, stored verbatim
        stereo_key=json.dumps({"stereo": stereo}),
        stereo_mode=mode,
        inchikey=f"IK-{core}-{stereo}-{mode}",
        smiles=f"C{core}{stereo}",
        inchi=f"InChI=1S/{core}/{stereo}",
        core_inchikey=f"CIK-{core}",
        formula=f"C{len(core)}H4",
        detected_mode=mode,
        molblock=f"\n  molfile {core} {stereo}\n\nM  END\n",
        rdkit_version="2026.09.2",
        inchi_version="1.07",
    )
    fields.update(extra)
    return Normalized(**fields)


def exp(ref: str = "4180", code: str | None = "ABC-008") -> ExperimentRef:
    return ExperimentRef(ref=ref, code=code, url=f"https://eln.example/exp/{ref}")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sub" / "registry.sqlite"


@pytest.fixture
def repo(db_path: Path):
    r = SqliteRepository(db_path, clock=StepClock())
    yield r
    r.close()


def raw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# ----------------------------------------------------------------------------- basics

def test_implements_protocol(repo):
    assert isinstance(repo, RegistryRepository)


def test_creates_folder_schema_and_meta(repo, db_path):
    assert db_path.exists()
    assert repo.get_meta("schema_version") == "1"
    assert repo.get_meta("id_format") == IdFormat.DEFAULT
    assert repo.get_meta("created_at") == "2026-11-17T15:04:05Z"
    with raw(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cores", "stereos", "batches", "meta", "events"} <= tables


def test_connection_pragmas(repo):
    conn = repo._conn
    assert conn.isolation_level is None
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10_000


def test_first_registration(repo):
    res = repo.register(norm(), exp(), 1, source="extension")
    b = res.batch
    assert res.created and res.core_created and res.stereo_created
    assert b.full_id == "000001_01_001"
    assert (b.core_no, b.stereo_no, b.batch_no) == (1, 1, 1)
    assert b.core_stereo_id == "000001_01"
    assert b.experiment_ref == "4180"
    assert b.experiment_code == "ABC-008"
    assert b.experiment_url == "https://eln.example/exp/4180"
    assert b.product_index == 1
    assert b.molfile == norm().molblock
    assert b.smiles == norm().smiles
    assert b.inchikey == norm().inchikey
    assert b.stereo_mode == "abs"
    assert b.source == "extension"
    assert b.rdkit_version == "2026.09.2" and b.inchi_version == "1.07"
    assert b.created_at.endswith("Z") and len(b.created_at) == 20
    assert not b.retired and b.retired_reason is None
    assert repo.get_batch("000001_01_001") == b


def test_same_core_new_stereo(repo):
    repo.register(norm("A", "s1"), exp("1"), 1)
    res = repo.register(norm("A", "s2"), exp("2"), 1)
    assert res.batch.full_id == "000001_02_001"
    assert res.created and not res.core_created and res.stereo_created


def test_new_core(repo):
    repo.register(norm("A"), exp("1"), 1)
    res = repo.register(norm("B"), exp("2"), 1)
    assert res.batch.full_id == "000002_01_001"
    assert res.core_created and res.stereo_created


def test_second_experiment_same_compound(repo):
    repo.register(norm(), exp("1"), 1)
    res = repo.register(norm(), exp("2"), 3)
    assert res.batch.full_id == "000001_01_002"
    assert res.created and not res.core_created and not res.stereo_created


def test_same_experiment_twice_is_idempotent(repo):
    first = repo.register(norm(), exp(), 1)
    before = repo.stats()
    again = repo.register(norm(), exp(), 1)
    assert again.created is False
    assert again.batch == first.batch
    assert again.warnings == ()
    assert repo.stats() == before == {"cores": 1, "stereos": 1, "batches": 1, "retired": 0}


def test_same_compound_other_product_index_same_experiment(repo):
    first = repo.register(norm(), exp(), 1)
    again = repo.register(norm(), exp(), 2)
    assert again.created is False
    assert again.batch.full_id == first.batch.full_id
    assert again.batch.product_index == 1
    assert again.warnings  # tells the user it is product 1
    assert repo.stats()["batches"] == 1


def test_two_products_in_one_experiment(repo):
    a = repo.register(norm("A"), exp(), 1)
    b = repo.register(norm("B"), exp(), 2)
    assert a.batch.full_id == "000001_01_001"
    assert b.batch.full_id == "000002_01_001"
    assert [x.full_id for x in repo.find_batches("4180")] == ["000001_01_001", "000002_01_001"]


def test_slot_conflict_consumes_nothing(repo):
    repo.register(norm("A"), exp("E1"), 1)
    with pytest.raises(SlotConflictError) as info:
        repo.register(norm("B"), exp("E1"), 1)  # new core into an occupied slot
    assert info.value.code == "slot_conflict"
    assert info.value.existing.full_id == "000001_01_001"
    assert info.value.to_dict()["existing_id"] == "000001_01_001"
    with pytest.raises(SlotConflictError):
        repo.register(norm("A", "s2"), exp("E1"), 1)  # new stereo into an occupied slot
    assert repo.stats() == {"cores": 1, "stereos": 1, "batches": 1, "retired": 0}
    # numbering continues without gaps
    assert repo.register(norm("B"), exp("E2"), 1).batch.full_id == "000002_01_001"
    assert repo.register(norm("A", "s2"), exp("E3"), 1).batch.full_id == "000001_02_001"


def test_retire_then_reregister(repo):
    first = repo.register(norm(), exp(), 1).batch
    retired = repo.retire(first.full_id, "  wrong structure drawn ")
    assert retired.retired
    assert retired.retired_reason == "wrong structure drawn"
    assert retired.retired_at.endswith("Z")
    again = repo.register(norm(), exp(), 1)
    assert again.created
    assert again.batch.full_id == "000001_01_002"
    old = repo.get_batch(first.full_id)
    assert old is not None and old.retired
    assert repo.stats() == {"cores": 1, "stereos": 1, "batches": 2, "retired": 1}


def test_retire_frees_the_slot_for_another_compound(repo):
    a = repo.register(norm("A"), exp(), 1).batch
    repo.retire(a.full_id, "was compound B")
    b = repo.register(norm("B"), exp(), 1)
    assert b.created and b.batch.full_id == "000002_01_001"


def test_retire_errors(repo):
    b = repo.register(norm(), exp(), 1).batch
    with pytest.raises(NotFoundError):
        repo.retire("000009_01_001", "x")
    with pytest.raises(RegistryError):
        repo.retire(b.full_id, "   ")
    repo.retire(b.full_id, "duplicate")
    with pytest.raises(RegistryError) as info:
        repo.retire(b.full_id, "again")
    assert info.value.code == "already_retired"
    assert repo.get_batch(b.full_id).retired_reason == "duplicate"


def test_stereo_mode_distinguishes_variants(repo):
    a = repo.register(norm("A", "same", "abs"), exp("1"), 1)
    b = repo.register(norm("A", "same", "rac"), exp("2"), 1)
    assert a.batch.full_id == "000001_01_001"
    assert b.batch.full_id == "000001_02_001"
    assert b.stereo_created
    # same experiment: abs and rac are different compounds -> two batches
    c = repo.register(norm("A", "same", "rac"), exp("1"), 2)
    assert c.created and c.batch.full_id == "000001_02_002"


def test_get_batch_unknown(repo):
    assert repo.get_batch("000001_01_001") is None


# ----------------------------------------------------------------------------- overflow

TINY = IdFormat("{core:01d}_{stereo:01d}_{batch:01d}")


def test_core_overflow(tmp_path):
    with SqliteRepository(tmp_path / "r.sqlite", TINY) as repo:
        for i in range(1, 10):
            assert repo.register(norm(f"C{i}"), exp(f"E{i}"), 1).batch.full_id == f"{i}_1_1"
        before = repo.stats()
        with pytest.raises(IdOverflowError) as info:
            repo.register(norm("C10"), exp("E10"), 1)
        assert info.value.code == "id_overflow"
        assert repo.stats() == before
        # existing cores still accept new batches
        assert repo.register(norm("C1"), exp("E10"), 1).batch.full_id == "1_1_2"


def test_stereo_overflow(tmp_path):
    with SqliteRepository(tmp_path / "r.sqlite", TINY) as repo:
        for i in range(1, 10):
            assert repo.register(norm("A", f"s{i}"), exp(f"E{i}"), 1).batch.full_id == f"1_{i}_1"
        before = repo.stats()
        with pytest.raises(IdOverflowError):
            repo.register(norm("A", "s10"), exp("E10"), 1)
        assert repo.stats() == before


def test_batch_overflow(tmp_path):
    with SqliteRepository(tmp_path / "r.sqlite", TINY) as repo:
        for i in range(1, 10):
            assert repo.register(norm("A"), exp(f"E{i}"), 1).batch.full_id == f"1_1_{i}"
        before = repo.stats()
        with pytest.raises(IdOverflowError):
            repo.register(norm("A"), exp("E10"), 1)
        assert repo.stats() == before
        assert repo.register(norm("B"), exp("E10"), 1).batch.full_id == "2_1_1"


# ----------------------------------------------------------------------------- atomicity

def test_exception_inside_transaction_rolls_back(repo, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("crash after the inserts")

    monkeypatch.setattr(repo, "_insert_event", boom)
    with pytest.raises(RuntimeError):
        repo.register(norm("A"), exp(), 1)
    monkeypatch.undo()
    assert repo.stats() == {"cores": 0, "stereos": 0, "batches": 0, "retired": 0}
    assert repo.register(norm("B"), exp(), 1).batch.full_id == "000001_01_001"


def test_invalid_arguments(repo):
    with pytest.raises(ChemistryError) as info:
        repo.register(norm(mode="weird"), exp(), 1)
    assert info.value.code == "invalid_mode"
    with pytest.raises(RegistryError):
        repo.register(norm(), ExperimentRef(ref="  "), 1)
    with pytest.raises(RegistryError):
        repo.register(norm(), exp(), 0)
    with pytest.raises(RegistryError):
        repo.register(norm(), exp(), True)  # type: ignore[arg-type]
    with pytest.raises(RegistryError):
        repo.register(norm(), exp(), 1, source="web")
    with pytest.raises(RegistryError):
        repo.register(norm(core_key=""), exp(), 1)
    assert repo.stats()["batches"] == 0


# ----------------------------------------------------------------------------- reopen / format

def test_persistence_across_reopen(db_path):
    with SqliteRepository(db_path) as repo:
        b = repo.register(norm(), exp(), 1).batch
        repo.retire(b.full_id, "test")
        repo.register(norm(), exp(), 1)
    with SqliteRepository(db_path) as repo:
        assert repo.get_batch(b.full_id).retired
        assert repo.register(norm(), exp("other"), 1).batch.full_id == "000001_01_003"
        assert repo.register(norm("B"), exp("other2"), 1).batch.full_id == "000002_01_001"
        assert repo.integrity_check() == "ok"


def test_format_mismatch_on_reopen(db_path):
    SqliteRepository(db_path).close()
    other = IdFormat("UB-{core:06d}_{stereo:02d}_{batch:03d}")
    with pytest.raises(FormatMismatchError) as info:
        SqliteRepository(db_path, other)
    assert info.value.code == "id_format_mismatch"
    assert IdFormat.DEFAULT in info.value.message and other.template in info.value.message
    # None adopts the stored format
    with SqliteRepository(db_path, None) as repo:
        assert repo.id_format == IdFormat()


def test_custom_format_is_stored_and_used(db_path):
    fmt = IdFormat("UB-{core:04d}-{stereo:02d}-{batch:02d}")
    with SqliteRepository(db_path, fmt) as repo:
        res = repo.register(norm(), exp(), 1)
        assert res.batch.full_id == "UB-0001-01-01"
        assert res.batch.core_stereo_id == "UB-0001-01"
    with SqliteRepository(db_path) as repo:  # adopts stored
        assert repo.id_format == fmt
    with pytest.raises(FormatMismatchError):
        SqliteRepository(db_path, IdFormat())


def test_open_repository_uses_config(tmp_path, monkeypatch):
    from compound_registry.config import load_config

    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "home"))
    cfg = load_config()
    with open_repository(cfg) as repo:
        assert repo.path == cfg.db_path
        assert repo.register(norm(), exp(), 1).batch.full_id == "000001_01_001"


def test_refuses_foreign_sqlite_file(tmp_path):
    path = tmp_path / "other.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE stuff (x)")
    conn.close()
    with pytest.raises(StorageError):
        SqliteRepository(path)


def test_garbage_file_is_storage_error(tmp_path):
    path = tmp_path / "junk.sqlite"
    path.write_bytes(b"this is definitely not a database" * 100)
    with pytest.raises(StorageError) as info:
        SqliteRepository(path)
    assert info.value.code == "storage_error"


def test_close_is_idempotent_and_context_manager(db_path):
    repo = SqliteRepository(db_path)
    repo.close()
    repo.close()
    with pytest.raises(StorageError):
        repo.get_batch("000001_01_001")
    with SqliteRepository(db_path) as r2:
        r2.register(norm(), exp(), 1)
    with pytest.raises(StorageError):
        r2.stats()


# ----------------------------------------------------------------------------- triggers

@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM cores",
        "DELETE FROM stereos",
        "DELETE FROM batches",
        "DELETE FROM events",
        "UPDATE cores SET formula = 'X'",
        "UPDATE cores SET core_key = 'other'",
        "UPDATE stereos SET stereo_mode = 'rac'",
        "UPDATE stereos SET molfile = ''",
        "UPDATE batches SET experiment_ref = 'other'",
        "UPDATE batches SET product_index = 7",
        "UPDATE batches SET full_id = '000009_09_009'",
        "UPDATE batches SET retired_at = '2026-01-01T00:00:00Z', retired_reason = 'x', smiles = 'C'",
        "UPDATE batches SET retired_at = '2026-01-01T00:00:00Z', retired_reason = '  '",
        "UPDATE batches SET retired_at = '2026-01-01T00:00:00Z'",
        "UPDATE events SET event = 'x'",
        "UPDATE meta SET value = 'X' WHERE key = 'id_format'",
        "DELETE FROM meta WHERE key = 'id_format'",
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('id_format', 'X')",
        "INSERT OR REPLACE INTO cores(core_no, core_key, inchikey, formula, created_at)"
        " VALUES (1, 'x', '', '', '')",
        "INSERT OR REPLACE INTO cores(core_no, core_key, inchikey, formula, created_at)"
        " VALUES (3, (SELECT core_key FROM cores WHERE core_no = 1), '', '', '')",
        "INSERT INTO cores(core_no, core_key, inchikey, formula, created_at) VALUES (5, 'gap', '', '', '')",
    ],
)
def test_triggers_block_raw_changes(repo, db_path, sql):
    repo.register(norm("A"), exp("1"), 1)
    repo.register(norm("B"), exp("2"), 1)
    repo.close()
    with raw(db_path) as conn:
        before = [list(map(tuple, conn.execute(f"SELECT * FROM {t} ORDER BY 1")))
                  for t in ("cores", "stereos", "batches", "meta", "events")]
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
        after = [list(map(tuple, conn.execute(f"SELECT * FROM {t} ORDER BY 1")))
                 for t in ("cores", "stereos", "batches", "meta", "events")]
    conn.close()
    assert before == after


def test_raw_retire_once_allowed_but_not_undo(repo, db_path):
    repo.register(norm(), exp(), 1)
    repo.close()
    conn = raw(db_path)
    try:
        conn.execute("UPDATE batches SET retired_at = '2026-01-01T00:00:00Z', retired_reason = 'r'")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE batches SET retired_at = NULL, retired_reason = NULL")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE batches SET retired_reason = 'changed'")
    finally:
        conn.close()


def test_mutable_meta(repo):
    assert repo.get_meta("last_backup_at") is None
    repo.set_meta("last_backup_at", "2026-01-01T00:00:00Z")
    repo.set_meta("last_backup_at", "2026-01-02T00:00:00Z")
    assert repo.get_meta("last_backup_at") == "2026-01-02T00:00:00Z"
    with pytest.raises(RegistryError):
        repo.set_meta("id_format", "x")
    assert repo.get_meta("id_format") == IdFormat.DEFAULT


# ----------------------------------------------------------------------------- events

def test_events_are_logged(repo, db_path):
    b = repo.register(norm(), exp(), 1).batch
    repo.register(norm(), exp(), 1)
    repo.retire(b.full_id, "oops")
    rows = raw(db_path).execute("SELECT at, event, full_id, details FROM events ORDER BY event_id").fetchall()
    assert [r["event"] for r in rows] == ["create", "register", "register_repeat", "retire"]
    assert all(r["at"].endswith("Z") for r in rows)
    assert rows[1]["full_id"] == b.full_id
    details = json.loads(rows[1]["details"])
    assert details["experiment_ref"] == "4180" and details["product_index"] == 1
    assert json.loads(rows[3]["details"]) == {"reason": "oops"}


def test_failed_registration_writes_no_event(repo, db_path):
    repo.register(norm("A"), exp(), 1)
    with pytest.raises(SlotConflictError):
        repo.register(norm("B"), exp(), 1)
    events = [r[0] for r in raw(db_path).execute("SELECT event FROM events")]
    assert events == ["create", "register"]


# ----------------------------------------------------------------------------- reading

def test_lookup_contents(repo):
    empty = repo.lookup(norm("A"), "E1", 1)
    assert empty.core is None and empty.stereo is None and empty.batches == ()
    assert empty.existing_batch is None and empty.slot_conflict is None

    b1 = repo.register(norm("A", "s1"), exp("E1"), 1).batch
    b2 = repo.register(norm("A", "s1"), exp("E2"), 1).batch
    repo.retire(b1.full_id, "bad")
    b3 = repo.register(norm("A", "s1"), exp("E1"), 2).batch
    s2 = repo.register(norm("A", "s2"), exp("E3"), 1).batch
    repo.register(norm("A", "s2"), exp("E4"), 1)
    repo.register(norm("A", "s3", "rac"), exp("E5"), 1)
    other = repo.register(norm("Z"), exp("E9"), 1).batch

    res = repo.lookup(norm("A", "s1"), "E1", 1)
    assert res.core is not None
    assert res.core.core_no == 1 and res.core.core_key == norm("A").core_key
    assert res.core.inchikey == "CIK-A" and res.core.formula == "C1H4"
    assert res.stereo is not None
    assert res.stereo.core_stereo_id == "000001_01"
    assert res.stereo.stereo_key == norm("A", "s1").stereo_key
    assert res.stereo.molfile == norm("A", "s1").molblock
    assert res.stereo.batch_count == 3
    assert [b.full_id for b in res.batches] == [b1.full_id, b2.full_id, b3.full_id]
    assert res.batches[0].retired
    assert [(s.stereo_no, s.batch_count, s.stereo_mode) for s in res.other_stereos] == [
        (2, 2, "abs"), (3, 1, "rac")]
    assert res.existing_batch == b3  # active one, any product index
    # slot 1 of E1 is free again (retired), so no conflict
    assert res.slot_conflict is None

    # a structure of an unregistered stereo of core A
    res = repo.lookup(norm("A", "s9"), "E3", 1)
    assert res.core.core_no == 1 and res.stereo is None and res.batches == ()
    assert [s.stereo_no for s in res.other_stereos] == [1, 2, 3]
    assert res.existing_batch is None
    assert res.slot_conflict.full_id == s2.full_id

    # unknown structure, slot taken
    res = repo.lookup(norm("Q"), "E9", 1)
    assert res.core is None and res.slot_conflict == other

    # no experiment given -> neither existing nor conflict
    res = repo.lookup(norm("A", "s1"))
    assert res.existing_batch is None and res.slot_conflict is None
    # experiment but no product index -> no conflict, existing still reported
    res = repo.lookup(norm("A", "s2"), "E3")
    assert res.existing_batch.full_id == s2.full_id and res.slot_conflict is None


def test_iter_batches_order(repo):
    repo.register(norm("B"), exp("1"), 1)      # 000001_01_001
    repo.register(norm("A"), exp("2"), 1)      # 000002_01_001
    repo.register(norm("B", "s2"), exp("3"), 1)  # 000001_02_001
    x = repo.register(norm("B"), exp("4"), 1).batch  # 000001_01_002
    repo.register(norm("A"), exp("5"), 1)      # 000002_01_002
    repo.retire(x.full_id, "r")
    ids = [b.full_id for b in repo.iter_batches()]
    assert ids == ["000001_01_001", "000001_01_002", "000001_02_001", "000002_01_001", "000002_01_002"]
    assert all(isinstance(b, BatchRecord) for b in repo.iter_batches())
    active = [b.full_id for b in repo.iter_batches(include_retired=False)]
    assert active == ["000001_01_001", "000001_02_001", "000002_01_001", "000002_01_002"]


def test_find_batches_by_product_index(repo):
    repo.register(norm("A"), exp("E"), 2)
    a = repo.register(norm("B"), exp("E"), 1).batch
    repo.retire(a.full_id, "r")
    repo.register(norm("C"), exp("E"), 1)
    repo.register(norm("D"), exp("other"), 1)
    found = repo.find_batches("E")
    assert [(b.product_index, b.full_id, b.retired) for b in found] == [
        (1, "000002_01_001", True), (1, "000003_01_001", False), (2, "000001_01_001", False)]
    assert repo.find_batches("nothing") == ()


def test_timestamps_from_injected_clock(db_path):
    clock = StepClock(datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2))))
    with SqliteRepository(db_path, clock=clock) as repo:
        assert repo.get_meta("created_at") == "2030-01-02T01:04:05Z"
        b = repo.register(norm(), exp(), 1).batch
        assert b.created_at == "2030-01-02T01:04:06Z"
        assert repo.lookup(norm()).core.created_at == b.created_at


def test_backup_to_and_integrity(repo, tmp_path):
    repo.register(norm(), exp(), 1)
    assert repo.integrity_check() == "ok"
    dest = tmp_path / "copy" / "c.sqlite"
    repo.backup_to(dest)
    with SqliteRepository(dest) as copy:
        assert copy.get_batch("000001_01_001") is not None
    with pytest.raises(StorageError):
        repo.backup_to(repo.path)


# ----------------------------------------------------------------------------- concurrency

WORKER = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from compound_registry.models import ExperimentRef, Normalized
    from compound_registry.sqlite_repo import SqliteRepository

    db, idx, go = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
    deadline = time.time() + 60
    while not go.exists():
        if time.time() > deadline:
            sys.exit(3)
        time.sleep(0.002)

    def n(core):
        return Normalized(core_key="InChI=1S/" + core, stereo_key="{}", stereo_mode="abs",
                          inchikey="K", smiles="C")

    out = []
    with SqliteRepository(db) as repo:   # opened concurrently too (first-time creation race)
        for k in range(3):
            r = repo.register(n(f"W{idx}-{k}"), ExperimentRef(f"E{idx}-{k}"), 1)
            out.append(["unique", r.batch.full_id, r.created])
            r = repo.register(n("SHARED"), ExperimentRef("SHARED-EXP"), 1)
            out.append(["shared", r.batch.full_id, r.created])
            r = repo.register(n("SHARED"), ExperimentRef(f"P{idx}-{k}"), 1)
            out.append(["multi", r.batch.full_id, r.created])
    print(json.dumps(out))
    """
)


def test_concurrent_processes(tmp_path):
    workers = 6
    script = tmp_path / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    db = tmp_path / "shared" / "registry.sqlite"
    go = tmp_path / "go"
    env = dict(os.environ)
    src = str(Path(compound_registry.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    procs = [
        subprocess.Popen([sys.executable, str(script), str(db), str(i), str(go)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for i in range(workers)
    ]
    time.sleep(1.0)  # let the interpreters start and wait at the barrier
    go.write_text("go")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
        results.extend(json.loads(out.strip().splitlines()[-1]))

    created = [r[1] for r in results if r[2]]
    assert len(created) == len(set(created))  # every created ID is unique

    shared = [r for r in results if r[0] == "shared"]
    assert len(shared) == workers * 3
    assert len({r[1] for r in shared}) == 1           # all got the same ID
    assert sum(1 for r in shared if r[2]) == 1        # exactly one created it

    with SqliteRepository(db) as repo:
        batches = list(repo.iter_batches())
        assert repo.integrity_check() == "ok"
        stats = repo.stats()
    expected_cores = workers * 3 + 1
    assert stats == {"cores": expected_cores, "stereos": expected_cores,
                     "batches": workers * 3 + 1 + workers * 3, "retired": 0}
    assert sorted({b.core_no for b in batches}) == list(range(1, expected_cores + 1))
    shared_core = next(b.core_no for b in batches if b.full_id == shared[0][1])
    shared_batches = sorted(b.batch_no for b in batches if b.core_no == shared_core)
    assert shared_batches == list(range(1, workers * 3 + 2))  # gap-free 1..19
    assert sum(1 for b in batches if b.experiment_ref == "SHARED-EXP") == 1
    assert len({b.full_id for b in batches}) == len(batches)


# ----------------------------------------------------------------------------- damaged registry

def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


@pytest.mark.parametrize("table", ["cores", "stereos", "batches", "events"])
def test_dropped_table_is_refused_not_recreated(db_path, table):
    with SqliteRepository(db_path) as repo:
        repo.register(norm("A"), exp("1"), 1)
        repo.register(norm("B"), exp("2"), 1)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(f"DROP TABLE {table}")
    conn.close()
    with pytest.raises(StorageError) as info:
        SqliteRepository(db_path)
    assert info.value.code == "registry_damaged"
    assert table in info.value.message and "backup" in info.value.message
    assert table not in _tables(db_path)  # nothing was re-created


def test_two_dropped_tables_are_both_named(db_path):
    SqliteRepository(db_path).close()
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("DROP TABLE batches")
    conn.execute("DROP TABLE events")
    conn.close()
    with pytest.raises(StorageError) as info:
        SqliteRepository(db_path)
    assert "batches" in info.value.message and "events" in info.value.message


def test_empty_file_is_initialised(tmp_path):
    path = tmp_path / "empty.sqlite"
    path.write_bytes(b"")
    with SqliteRepository(path) as repo:
        assert repo.register(norm(), exp(), 1).batch.full_id == "000001_01_001"


@pytest.mark.parametrize(
    "sql, words",
    [
        # a lost core (core numbers 1..2 but only one row)
        (["DROP TRIGGER cores_no_delete", "DELETE FROM cores WHERE core_no = 1"], "CORE"),
        # a lost stereo variant at the start of a core
        (["DROP TRIGGER stereos_no_delete", "DELETE FROM stereos WHERE stereo_no = 1"], "STEREO"),
        # a lost batch
        (["DROP TRIGGER batches_no_delete", "DELETE FROM batches WHERE batch_no = 1"], "BATCH"),
        # a lost register event
        (["DROP TRIGGER events_no_delete",
          "DELETE FROM events WHERE event_id = "
          "(SELECT max(event_id) FROM events WHERE event = 'register')"],
         "'register' event"),
    ],
)
def test_consistency_check_on_open(db_path, sql, words):
    with SqliteRepository(db_path) as repo:
        repo.register(norm("A", "s1"), exp("1"), 1)   # 000001_01_001
        repo.register(norm("A", "s2"), exp("2"), 1)   # 000001_02_001
        repo.register(norm("A", "s2"), exp("3"), 1)   # 000001_02_002
        repo.register(norm("B"), exp("4"), 1)         # 000002_01_001
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    for statement in sql:
        conn.execute(statement)
    conn.close()
    with pytest.raises(StorageError) as info:
        SqliteRepository(db_path)
    assert info.value.code == "registry_damaged"
    assert words in info.value.message
    assert "backup" in info.value.message


def test_consistent_registry_opens(db_path):
    with SqliteRepository(db_path) as repo:
        b = repo.register(norm("A"), exp("1"), 1).batch
        repo.register(norm("A"), exp("2"), 1)
        repo.retire(b.full_id, "r")
        repo.register(norm("A"), exp("1"), 1)
    with SqliteRepository(db_path) as repo:
        assert repo.stats()["batches"] == 3


# ----------------------------------------------------------------------------- experiment ref

def test_experiment_ref_is_stripped(repo):
    first = repo.register(norm(), ExperimentRef("4180"), 1)
    again = repo.register(norm(), ExperimentRef("4180 "), 1)
    tabbed = repo.register(norm(), ExperimentRef("\t4180\n"), 1)
    assert again.created is False and tabbed.created is False
    assert again.batch.full_id == tabbed.batch.full_id == first.batch.full_id
    assert repo.stats()["batches"] == 1
    padded = repo.register(norm("B"), ExperimentRef("  E2  "), 1).batch
    assert padded.experiment_ref == "E2"
    assert repo.lookup(norm(), " 4180 ").existing_batch.full_id == first.batch.full_id
    assert repo.lookup(norm("C"), "4180  ", 1).slot_conflict.full_id == first.batch.full_id
    assert [b.full_id for b in repo.find_batches(" E2 ")] == [padded.full_id]
    with pytest.raises(SlotConflictError):
        repo.register(norm("C"), ExperimentRef(" 4180"), 1)
    with pytest.raises(RegistryError) as info:
        repo.register(norm("C"), ExperimentRef(" \t "), 1)
    assert info.value.code == "invalid_argument"


# ----------------------------------------------------------------------------- recovery helpers

def test_register_with_created_at_and_expected_id(repo):
    res = repo.register(norm("A"), exp("1"), 1, created_at="2020-01-02T03:04:05Z",
                        expect_full_id="000001_01_001")
    assert res.batch.created_at == "2020-01-02T03:04:05Z"
    assert repo.lookup(norm("A")).core.created_at == "2020-01-02T03:04:05Z"
    before = repo.stats()
    with pytest.raises(RegistryError) as info:
        repo.register(norm("B"), exp("2"), 1, expect_full_id="000005_01_001")
    assert info.value.code == "id_mismatch" and "000002_01_001" in info.value.message
    assert repo.stats() == before  # rolled back
    # an idempotent repeat must also be the expected ID
    with pytest.raises(RegistryError):
        repo.register(norm("A"), exp("1"), 1, expect_full_id="000001_01_002")
    assert repo.register(norm("A"), exp("1"), 1, expect_full_id="000001_01_001").created is False
    with pytest.raises(RegistryError) as info:
        repo.register(norm("B"), exp("2"), 1, created_at="yesterday")
    assert info.value.code == "invalid_argument"
    assert repo.stats() == before


def test_retire_with_retired_at(repo):
    b = repo.register(norm(), exp(), 1).batch
    retired = repo.retire(b.full_id, "r", retired_at="2021-05-06T07:08:09Z")
    assert retired.retired_at == "2021-05-06T07:08:09Z"


NORM_FIELDS = ("core_key", "stereo_key", "stereo_mode", "inchikey", "smiles", "inchi",
               "core_inchikey", "formula", "detected_mode", "mode_overridden", "molblock",
               "input_format", "rdkit_version", "inchi_version")


def test_registration_details(repo):
    n = norm("A", mode="rac", mode_overridden=True, detected_mode="abs", input_format="smiles")
    a = repo.register(n, exp("1"), 2).batch
    repo.retire(a.full_id, "bad")
    (d,) = repo.registration_details([a.full_id])
    assert d["id"] == a.full_id and d["at"] == a.created_at
    assert (d["experiment_ref"], d["experiment_code"], d["experiment_url"]) == \
        ("1", "ABC-008", "https://eln.example/exp/1")
    assert d["product_index"] == 2 and d["source"] == "cli"
    for name in NORM_FIELDS:
        assert d[name] == getattr(n, name), name
    assert d["retired_at"] == repo.get_batch(a.full_id).retired_at and d["retired_reason"] == "bad"
    assert d["register_seq"] < d["retire_seq"]
    assert repo.registration_details([]) == []


def test_replay_is_all_or_nothing(tmp_path):
    from compound_registry.sqlite_repo import ReplayRegister, ReplayRetire

    src = tmp_path / "src.sqlite"
    with SqliteRepository(src, clock=StepClock()) as repo:
        repo.register(norm("A"), exp("1"), 1)
        repo.register(norm("A"), exp("2"), 1)
        repo.retire("000001_01_001", "wrong")
        repo.register(norm("B"), exp("1"), 1)
        details = repo.registration_details()
        original = list(repo.iter_batches())

    def op(d, expect=None):
        n = Normalized(**{k: d[k] for k in NORM_FIELDS})
        return ReplayRegister(n, ExperimentRef(d["experiment_ref"], d["experiment_code"],
                                               d["experiment_url"]),
                              d["product_index"], d["source"], d["at"], expect or d["id"])

    retire = ReplayRetire("000001_01_001", "wrong", details[0]["retired_at"])
    with SqliteRepository(tmp_path / "dst.sqlite") as dst:
        # B needs the slot freed by the retirement: without it the replay fails ...
        with pytest.raises(RegistryError) as info:
            dst.replay([op(details[0]), op(details[1]), op(details[2])])
        assert "000002_01_001" in info.value.message
        assert dst.stats()["batches"] == 0  # ... and nothing at all is kept
        # a wrong expected ID also rolls everything back
        with pytest.raises(RegistryError) as info:
            dst.replay([op(details[0]), op(details[1], expect="000001_01_009")])
        assert info.value.code == "id_mismatch" and "000001_01_009" in info.value.message
        assert dst.stats()["batches"] == 0
        # in journal order it reproduces everything exactly
        assert dst.replay([op(details[0]), op(details[1]), retire, op(details[2])]) == \
            ["000001_01_001", "000001_01_002", "000001_01_001", "000002_01_001"]
        assert list(dst.iter_batches()) == original
