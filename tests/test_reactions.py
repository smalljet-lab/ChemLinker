"""Reactions of experiments: storage (sqlite_repo), service ``set_reaction`` /
``reaction``, host ``set_reaction`` (in process and as a real process) and the
CLI ``reaction``.

A reaction is an annotation stored per experiment (the ELN's internal key),
mirrored from the ELN page: replaced as a whole when it differs, every change
an event (without the data), never part of a key and never in the journal.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from compound_registry import backup as backup_mod
from compound_registry import cli, host, sqlite_repo
from compound_registry.backup import BackupOutcome
from compound_registry.cli import main
from compound_registry.errors import NotFoundError, RegistryError
from compound_registry.host import HomeChoice
from compound_registry.service import RegistryService, clean_reaction
from compound_registry.sqlite_repo import SqliteRepository, canonical_json
from conftest import build_example_registry, example_data

ABC002 = example_data()["experiments"]["ABC-002"]
REF = ABC002["experiment_ref"]            # "4120"
URL = ABC002["experiment_url"]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)


@pytest.fixture
def home(tmp_path, capsys) -> Path:
    """The example registry WITHOUT stored reactions."""
    path = build_example_registry(tmp_path / "example", reactions=False)
    capsys.readouterr()
    return path


def db(home: Path) -> Path:
    return home / "registry.sqlite"


def reaction_events(home: Path) -> list[dict]:
    with closing(sqlite3.connect(db(home).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        rows = c.execute("SELECT event, full_id, details FROM events ORDER BY event_id").fetchall()
    out = []
    for event, full_id, details in rows:
        if event == sqlite_repo.REACTION_EVENT:
            assert full_id is None
            out.append(json.loads(details))
    return out


def stored_rows(home: Path) -> list[tuple]:
    with closing(sqlite3.connect(db(home).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        return c.execute("SELECT experiment_ref, experiment_code, experiment_url, data "
                         "FROM reactions ORDER BY experiment_ref").fetchall()


def reaction() -> dict:
    return copy.deepcopy(ABC002["reaction"])


# --------------------------------------------------------------------------- storage

def test_repo_set_get_unchanged_replace(home):
    data = reaction()
    with SqliteRepository(db(home)) as repo:
        assert repo.get_reaction(REF) is None
        record, changed = repo.set_reaction(REF, "ABC-002", URL, data)
        assert changed is True
        assert record["data"] == data and record["experiment_code"] == "ABC-002"
        assert record["experiment_url"] == URL and record["updated_at"].endswith("Z")
        text = canonical_json(data)
        assert record["digest"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert repo.get_reaction(f"  {REF} ") == record

        # Same content (also given as JSON text, keys in another order): nothing written.
        again, changed = repo.set_reaction(REF, "ABC-002", URL,
                                           json.dumps(data, sort_keys=False, indent=2))
        assert changed is False and again == record

        data["temperature"] = "40"
        new, changed = repo.set_reaction(REF, None, None, data)   # None keeps code and URL
        assert changed is True and new["data"]["temperature"] == "40"
        assert new["experiment_code"] == "ABC-002" and new["experiment_url"] == URL
        assert new["digest"] != record["digest"]
        listed = repo.list_reactions()
        assert [r["experiment_ref"] for r in listed] == [REF] and "data" not in listed[0]
        assert repo.list_reactions(with_data=True)[0]["data"] == data

    rows = stored_rows(home)
    assert rows == [(REF, "ABC-002", URL, canonical_json(data))]
    assert "\n" not in rows[0][3] and ", " not in rows[0][3].split('"name"')[0]
    events = reaction_events(home)
    assert len(events) == 2
    assert events[0] == {"experiment_ref": REF, "code": "ABC-002", "digest": record["digest"],
                         "replaced": False}
    assert events[1]["replaced"] is True and events[1]["digest"] == new["digest"]
    # The event names the experiment, it does not copy the data.
    assert all("reactants" not in json.dumps(e) for e in events)


def test_repo_a_new_code_alone_is_a_change(home):
    with SqliteRepository(db(home)) as repo:
        repo.set_reaction(REF, None, None, reaction())
        record, changed = repo.set_reaction(REF, "ABC-002", None, reaction())
        assert changed is True and record["experiment_code"] == "ABC-002"
        assert repo.set_reaction(REF, "ABC-002", None, reaction())[1] is False
    assert len(reaction_events(home)) == 2


@pytest.mark.parametrize("data", [[], "[1]", "not json", 5, None, {"x": float("nan")}])
def test_repo_refuses_data_that_is_not_a_json_object(home, data):
    with SqliteRepository(db(home)) as repo:
        with pytest.raises(RegistryError) as info:
            repo.set_reaction(REF, None, None, data)
        assert info.value.code == "invalid_argument"
        with pytest.raises(RegistryError):
            repo.set_reaction("  ", None, None, {})
    assert stored_rows(home) == [] and reaction_events(home) == []


def test_a_registry_from_before_reactions_opens_and_gets_the_table(home):
    with closing(sqlite3.connect(str(db(home)), isolation_level=None)) as c:
        c.execute("DROP TABLE reactions")
    assert "reactions" not in sqlite_repo._TABLES   # not required: not "damaged"
    with RegistryService.open(home) as svc:
        assert svc.doctor()["integrity"] == "ok"
        assert svc.set_reaction(REF, reaction())["changed"] is True
    assert [r[0] for r in stored_rows(home)] == [REF]


def test_reaction_events_do_not_disturb_the_consistency_check(home):
    with SqliteRepository(db(home)) as repo:
        for temperature in ("1", "2", "3"):
            data = reaction()
            data["temperature"] = temperature
            repo.set_reaction(REF, None, None, data)
    with SqliteRepository(db(home)) as repo:
        assert repo.integrity_check() == "ok"


# --------------------------------------------------------------------------- service

def test_service_set_reaction_and_read_it_back(home):
    with RegistryService.open(home) as svc:
        r = svc.set_reaction(REF, ABC002["reaction"], experiment_code="ABC-002",
                             experiment_url=URL)
        assert set(r) == {"experiment_ref", "changed", "updated_at", "warnings"}
        assert r["experiment_ref"] == REF and r["changed"] is True and r["warnings"] == []
        again = svc.set_reaction(f" {REF} ", ABC002["reaction"])
        assert again["changed"] is False and again["updated_at"] == r["updated_at"]

        for key in (REF, "ABC-002", "abc-002", " ABC-002 "):
            got = svc.reaction(key)
            assert set(got) == {"experiment_ref", "experiment_code", "experiment_url",
                                "updated_at", "reaction"}
            assert got["experiment_ref"] == REF and got["experiment_code"] == "ABC-002"
            assert got["reaction"] == ABC002["reaction"]
        with pytest.raises(NotFoundError):
            svc.reaction("ABC-001")          # registered, but no reaction stored
        with pytest.raises(NotFoundError):
            svc.reaction("ABC-999")
    assert len(reaction_events(home)) == 1


def test_service_keeps_the_eln_number_of_the_batches_when_none_is_given(home):
    with RegistryService.open(home) as svc:
        svc.set_reaction(REF, ABC002["reaction"])
        got = svc.reaction("ABC-002")
    assert got["experiment_code"] == "ABC-002" and got["experiment_url"] == URL


def test_service_completes_and_checks_the_reaction(home):
    sparse = {"reactants": [{"row": 1, "name": "  Ethanol "}], "reagents": [], "solvents": []}
    with RegistryService.open(home) as svc:
        svc.set_reaction(REF, sparse)
        stored = svc.reaction(REF)["reaction"]
        assert stored == clean_reaction(sparse)
        assert stored["reactants"][0] == {
            "row": 1, "name": "Ethanol", "formula": None, "cas": None,
            "source_experiment_ref": None, "source_experiment_code": None, "molfile": None,
            "actual": None, "planned": None}
        assert stored["temperature"] is None and stored["reference_amount"] is None
        for bad in ({"reactants": [], "reagents": []},                    # solvents missing
                    {**sparse, "extra": 1},                               # unknown key
                    {**sparse, "solvents": [{"name": "THF", "volume": -1}]},
                    {**sparse, "amounts_shown": "both"},
                    [], "x"):
            with pytest.raises(RegistryError) as info:
                svc.set_reaction(REF, bad)
            assert info.value.code == "invalid_argument", bad
        with pytest.raises(RegistryError) as info:
            svc.set_reaction(REF, sparse, experiment_url="javascript:alert(1)")
        assert info.value.code == "invalid_argument"
    assert len(reaction_events(home)) == 1


def test_service_refuses_an_experiment_without_registered_product(home):
    with RegistryService.open(home) as svc:
        with pytest.raises(NotFoundError) as info:
            svc.set_reaction("999999", ABC002["reaction"], experiment_code="ABC-077")
        assert "ABC-077" in info.value.message and "nothing was stored" in info.value.message
        with pytest.raises(RegistryError) as info:
            svc.set_reaction("  ", ABC002["reaction"])
        assert info.value.code == "invalid_argument"
    assert stored_rows(home) == []


def test_a_retired_batch_still_allows_its_reaction(home):
    with RegistryService.open(home) as svc:
        svc.retire("EX0004_01_001", "test")
        abc008 = example_data()["experiments"]["ABC-008"]
        assert svc.set_reaction(abc008["experiment_ref"], abc008["reaction"])["changed"] is True


def test_written_while_the_guards_block_registering_and_not_journalled(home):
    journal = home / "issued-ids.jsonl"
    old = home.parent / "old.sqlite"
    shutil.copy2(db(home), old)
    with RegistryService.open(home) as svc:
        svc.register(smiles="CCCCCCO", experiment_ref="104")
    saved = journal.read_bytes()
    shutil.copy2(old, db(home))
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as info:
            svc.retire("EX0001_01_001", "x")
        assert info.value.code == "database_older_than_journal"
        assert svc.set_reaction(REF, ABC002["reaction"])["changed"] is True
    assert journal.read_bytes() == saved   # reactions never go to the issued-ID journal


def test_backup_runs_after_a_change_and_its_warning_is_reported(home, monkeypatch):
    calls = []

    def fake(repo, config, now=None):
        calls.append(1)
        return BackupOutcome(performed=False, warning="Backup failed: disk full")

    monkeypatch.setattr(backup_mod, "maybe_backup", fake)
    with RegistryService.open(home) as svc:
        r = svc.set_reaction(REF, ABC002["reaction"])
        assert r["warnings"] == ["Backup failed: disk full"] and calls == [1]
        r = svc.set_reaction(REF, ABC002["reaction"])     # unchanged: no backup
        assert r["warnings"] == [] and calls == [1]


def test_a_backend_without_reactions_refuses(home):
    class Bare:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name in ("set_reaction", "get_reaction", "list_reactions"):
                raise AttributeError(name)
            return getattr(self._inner, name)

    with RegistryService.open(home) as svc:
        svc.repo = Bare(svc.repo)
        for call in (lambda: svc.set_reaction(REF, ABC002["reaction"]),
                     lambda: svc.reaction(REF), lambda: svc.route("EX0001_01", open=False)):
            with pytest.raises(RegistryError) as info:
                call()
            assert info.value.code == "not_supported"
        svc.repo = svc.repo._inner


# --------------------------------------------------------------------------- host

def ask(home, op, params):
    body = json.dumps({"id": "r1", "v": 1, "op": op, "params": params}).encode("utf-8")
    return host.handle_message(body, HomeChoice(home, "host-config")).response


def test_host_set_reaction_round_trip(home):
    params = {"experiment_ref": REF, "experiment_code": "ABC-002", "experiment_url": URL,
              "reaction": ABC002["reaction"]}
    doc = ask(home, "set_reaction", params)
    assert doc["ok"] is True and doc["id"] == "r1"
    assert doc["result"]["changed"] is True and doc["result"]["experiment_ref"] == REF
    assert ask(home, "set_reaction", params)["result"]["changed"] is False
    with RegistryService.open(home) as svc:
        assert svc.reaction("ABC-002")["reaction"] == ABC002["reaction"]


def test_host_set_reaction_errors(home):
    base = {"experiment_ref": REF, "reaction": ABC002["reaction"]}
    missing = ask(home, "set_reaction", {**base, "experiment_ref": "424242"})
    assert missing["ok"] is False and missing["error"]["code"] == "not_found"
    for params in ({"experiment_ref": REF},
                   {**base, "reaction": {**ABC002["reaction"], "time": "2 h"}},
                   {**base, "unknown": 1}):
        res = ask(home, "set_reaction", params)
        assert res["ok"] is False and res["error"]["code"] == "bad_request", params
    assert reaction_events(home) == []


def test_host_never_creates_a_registry_for_reactions(tmp_path):
    nowhere = tmp_path / "nowhere"
    for op, params in (("set_reaction", {"experiment_ref": REF, "reaction": ABC002["reaction"]}),
                       ("route", {"core_stereo_id": "EX0001_01"})):
        assert ask(nowhere, op, params)["error"]["code"] == "registry_missing"
    assert not nowhere.exists()


def test_host_process_set_reaction(home):
    """The real host process: frames over pipes, every reaction of the example data stored."""
    from test_host_subprocess import MODULE, frame, parse_frames, request

    env = dict(__import__("os").environ)
    env["COMPOUND_REGISTRY_HOME"] = str(home)
    data = b"".join(
        frame(request("set_reaction", {"experiment_ref": e["experiment_ref"],
                                       "experiment_code": e["experiment_code"],
                                       "experiment_url": e["experiment_url"],
                                       "reaction": e["reaction"]}, request_id=i))
        for i, e in enumerate(example_data()["experiments"].values(), start=1))
    p = subprocess.run(MODULE, input=data, capture_output=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    docs = parse_frames(p.stdout)
    assert [d["id"] for d in docs] == [1, 2, 3, 4, 5, 6]
    assert all(d["ok"] and d["result"]["changed"] for d in docs), docs
    assert len(stored_rows(home)) == 6
    log = (home / "logs" / "host.log").read_text(encoding="utf-8")
    assert "op=set_reaction" in log and "Phenylethylamine" not in log and "M  END" not in log


# --------------------------------------------------------------------------- CLI

def test_cli_reaction(home, capsys):
    with RegistryService.open(home) as svc:
        svc.set_reaction(REF, ABC002["reaction"])
    base = ["--home", str(home)]
    assert main(base + ["reaction", "ABC-002"]) == 0
    text = capsys.readouterr().out
    assert text.startswith(f"ABC-002  (internal key {REF}, stored ")
    assert "1. ABC-001 (C13H10O)  [from ABC-001, key 4110]" in text
    assert "actual: eq 1, 8.232 mmol, 1500 mg" in text
    assert "2. (S)-(-)-1-Phenylethylamine (C8H11N, 2627-86-3)" in text
    assert "Reagents: none" in text and "Solvents: EtOH - shared stock 25 ml" in text
    assert "Reference amount: 8.232 mmol" in text and "Temperature: -" in text

    assert main(base + ["--json", "reaction", REF]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["experiment_code"] == "ABC-002" and doc["reaction"] == ABC002["reaction"]

    assert main(base + ["reaction", "ABC-003"]) == 1
    assert "error [not_found]" in capsys.readouterr().err
    assert main(base + ["--json", "reaction", "ABC-003"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
