"""Tests for compound_registry.backup."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from compound_registry.backup import (
    BackupOutcome,
    backup_now,
    list_backups,
    maybe_backup,
    open_readonly,
    readonly_target,
    verify_backup,
)
from compound_registry.config import Config, load_config
from compound_registry.models import ExperimentRef, Normalized
from compound_registry.sqlite_repo import SqliteRepository

T0 = datetime(2026, 11, 17, 8, 30, 0, tzinfo=timezone.utc)


def norm(core: str = "A") -> Normalized:
    return Normalized(core_key=f"InChI=1S/{core}", stereo_key="{}", stereo_mode="abs",
                      inchikey=f"IK-{core}", smiles="C", molblock="M  END\n")


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "home"))


@pytest.fixture
def config() -> Config:
    return load_config()  # under tmp_path/home thanks to the env var


@pytest.fixture
def repo(config):
    r = SqliteRepository(config.db_path)
    r.register(norm("A"), ExperimentRef("E1"), 1)
    r.register(norm("B"), ExperimentRef("E1"), 2)
    yield r
    r.close()


def test_backup_now_writes_verified_copy(repo, config):
    out = backup_now(repo, config, now=T0)
    assert isinstance(out, BackupOutcome)
    assert out.performed and out.warning is None
    assert out.path == config.backup_dir / "registry-20261117-083000.sqlite"
    assert out.path.is_file()
    assert verify_backup(out.path) == "ok"
    assert not list(config.backup_dir.glob(".*partial"))
    # the copy contains the data
    with SqliteRepository(out.path) as copy:
        assert [b.full_id for b in copy.iter_batches()] == ["000001_01_001", "000002_01_001"]
    # recorded in meta and in the event log
    assert repo.get_meta("last_backup_at") == "2026-11-17T08:30:00Z"
    conn = sqlite3.connect(config.db_path)
    try:
        row = conn.execute("SELECT details FROM events WHERE event = 'backup'").fetchone()
    finally:
        conn.close()
    assert json.loads(row[0])["path"] == str(out.path)


def test_backup_name_uses_utc(repo, config):
    local = T0.astimezone(timezone(timedelta(hours=-5)))
    out = backup_now(repo, config, now=local)
    assert out.path.name == "registry-20261117-083000.sqlite"


def test_maybe_backup_respects_interval(repo, config):
    first = maybe_backup(repo, config, now=T0)
    assert first.performed
    assert maybe_backup(repo, config, now=T0 + timedelta(hours=1)) == BackupOutcome(performed=False)
    assert not maybe_backup(repo, config, now=T0 + timedelta(hours=23, minutes=59)).performed
    later = maybe_backup(repo, config, now=T0 + timedelta(hours=24))
    assert later.performed
    assert len(list_backups(config)) == 2
    # clock went backwards: back up rather than trust a future timestamp
    assert maybe_backup(repo, config, now=T0 - timedelta(days=3)).performed


def test_maybe_backup_interval_zero_always(repo, config):
    cfg = replace(config, backup_interval_hours=0)
    assert maybe_backup(repo, cfg, now=T0).performed
    assert maybe_backup(repo, cfg, now=T0 + timedelta(seconds=1)).performed


def test_retention_keeps_newest(repo, config):
    cfg = replace(config, backup_keep=3)
    unrelated = cfg.backup_dir / "notes.txt"
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("keep me")
    paths = []
    removed_all: list[Path] = []
    for day in range(5):
        out = backup_now(repo, cfg, now=T0 + timedelta(days=day))
        assert out.performed
        paths.append(out.path)
        removed_all.extend(out.removed)
    assert list_backups(cfg) == paths[-3:]
    assert removed_all == paths[:2]
    assert unrelated.exists()


@pytest.mark.parametrize("keep", [1, 2, 3])
def test_retention_never_deletes_the_new_backup(repo, config, keep):
    """The clock went backwards: the new file sorts as the OLDEST backup."""
    cfg = replace(config, backup_keep=keep)
    for day in range(1, 4):
        assert backup_now(repo, cfg, now=T0 + timedelta(days=day)).performed
    out = backup_now(repo, cfg, now=T0 - timedelta(days=30))
    assert out.performed and out.path.is_file()
    assert out.path not in out.removed
    remaining = list_backups(cfg)
    assert out.path in remaining and len(remaining) == keep
    assert remaining[0] == out.path  # it is the oldest by name, and it is kept
    assert verify_backup(out.path) == "ok"


def test_retention_with_new_backup_in_the_middle(repo, config):
    cfg = replace(config, backup_keep=2)
    backup_now(repo, cfg, now=T0)
    backup_now(repo, cfg, now=T0 + timedelta(days=2))
    out = backup_now(repo, cfg, now=T0 + timedelta(days=1))
    assert out.path.is_file()
    assert [p.name for p in list_backups(cfg)] == [
        "registry-20261118-083000.sqlite", "registry-20261119-083000.sqlite"]


def test_retention_zero_keeps_all(repo, config):
    cfg = replace(config, backup_keep=0)
    for day in range(4):
        assert backup_now(repo, cfg, now=T0 + timedelta(days=day)).removed == ()
    assert len(list_backups(cfg)) == 4


def test_list_backups_order_and_filter(config):
    folder = config.backup_dir
    folder.mkdir(parents=True)
    for name in ["registry-20261117-120000.sqlite", "registry-20250101-000000.sqlite",
                 ".registry-20261118-000000.sqlite.partial", "registry-latest.sqlite", "other.sqlite"]:
        (folder / name).write_bytes(b"")
    assert [p.name for p in list_backups(config)] == [
        "registry-20250101-000000.sqlite", "registry-20261117-120000.sqlite"]


def test_list_backups_missing_folder(config):
    assert list_backups(replace(config, backup_dir=config.home / "nope")) == []


def test_backup_folder_is_a_file_never_raises(repo, config, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file")
    cfg = replace(config, backup_dir=blocker / "backups")
    out = backup_now(repo, cfg, now=T0)
    assert not out.performed and out.warning
    out = maybe_backup(repo, cfg, now=T0)
    assert not out.performed and out.warning
    assert repo.get_meta("last_backup_at") is None


def test_invalid_backup_folder_never_raises(repo, config):
    cfg = replace(config, backup_dir=Path("Q:\\no<such>|drive\x00\\bk"))
    out = maybe_backup(repo, cfg, now=T0)
    assert not out.performed and out.warning


def test_closed_repository_never_raises(repo, config):
    repo.close()
    out = maybe_backup(repo, config, now=T0)
    assert not out.performed and out.warning
    out = backup_now(repo, config, now=T0)
    assert not out.performed and out.warning
    assert list_backups(config) == []
    if config.backup_dir.exists():
        assert not list(config.backup_dir.glob(".*partial"))  # temp file cleaned up


def test_refuses_to_overwrite_live_database(tmp_path):
    folder = tmp_path / "same"
    db = folder / "registry-20261117-083000.sqlite"
    cfg = Config(home=tmp_path, config_path=tmp_path / "config.toml", db_path=db, backup_dir=folder)
    with SqliteRepository(db) as repo:
        repo.register(norm(), ExperimentRef("E"), 1)
        out = backup_now(repo, cfg, now=T0)
        assert not out.performed and "live database" in out.warning
        assert repo.get_batch("000001_01_001") is not None
        # a different second is fine, in the same folder
        assert backup_now(repo, cfg, now=T0 + timedelta(seconds=1)).performed


def test_verify_backup_problems(tmp_path):
    assert verify_backup(tmp_path / "missing.sqlite") != "ok"
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database at all" * 200)
    assert verify_backup(junk) != "ok"
    foreign = tmp_path / "foreign.sqlite"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE x (y)")
    conn.commit()
    conn.close()
    assert "not a compound registry" in verify_backup(foreign)


def test_readonly_target_for_drive_and_unc_paths(tmp_path):
    target, is_uri = readonly_target(tmp_path / "a b#c.sqlite")
    assert is_uri and target.endswith("?mode=ro")
    assert target.startswith("file:///") or (os.name != "nt" and target.startswith("file:/"))
    assert "a%20b%23c.sqlite" in target  # special characters are escaped
    for unc in (r"\\server\share\backups\registry-20261117-083000.sqlite",
                "//server/share/backups/registry.sqlite"):
        target, is_uri = readonly_target(Path(unc))
        assert is_uri is False
        assert not target.startswith("file:")
        assert "server" in target and "share" in target


def test_verify_backup_through_the_unc_fallback(repo, config, monkeypatch):
    """Simulate a UNC path: the plain-path connection must work and be read-only."""
    from compound_registry import backup as backup_mod

    out = backup_now(repo, config, now=T0)
    seen: list[str] = []

    def as_unc(path):
        seen.append(str(path))
        return os.path.abspath(str(path)), False

    monkeypatch.setattr(backup_mod, "readonly_target", as_unc)
    assert verify_backup(out.path) == "ok"
    assert seen == [os.path.abspath(str(out.path))]
    before = out.path.read_bytes()
    conn = open_readonly(out.path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE x (y)")
    finally:
        conn.close()
    assert out.path.read_bytes() == before


def test_verify_backup_on_a_real_unc_path(repo, config):
    """Uses the administrative share \\\\localhost\\C$ when it is reachable."""
    out = backup_now(repo, config, now=T0)
    local = os.path.abspath(str(out.path))
    drive, rest = os.path.splitdrive(local)
    if os.name != "nt" or len(drive) != 2:
        pytest.skip("Windows drive paths only")
    unc = Path(f"\\\\localhost\\{drive[0]}$" + rest)
    try:
        reachable = unc.is_file()
    except OSError:
        reachable = False
    if not reachable:
        pytest.skip("the administrative share \\\\localhost\\C$ is not reachable here")
    assert readonly_target(unc)[1] is False
    assert verify_backup(unc) == "ok"


def test_restore_from_backup(repo, config, tmp_path):
    out = backup_now(repo, config, now=T0)
    assert out.performed
    repo.close()
    restored = tmp_path / "restored" / "registry.sqlite"
    restored.parent.mkdir()
    shutil.copy2(out.path, restored)
    with SqliteRepository(restored) as r:
        assert r.integrity_check() == "ok"
        assert [b.full_id for b in r.iter_batches()] == ["000001_01_001", "000002_01_001"]
        assert r.register(norm("C"), ExperimentRef("E2"), 1).batch.full_id == "000003_01_001"
        # the repeat of a backed-up registration is still idempotent
        assert not r.register(norm("A"), ExperimentRef("E1"), 1).created
