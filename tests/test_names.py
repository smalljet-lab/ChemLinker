"""Names of stereo variants: storage (sqlite_repo), service, CLI ``name``, host ``set_name``.

A name is an annotation stored per CORE_STEREO (meant for the IUPAC name the
user pastes). It may change or disappear, every change is an event, and it is
never part of a key.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from compound_registry import backup as backup_mod
from compound_registry import cli, host, protocol, sqlite_repo
from compound_registry.backup import BackupOutcome
from compound_registry.cli import main
from compound_registry.errors import NotFoundError, RegistryError
from compound_registry.host import HomeChoice
from compound_registry.service import FINGERPRINT_KEY, RegistryService
from compound_registry.sqlite_repo import SqliteRepository

AMINE_R = "C[C@H](N)c1ccccc1"
AMINE_S = "C[C@@H](N)c1ccccc1"
ETHANOL = "CCO"
NAME_R = "(1R)-1-phenylethan-1-amine"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)


@pytest.fixture
def home(tmp_path) -> Path:
    """000001_01 (R)-amine, 000001_02 (S)-amine, 000002_01 ethanol."""
    path = tmp_path / "home"
    with RegistryService.open(path) as svc:
        svc.register(smiles=AMINE_R, experiment_ref="101", experiment_code="ABC-001")
        svc.register(smiles=AMINE_S, experiment_ref="102", experiment_code="ABC-002")
        svc.register(smiles=ETHANOL, experiment_ref="103", experiment_code="ABC-003")
    return path


def db(home: Path) -> Path:
    return home / "registry.sqlite"


def events(home: Path, kinds=sqlite_repo.NAME_EVENTS) -> list[tuple[str, dict]]:
    with closing(sqlite3.connect(db(home).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        rows = c.execute("SELECT event, full_id, details FROM events ORDER BY event_id").fetchall()
    out = []
    for event, full_id, details in rows:
        if event in kinds:
            assert full_id is None
            out.append((event, json.loads(details)))
    return out


# --------------------------------------------------------------------------- storage

def test_set_change_remove(home):
    with SqliteRepository(db(home)) as repo:
        assert repo.get_name("000001_01") is None and repo.get_names() == {}
        rec, changed = repo.set_name("000001_01", "  first name ")
        assert changed and rec.text == "first name" and rec.core_stereo_id == "000001_01"
        sqlite_repo.parse_timestamp(rec.updated_at)  # the registry's timestamp format
        rec2, changed = repo.set_name("000001_01", NAME_R)
        assert changed and rec2.text == NAME_R
        assert repo.get_name("000001_01") == rec2
        assert set(repo.get_names()) == {"000001_01"}
        same, changed = repo.set_name("000001_01", NAME_R + "  ")
        assert not changed and same == rec2
        gone, changed = repo.set_name("000001_01", None)
        assert changed and gone is None and repo.get_name("000001_01") is None
        nothing, changed = repo.set_name("000001_01", None)
        assert not changed and nothing is None
    assert events(home) == [
        ("name_set", {"core_stereo_id": "000001_01", "name": "first name", "previous": None,
                      "source": "user"}),
        ("name_set", {"core_stereo_id": "000001_01", "name": NAME_R, "previous": "first name",
                      "source": "user"}),
        ("name_removed", {"core_stereo_id": "000001_01", "name": None, "previous": NAME_R}),
    ]


def test_unknown_core_stereo_is_not_found(home):
    with SqliteRepository(db(home)) as repo:
        for bad in ("000009_01", "000001_03", "000001_01_001", "1"):
            with pytest.raises(NotFoundError):
                repo.set_name(bad, "x")
            with pytest.raises(NotFoundError):
                repo.get_name(bad)
        with pytest.raises(RegistryError) as info:
            repo.set_name("000001_01", "   ")
        assert info.value.code == "invalid_argument"
    assert events(home) == []


def test_names_survive_reopening_and_do_not_disturb_the_consistency_check(home):
    with SqliteRepository(db(home)) as repo:
        for _ in range(3):
            repo.set_name("000001_01", "a")
            repo.set_name("000001_01", None)
        repo.set_name("000001_02", "kept")
    with SqliteRepository(db(home)) as repo:  # _check_consistency counts register events only
        assert {k: v.text for k, v in repo.get_names().items()} == {"000001_02": "kept"}
        assert repo.integrity_check() == "ok"
    with RegistryService.open(home) as svc:
        assert svc.doctor()["integrity"] == "ok"


def test_names_cannot_point_to_a_missing_variant(home):
    with closing(sqlite3.connect(str(db(home)), isolation_level=None)) as c:
        c.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO names(core_stereo_id, name, updated_at) "
                      "VALUES ('000099_01', 'x', '2026-11-17T00:00:00Z')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO names(core_stereo_id, name, updated_at) "
                      "VALUES ('000001_01', '  ', '2026-11-17T00:00:00Z')")


def test_a_registry_from_before_names_opens_and_gets_the_table(home):
    with closing(sqlite3.connect(str(db(home)), isolation_level=None)) as c:
        c.execute("DROP TABLE names")   # as created by the first release
    assert "names" not in sqlite_repo._TABLES  # not required: not "damaged"
    with RegistryService.open(home) as svc:
        assert svc.doctor()["integrity"] == "ok"
        assert svc.analyze(smiles=AMINE_R)["name"] is None
        assert svc.set_name("000001_01", NAME_R)["changed"] is True
    with closing(sqlite3.connect(str(db(home)))) as c:
        assert c.execute("SELECT name FROM names").fetchall() == [(NAME_R,)]
        assert c.execute("PRAGMA user_version").fetchone()[0] == sqlite_repo.SCHEMA_VERSION == 1


def test_an_extra_table_does_not_make_a_registry_damaged(home):
    # What an older program checks: every table of its own list is present.
    # The names table is extra for it and is simply not looked at.
    with closing(sqlite3.connect(str(db(home)))) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "names" in tables and set(sqlite_repo._TABLES) <= tables
    with SqliteRepository(db(home)) as repo:
        assert repo.get_meta("schema_version") == "1"


# --------------------------------------------------------------------------- service

def test_service_set_name_result_shape(home):
    with RegistryService.open(home) as svc:
        r = svc.set_name(" 000001_01 ", f"  {NAME_R} ")
        assert set(r) == {"core_stereo_id", "name", "changed", "warnings"}
        assert r["core_stereo_id"] == "000001_01" and r["changed"] is True
        assert r["name"]["text"] == NAME_R
        assert set(r["name"]) == {"text", "updated_at", "source"} and r["name"]["source"] == "user"
        again = svc.set_name("000001_01", NAME_R)
        assert again["changed"] is False and again["name"] == r["name"]
        removed = svc.set_name("000001_01", None)
        assert removed == {"core_stereo_id": "000001_01", "name": None, "changed": True,
                           "warnings": []}
        assert svc.get_name("000001_01") == {"core_stereo_id": "000001_01", "name": None}


@pytest.mark.parametrize("bad", ["", "   ", "two\nlines", "tab\there", "bell\x07",
                                 "c1\x85c2", "c1\x9bc2", "line\u2028sep", "n" * (protocol.MAX_NAME_TEXT + 1),
                                 5, ["x"]])
def test_service_refuses_bad_names(home, bad):
    with RegistryService.open(home) as svc, pytest.raises(RegistryError) as info:
        svc.set_name("000001_01", bad)
    assert info.value.code == "invalid_argument"
    assert events(home) == []


def test_service_accepts_long_unicode_names(home):
    text = "(2S,3R)-2-amino-3-hydroxy-N′-[(β-D-glucopyranosyl)]" + "x" * 900
    with RegistryService.open(home) as svc:
        assert svc.set_name("000002_01", text)["name"]["text"] == text


def test_service_unknown_variant(home):
    with RegistryService.open(home) as svc:
        with pytest.raises(NotFoundError):
            svc.set_name("000001_09", "x")
        with pytest.raises(NotFoundError):
            svc.get_name("000005_01")
        with pytest.raises(RegistryError) as info:
            svc.set_name("  ", "x")
        assert info.value.code == "invalid_argument"


def test_names_are_written_while_registering_is_blocked(home):
    with RegistryService.open(home) as svc:
        svc.repo.set_meta(FINGERPRINT_KEY, "rdkit=0;inchi=0;keyalg=0")
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as info:
            svc.register(smiles="CCCO", experiment_ref="9")
        assert info.value.code == "chemistry_version_changed"
        assert svc.set_name("000001_01", NAME_R)["changed"] is True


def test_names_are_written_while_the_database_is_older_than_the_journal(home):
    journal = home / "issued-ids.jsonl"
    old = home.parent / "old.sqlite"
    shutil.copy2(db(home), old)
    with RegistryService.open(home) as svc:
        svc.register(smiles="CCCO", experiment_ref="104")
    saved = journal.read_bytes()
    shutil.copy2(old, db(home))  # an older backup restored: it lacks 000003_01_001
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as info:
            svc.retire("000001_01_001", "x")
        assert info.value.code == "database_older_than_journal"
        assert svc.set_name("000001_01", NAME_R)["changed"] is True
    assert journal.read_bytes() == saved  # names never go to the issued-ID journal


def test_backup_runs_after_a_change_and_its_warning_is_reported(home, monkeypatch):
    calls = []

    def fake(repo, config, now=None):
        calls.append(1)
        return BackupOutcome(performed=False, warning="Backup failed: disk full")

    monkeypatch.setattr(backup_mod, "maybe_backup", fake)
    with RegistryService.open(home) as svc:
        r = svc.set_name("000001_01", NAME_R)
        assert r["warnings"] == ["Backup failed: disk full"] and calls == [1]
        r = svc.set_name("000001_01", NAME_R)       # unchanged: nothing written, no backup
        assert r["warnings"] == [] and calls == [1]


def test_names_in_analyze_and_find(home):
    with RegistryService.open(home) as svc:
        svc.set_name("000001_02", "(1S)-1-phenylethan-1-amine")
        a = svc.analyze(smiles=AMINE_S)
        assert a["name"]["text"] == "(1S)-1-phenylethan-1-amine"
        assert svc.analyze(smiles=AMINE_R)["name"] is None          # registered, no name
        assert svc.analyze(smiles="CCCCO")["name"] is None           # not registered
        f = svc.find_by_id("000001_01", with_svg=False)
    v = f["variants"][0]
    assert v["name"] is None and v["inchi"].startswith("InChI=1S/C8H11N/")
    assert v["properties"]["sum_formula"] == "C8H11N" and v["properties"]["mol_weight"] == 121.183
    o = f["other_variants"][0]
    assert o["name"]["text"] == "(1S)-1-phenylethan-1-amine" and o["inchi"].startswith("InChI=")
    assert "properties" not in o and "svg" not in o
    assert v["inchi"] != o["inchi"]


def test_register_reports_properties(tmp_path):
    with RegistryService.open(tmp_path / "h") as svc:
        r = svc.register(smiles="CCN.Cl", experiment_ref="1")
    assert r["structure"]["properties"]["sum_formula"] == "C2H8ClN"


# --------------------------------------------------------------------------- CLI

def test_cli_name(home, capsys):
    base = ["--home", str(home), "name", "000001_01"]
    assert main(base) == 0
    assert "(no name stored)" in capsys.readouterr().out
    assert main(base + [NAME_R]) == 0
    assert f"Name of 000001_01 stored: {NAME_R}" in capsys.readouterr().out
    assert main(base + [NAME_R]) == 0
    assert "unchanged" in capsys.readouterr().out
    assert main(base) == 0
    out = capsys.readouterr().out
    assert NAME_R in out and "stored 20" in out
    assert main(["--home", str(home), "--json", "name", "000001_01"]) == 0
    assert json.loads(capsys.readouterr().out)["name"]["text"] == NAME_R
    assert main(base + ["(1R)-1-phényl"]) == 0
    out = capsys.readouterr().out
    out.encode("ascii")
    assert "ph\\xe9nyl" in out
    assert main(base + ["--remove"]) == 0
    assert "removed" in capsys.readouterr().out
    assert main(base + ["--remove", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is False


def test_cli_name_errors(home, capsys):
    assert main(["--home", str(home), "name", "000009_01", "x"]) == 1
    assert "error [not_found]" in capsys.readouterr().err
    assert main(["--home", str(home), "name", "000001_01", "  "]) == 1
    assert "error [invalid_argument]" in capsys.readouterr().err
    assert main(["--home", str(home), "name", "000001_01", "x", "--remove"]) == 2


# --------------------------------------------------------------------------- host

def ask(home, op, params):
    body = json.dumps({"id": "n1", "v": 1, "op": op, "params": params}).encode("utf-8")
    return host.handle_message(body, HomeChoice(home, "host-config")).response


def test_host_set_name_round_trip(home):
    doc = ask(home, "set_name", {"core_stereo_id": "000001_01", "name": NAME_R})
    assert doc["ok"] is True and doc["id"] == "n1"
    assert doc["result"]["name"]["text"] == NAME_R and doc["result"]["changed"] is True
    assert ask(home, "find", {"id": "000001"})["result"]["variants"][0]["name"]["text"] == NAME_R
    assert ask(home, "analyze", {"smiles": AMINE_R})["result"]["name"]["text"] == NAME_R
    removed = ask(home, "set_name", {"core_stereo_id": "000001_01", "name": None})
    assert removed["ok"] and removed["result"]["name"] is None


def test_host_set_name_errors(home):
    missing = ask(home, "set_name", {"core_stereo_id": "000007_01", "name": "x"})
    assert missing["ok"] is False and missing["error"]["code"] == "not_found"
    bad = ask(home, "set_name", {"core_stereo_id": "000001_01", "name": "a\nb"})
    assert bad["ok"] is False and bad["error"]["code"] == "bad_request"
    no_key = ask(home, "set_name", {"core_stereo_id": "000001_01"})
    assert no_key["ok"] is False and no_key["error"]["code"] == "bad_request"


def test_host_never_creates_a_registry_for_set_name(tmp_path):
    nowhere = tmp_path / "nowhere"
    doc = ask(nowhere, "set_name", {"core_stereo_id": "000001_01", "name": "x"})
    assert doc["error"]["code"] == "registry_missing" and not nowhere.exists()
