"""Yields of batches: storage (sqlite_repo), service, host ``set_yield`` / ``yields``,
CLI ``yields`` and the export columns.

A yield is an annotation stored per batch (one product of one experiment),
mirrored from the ELN: it may change or disappear, every change is an event,
and it is never part of a key.
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from rdkit import Chem

from compound_registry import backup as backup_mod
from compound_registry import cli, host, protocol, sqlite_repo
from compound_registry.backup import BackupOutcome
from compound_registry.cli import main
from compound_registry.errors import NotFoundError, RegistryError
from compound_registry.host import HomeChoice
from compound_registry.models import YieldRecord
from compound_registry.service import FINGERPRINT_KEY, RegistryService, clean_yield
from compound_registry.sqlite_repo import SqliteRepository

AMINE_R = "C[C@H](N)c1ccccc1"
ISOPROPANOL = "CC(C)O"
ETHANOL = "CCO"
PROPANOL = "CCCO"
BUTANOL = "CCCCO"
PENTANOL = "CCCCCO"

# The fixture registry, in the order the yields list shows it (asc).
ORDER_ASC = [
    "000005_01_001",   # ABC-006, product 1
    "000002_01_001",   # ABC-9,   product 1
    "000003_01_001",   # ABC-9,   product 2
    "000001_01_001",   # ABC-10,  product 1
    "000004_01_001",   # no ELN number, key 99
    "000006_01_001",   # no ELN number, key 5001
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)


@pytest.fixture
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    with RegistryService.open(path) as svc:
        svc.register(smiles=AMINE_R, experiment_ref="101", experiment_code="ABC-10")  # 000001_01_001
        svc.register(smiles=ISOPROPANOL, experiment_ref="102", experiment_code="ABC-9")   # 000002_01_001
        svc.register(smiles=ETHANOL, experiment_ref="102", experiment_code="ABC-9",
                     product_index=2)                                                  # 000003_01_001
        svc.register(smiles=PROPANOL, experiment_ref="99")                             # 000004_01_001
        svc.register(smiles=BUTANOL, experiment_ref="103", experiment_code="ABC-006")  # 000005_01_001
        svc.register(smiles=PENTANOL, experiment_ref="5001")                           # 000006_01_001
    return path


def db(home: Path) -> Path:
    return home / "registry.sqlite"


def events(home: Path, kinds=sqlite_repo.YIELD_EVENTS) -> list[tuple[str, dict]]:
    with closing(sqlite3.connect(db(home).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        rows = c.execute("SELECT event, full_id, details FROM events ORDER BY event_id").fetchall()
    out = []
    for event, full_id, details in rows:
        if event in kinds:
            assert full_id is None   # like names and spectra: the ID is in the details
            out.append((event, json.loads(details)))
    return out


# --------------------------------------------------------------------------- storage

def test_repo_set_change_remove(home):
    with SqliteRepository(db(home)) as repo:
        assert repo.get_yields() == {}
        rec, changed = repo.set_yield(" 000001_01_001 ", 76.2, None)
        assert changed and rec == YieldRecord("000001_01_001", 76.2, None, rec.updated_at)
        sqlite_repo.parse_timestamp(rec.updated_at)
        same, changed = repo.set_yield("000001_01_001", 76.2, None)
        assert not changed and same == rec
        rec2, changed = repo.set_yield("000001_01_001", 76.2, 92)
        assert changed and (rec2.isolated, rec2.analytical) == (76.2, 92.0)
        assert repo.get_yields() == {"000001_01_001": rec2}
        gone, changed = repo.set_yield("000001_01_001", None, None)
        assert changed and gone is None and repo.get_yields() == {}
        nothing, changed = repo.set_yield("000001_01_001", None, None)
        assert not changed and nothing is None
    assert events(home) == [
        ("yield_set", {"full_id": "000001_01_001", "isolated": 76.2, "analytical": None,
                       "previous_isolated": None, "previous_analytical": None}),
        ("yield_set", {"full_id": "000001_01_001", "isolated": 76.2, "analytical": 92.0,
                       "previous_isolated": 76.2, "previous_analytical": None}),
        ("yield_removed", {"full_id": "000001_01_001", "isolated": None, "analytical": None,
                           "previous_isolated": 76.2, "previous_analytical": 92.0}),
    ]


def test_repo_refuses_unknown_ids_and_bad_values(home):
    with SqliteRepository(db(home)) as repo:
        for bad in ("000009_01_001", "000001_01", "000001_01_002", "1"):
            with pytest.raises(NotFoundError):
                repo.set_yield(bad, 50, None)
        for value in (-1, math.nan, math.inf, True, "50"):
            with pytest.raises(RegistryError) as info:
                repo.set_yield("000001_01_001", value, None)
            assert info.value.code == "invalid_argument"
    assert events(home) == []


def test_repo_reads_yields_of_given_ids(home):
    with SqliteRepository(db(home)) as repo:
        repo.set_yield("000001_01_001", 10, None)
        repo.set_yield("000002_01_001", None, 20)
        assert set(repo.get_yields(["000002_01_001", "000003_01_001", "nope"])) == {
            "000002_01_001"}
        assert repo.get_yields([]) == {}
        rows = {r["full_id"]: r for r in repo.batch_yields()}
    assert len(rows) == 6
    assert rows["000001_01_001"]["yield"].isolated == 10.0
    assert rows["000003_01_001"]["yield"] is None
    assert rows["000002_01_001"]["product_index"] == 1 and rows["000003_01_001"]["product_index"] == 2
    assert rows["000002_01_001"]["core_stereo_id"] == "000002_01"
    assert rows["000004_01_001"]["experiment_code"] is None and rows["000004_01_001"]["retired"] is False


def test_yields_table_constraints(home):
    with closing(sqlite3.connect(str(db(home)), isolation_level=None)) as c:
        c.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):   # unknown batch
            c.execute("INSERT INTO yields VALUES ('000099_01_001', 1, NULL, '2026-11-17T00:00:00Z')")
        with pytest.raises(sqlite3.IntegrityError):   # both null: no row at all
            c.execute("INSERT INTO yields VALUES ('000001_01_001', NULL, NULL, '2026-11-17T00:00:00Z')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO yields VALUES ('000001_01_001', -1, NULL, '2026-11-17T00:00:00Z')")


def test_a_registry_from_before_yields_opens_and_gets_the_table(home):
    with closing(sqlite3.connect(str(db(home)), isolation_level=None)) as c:
        c.execute("DROP TABLE yields")
    assert "yields" not in sqlite_repo._TABLES   # not required: not "damaged"
    with RegistryService.open(home) as svc:
        assert svc.doctor()["integrity"] == "ok"
        assert svc.set_yield("000001_01_001", "101", 1, 76.2, None)["changed"] is True
    with closing(sqlite3.connect(str(db(home)))) as c:
        assert c.execute("SELECT full_id, isolated FROM yields").fetchall() == [
            ("000001_01_001", 76.2)]
        assert c.execute("PRAGMA user_version").fetchone()[0] == sqlite_repo.SCHEMA_VERSION == 1


def test_yield_events_do_not_disturb_the_consistency_check(home):
    with SqliteRepository(db(home)) as repo:
        for _ in range(3):
            repo.set_yield("000001_01_001", 50, None)
            repo.set_yield("000001_01_001", None, None)
        repo.set_yield("000002_01_001", 70, None)
    with SqliteRepository(db(home)) as repo:
        assert set(repo.get_yields()) == {"000002_01_001"}
        assert repo.integrity_check() == "ok"


# --------------------------------------------------------------------------- service: set_yield

def test_service_set_yield_result_shape(home):
    with RegistryService.open(home) as svc:
        r = svc.set_yield(" 000001_01_001 ", " 101 ", 1, 81.456, 90)
        assert set(r) == {"id", "yield", "changed", "warnings"}
        assert r["id"] == "000001_01_001" and r["changed"] is True and r["warnings"] == []
        assert set(r["yield"]) == {"isolated", "analytical", "updated_at"}
        assert r["yield"]["isolated"] == 81.46 and r["yield"]["analytical"] == 90.0
        again = svc.set_yield("000001_01_001", "101", 1, 81.46, 90.0)
        assert again["changed"] is False and again["yield"] == r["yield"]
        removed = svc.set_yield("000001_01_001", "101", 1, None, None)
        assert removed == {"id": "000001_01_001", "yield": None, "changed": True, "warnings": []}
        assert svc.set_yield("000001_01_001", "101", 1, None, None)["changed"] is False
    assert [e for e, _ in events(home)] == ["yield_set", "yield_removed"]


@pytest.mark.parametrize("value, stored", [(0, 0.0), (1000, 1000.0), (99.999, 100.0),
                                           (12.346, 12.35), (12.344, 12.34), (-0.0, 0.0),
                                           (76.2, 76.2)])
def test_clean_yield_accepts_and_rounds(value, stored):
    result = clean_yield("isolated", value)
    assert result == stored and isinstance(result, float) and math.copysign(1, result) == 1


@pytest.mark.parametrize("bad", [-0.01, 1000.01, math.nan, math.inf, -math.inf, True, False,
                                 "76.2", [76.2]])
def test_service_refuses_bad_values(home, bad):
    with RegistryService.open(home) as svc:
        for args in ((bad, None), (None, bad)):
            with pytest.raises(RegistryError) as info:
                svc.set_yield("000001_01_001", "101", 1, *args)
            assert info.value.code == "invalid_argument"
    assert events(home) == []


@pytest.mark.parametrize("ref, index", [("102", 1), ("101", 2), ("1010", 1), ("10", 1)])
def test_yield_mismatch_writes_nothing(home, ref, index):
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as info:
            svc.set_yield("000001_01_001", ref, index, 76.2, None)
        assert info.value.code == "yield_mismatch"
        message = info.value.message
        assert "000001_01_001" in message and "ABC-10" in message
        assert f"product {index}" in message and ref in message and "nothing was stored" in message
        assert svc.yields()["items"][3]["yield"] is None
    assert events(home) == []


def test_service_unknown_id_and_bad_arguments(home):
    with RegistryService.open(home) as svc:
        with pytest.raises(NotFoundError):
            svc.set_yield("000001_01_009", "101", 1, 5, None)
        for args in (("  ", "101", 1), ("000001_01_001", "  ", 1), ("000001_01_001", "101", 0),
                     ("000001_01_001", "101", True), ("000001_01_001", None, 1)):
            with pytest.raises(RegistryError) as info:
                svc.set_yield(*args, 5, None)
            assert info.value.code == "invalid_argument"


def test_a_retired_batch_may_get_a_yield(home):
    with RegistryService.open(home) as svc:
        svc.retire("000004_01_001", "wrong product")
        r = svc.set_yield("000004_01_001", "99", 1, 12, None)
        assert r["changed"] is True and r["yield"]["isolated"] == 12.0


def test_yields_are_written_while_registering_is_blocked(home):
    with RegistryService.open(home) as svc:
        svc.repo.set_meta(FINGERPRINT_KEY, "rdkit=0;inchi=0;keyalg=0")
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as info:
            svc.register(smiles="CCCCCCO", experiment_ref="9")
        assert info.value.code == "chemistry_version_changed"
        assert svc.set_yield("000001_01_001", "101", 1, 50, None)["changed"] is True


def test_yields_are_written_while_the_database_is_older_than_the_journal(home):
    journal = home / "issued-ids.jsonl"
    old = home.parent / "old.sqlite"
    shutil.copy2(db(home), old)
    with RegistryService.open(home) as svc:
        svc.register(smiles="CCCCCCO", experiment_ref="104")
    saved = journal.read_bytes()
    shutil.copy2(old, db(home))
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as info:
            svc.retire("000001_01_001", "x")
        assert info.value.code == "database_older_than_journal"
        assert svc.set_yield("000001_01_001", "101", 1, 50, None)["changed"] is True
    assert journal.read_bytes() == saved  # yields never go to the issued-ID journal


def test_backup_runs_after_a_change_and_its_warning_is_reported(home, monkeypatch):
    calls = []

    def fake(repo, config, now=None):
        calls.append(1)
        return BackupOutcome(performed=False, warning="Backup failed: disk full")

    monkeypatch.setattr(backup_mod, "maybe_backup", fake)
    with RegistryService.open(home) as svc:
        r = svc.set_yield("000001_01_001", "101", 1, 50, None)
        assert r["warnings"] == ["Backup failed: disk full"] and calls == [1]
        r = svc.set_yield("000001_01_001", "101", 1, 50, None)   # unchanged: no backup
        assert r["warnings"] == [] and calls == [1]


# --------------------------------------------------------------------------- batch objects

def test_batch_objects_carry_the_yield(home):
    with RegistryService.open(home) as svc:
        stored = svc.set_yield("000002_01_001", "102", 1, 76.2, None)["yield"]
        a = svc.analyze(smiles=ISOPROPANOL)
        assert [b["yield"] for b in a["batches"]] == [stored]
        conflict = svc.analyze(smiles="CCCCCCCO", experiment_ref="102", product_index=1)
        assert conflict["slot_conflict"]["full_id"] == "000002_01_001"
        assert conflict["slot_conflict"]["yield"] == stored
        assert svc.analyze(smiles="CCCCCCCO")["slot_conflict"] is None

        assert svc.register(smiles=ISOPROPANOL, experiment_ref="102")["batch"]["yield"] == stored
        new = svc.register(smiles=ISOPROPANOL, experiment_ref="777")
        assert new["created"] is True and new["batch"]["yield"] is None

        f = svc.find_by_id("000002", with_svg=False)
        batches = {b["full_id"]: b for b in f["variants"][0]["batches"]}
        assert batches["000002_01_001"]["yield"] == stored and batches["000002_01_002"]["yield"] is None
        exp = {e["experiment_ref"]: e for e in f["experiments"]}
        assert exp["102"]["batches"][0]["yield"] == stored
        assert exp["777"]["batches"][0]["yield"] is None

        retired = svc.retire("000002_01_001", "mix-up")
        assert retired["batch"]["yield"] == stored
        # Other callers of batch dicts are unchanged.
        assert "yield" not in svc.get("000002_01_001")
        assert all("yield" not in b for b in svc.experiment("102"))


# --------------------------------------------------------------------------- service: yields listing

def test_listing_order_and_shape(home):
    with RegistryService.open(home) as svc:
        svc.set_yield("000001_01_001", "101", 1, 76.2, None)
        svc.set_name("000002_01", "propan-2-ol")
        r = svc.yields()
    assert set(r) == {"total", "registry_total", "offset", "limit", "items", "warnings"}
    assert r["total"] == r["registry_total"] == 6
    assert r["offset"] == 0 and r["limit"] == protocol.DEFAULT_YIELD_PAGE_SIZE
    assert [i["full_id"] for i in r["items"]] == ORDER_ASC
    row = r["items"][3]
    assert set(row) == {"experiment_ref", "experiment_code", "experiment_url", "product_index",
                        "full_id", "core_stereo_id", "name", "yield", "created_at", "retired"}
    assert row["experiment_code"] == "ABC-10" and row["experiment_ref"] == "101"
    assert row["yield"]["isolated"] == 76.2 and row["yield"]["analytical"] is None
    assert row["core_stereo_id"] == "000001_01" and row["retired"] is False
    assert r["items"][1]["name"] == "propan-2-ol" and row["name"] is None
    assert r["items"][2]["product_index"] == 2

    with RegistryService.open(home) as svc:
        desc = svc.yields(order="desc")
    assert [i["full_id"] for i in desc["items"]] == ORDER_ASC[::-1]


def test_listing_text_filter(home):
    with RegistryService.open(home) as svc:
        svc.set_name("000001_01", "(1R)-1-Phenylethan-1-amine")

        def ids(**kw):
            return [i["full_id"] for i in svc.yields(**kw)["items"]]

        assert ids(text="abc-9") == ["000002_01_001", "000003_01_001"]
        assert ids(text="  ABC-1  ") == ["000001_01_001"]          # stripped, ignoring case
        assert ids(text="phenylETHAN") == ["000001_01_001"]        # the name
        assert ids(text="04_01") == ["000004_01_001"]              # part of a full ID
        assert ids(text="5001") == ["000006_01_001"]               # the internal key, equal
        assert ids(text="500") == []                               # ... not a part of it
        r = svc.yields(text="abc")
        assert r["total"] == 4 and r["registry_total"] == 6
        assert ids(text="   ") == ORDER_ASC                         # blank: no filter


def test_listing_only_recorded_paging_and_retired(home):
    with RegistryService.open(home) as svc:
        svc.set_yield("000003_01_001", "102", 2, None, 40)
        svc.set_yield("000004_01_001", "99", 1, 60, None)
        svc.retire("000004_01_001", "wrong")
        r = svc.yields(only_recorded=True)
        assert [i["full_id"] for i in r["items"]] == ["000003_01_001", "000004_01_001"]
        assert r["total"] == 2 and r["registry_total"] == 6
        assert r["items"][1]["retired"] is True
        page = svc.yields(offset=2, limit=3)
        assert [i["full_id"] for i in page["items"]] == ORDER_ASC[2:5]
        assert page["total"] == 6 and page["offset"] == 2 and page["limit"] == 3
        assert svc.yields(offset=10)["items"] == []
        assert len(svc.yields(limit=None)["items"]) == 6
        for kw in ({"order": "up"}, {"offset": -1}, {"limit": 0}, {"limit": True},
                   {"only_recorded": "yes"}, {"text": 5}):
            with pytest.raises(RegistryError) as info:
                svc.yields(**kw)
            assert info.value.code == "invalid_argument"


def test_listing_shows_the_newest_eln_number_of_an_experiment(home):
    with RegistryService.open(home) as svc:
        svc.register(smiles="CCCCCCO", experiment_ref="99", experiment_code="ABC-001",
                     product_index=2)
        rows = [i for i in svc.yields()["items"] if i["experiment_ref"] == "99"]
    assert [(i["full_id"], i["experiment_code"]) for i in rows] == [
        ("000004_01_001", "ABC-001"), ("000007_01_001", "ABC-001")]


def test_listing_of_an_empty_registry(tmp_path):
    with RegistryService.open(tmp_path / "empty") as svc:
        r = svc.yields()
    assert r["items"] == [] and r["total"] == r["registry_total"] == 0


# --------------------------------------------------------------------------- export

def test_export_columns_carry_the_yields(home, tmp_path):
    with RegistryService.open(home) as svc:
        svc.set_yield("000001_01_001", "101", 1, 76.2, None)
        svc.set_yield("000002_01_001", "102", 1, 12.25, 30)
        svc.export("csv", tmp_path / "y.csv")
        svc.export("sdf", tmp_path / "y.sdf")
    with open(tmp_path / "y.csv", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames[-2:] == ["yield_isolated", "yield_analytical"]
        rows = {r["full_id"]: r for r in reader}
    assert (rows["000001_01_001"]["yield_isolated"], rows["000001_01_001"]["yield_analytical"]) \
        == ("76.2", "")
    assert (rows["000002_01_001"]["yield_isolated"], rows["000002_01_001"]["yield_analytical"]) \
        == ("12.25", "30.0")
    assert rows["000003_01_001"]["yield_isolated"] == ""
    mols = {m.GetProp("full_id"): m for m in Chem.SDMolSupplier(str(tmp_path / "y.sdf"))}
    assert mols["000001_01_001"].GetProp("yield_isolated") == "76.2"
    assert not mols["000001_01_001"].HasProp("yield_analytical")
    assert not mols["000003_01_001"].HasProp("yield_isolated")


# --------------------------------------------------------------------------- CLI

def test_cli_yields(home, capsys):
    with RegistryService.open(home) as svc:
        svc.set_yield("000001_01_001", "101", 1, 76.2, None)
        svc.set_name("000001_01", "(1R)-1-phényl")
    assert main(["--home", str(home), "yields"]) == 0
    out = capsys.readouterr().out
    out.encode("ascii")
    lines = out.splitlines()
    assert lines[0] == "6 batch(es)"
    assert lines[1].split() == ["ELN", "number", "Product", "Full", "ID", "Isolated", "%",
                                "Analytical", "%", "Name"]
    assert [line.split()[0] for line in lines[2:6]] == ["ABC-006", "ABC-9", "ABC-9", "ABC-10"]
    assert lines[5].split() == ["ABC-10", "1", "000001_01_001", "76.2", "-", "(1R)-1-ph\\xe9nyl"]
    assert lines[6].split()[:3] == ["(key", "99)", "1"]

    assert main(["--home", str(home), "yields", "--only-recorded"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "1 of 6 batch(es) listed" and len(lines) == 3

    assert main(["--home", str(home), "yields", "--text", "abc-9", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [i["full_id"] for i in doc["items"]] == ["000002_01_001", "000003_01_001"]
    assert doc["limit"] == 2

    assert main(["--home", str(home), "yields", "--text", "nothing-like-this"]) == 0
    assert capsys.readouterr().out.splitlines() == ["0 of 6 batch(es) listed"]


def test_cli_yields_shows_retired_rows(home, capsys):
    with RegistryService.open(home) as svc:
        svc.retire("000004_01_001", "wrong")
    assert main(["--home", str(home), "yields", "--text", "000004"]) == 0
    assert "000004_01_001 [RETIRED]" in capsys.readouterr().out


# --------------------------------------------------------------------------- host

def ask(home, op, params):
    body = json.dumps({"id": "y1", "v": 1, "op": op, "params": params}).encode("utf-8")
    return host.handle_message(body, HomeChoice(home, "host-config")).response


def yield_params(**overrides):
    params = {"id": "000001_01_001", "experiment_ref": "101", "product_index": 1,
              "isolated": 76.2, "analytical": None}
    params.update(overrides)
    return params


def test_host_set_yield_round_trip(home):
    doc = ask(home, "set_yield", yield_params(isolated=81.456))
    assert doc["ok"] is True and doc["id"] == "y1"
    assert doc["result"]["id"] == "000001_01_001" and doc["result"]["changed"] is True
    assert doc["result"]["yield"]["isolated"] == 81.46
    stored = doc["result"]["yield"]

    listing = ask(home, "yields", {"only_recorded": True})["result"]
    assert [i["full_id"] for i in listing["items"]] == ["000001_01_001"]
    assert listing["items"][0]["yield"] == stored and listing["registry_total"] == 6

    a = ask(home, "analyze", {"smiles": AMINE_R})["result"]
    assert a["batches"][0]["yield"] == stored
    f = ask(home, "find", {"id": "000001_01_001"})["result"]
    assert f["variants"][0]["batches"][0]["yield"] == stored
    assert f["experiments"][0]["batches"][0]["yield"] == stored

    removed = ask(home, "set_yield", yield_params(isolated=None))
    assert removed["ok"] and removed["result"]["yield"] is None and removed["result"]["changed"]


def test_host_yields_params(home):
    doc = ask(home, "yields", {})
    assert doc["ok"] and [i["full_id"] for i in doc["result"]["items"]] == ORDER_ASC
    assert doc["result"]["limit"] == protocol.DEFAULT_YIELD_PAGE_SIZE
    doc = ask(home, "yields", {"text": "ABC", "order": "desc", "offset": 1, "limit": 2})
    assert [i["full_id"] for i in doc["result"]["items"]] == ["000003_01_001", "000002_01_001"]
    assert doc["result"]["total"] == 4
    for bad in ({"limit": protocol.MAX_YIELD_PAGE_SIZE + 1}, {"order": "sideways"},
                {"only_recorded": 1}, {"unknown": 1}):
        res = ask(home, "yields", bad)
        assert res["ok"] is False and res["error"]["code"] == "bad_request"


def test_host_set_yield_errors(home):
    missing = ask(home, "set_yield", yield_params(id="000009_01_001"))
    assert missing["ok"] is False and missing["error"]["code"] == "not_found"
    mismatch = ask(home, "set_yield", yield_params(product_index=2))
    assert mismatch["ok"] is False and mismatch["error"]["code"] == "yield_mismatch"
    assert "000001_01_001" in mismatch["error"]["message"]
    for params in (yield_params(isolated=1000.5), yield_params(isolated=-1),
                   yield_params(isolated="81"), yield_params(analytical=True)):
        res = ask(home, "set_yield", params)
        assert res["ok"] is False and res["error"]["code"] == "bad_request"
    no_key = yield_params()
    del no_key["analytical"]
    assert ask(home, "set_yield", no_key)["error"]["code"] == "bad_request"
    no_ref = yield_params()
    del no_ref["experiment_ref"]
    assert ask(home, "set_yield", no_ref)["error"]["code"] == "bad_request"
    assert events(home) == []


def test_host_never_creates_a_registry_for_yields(tmp_path):
    nowhere = tmp_path / "nowhere"
    for op, params in (("set_yield", yield_params()), ("yields", {})):
        doc = ask(nowhere, op, params)
        assert doc["error"]["code"] == "registry_missing"
    assert not nowhere.exists()
