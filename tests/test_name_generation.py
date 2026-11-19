"""Generated names: storage of ``source``, service ``generate_name`` / ``generate_names``,
host op ``generate_name``, CLI ``name --generate`` and ``generate-names``.

ChemDraw is never started: the service gets a fake asker, and where the
service is created inside the CLI or the host, ``chemdraw.name_molfile`` is
replaced by a fake.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from compound_registry import backup as backup_mod
from compound_registry import chem, chemdraw, cli, host, sqlite_repo
from compound_registry.backup import BackupOutcome
from compound_registry.chem import _inchi, _parse_molblock
from compound_registry.chemdraw import ChemDrawAnswer, ChemDrawError
from compound_registry.cli import main
from compound_registry.errors import NotFoundError, RegistryError
from compound_registry.host import HomeChoice
from compound_registry.models import NameRecord
from compound_registry.service import FINGERPRINT_KEY, RegistryService
from compound_registry.sqlite_repo import SqliteRepository

AMINE_R = "C[C@H](N)c1ccccc1"
AMINE_S = "C[C@@H](N)c1ccccc1"
ETHANOL = "CCO"
MIXED = "C[C@@H](O)[C@H](N)C(=O)O |&1:1|"

NAME_R = "(R)-1-phenylethan-1-amine"
NAME_S = "(S)-1-phenylethan-1-amine"
NAME_RAC = "rac-(R)-1-phenylethan-1-amine"
NAME_ETHANOL = "ethanol"
UNNAMED = "000003_01"    # the mixed variant: never named automatically


def inchi_of(molfile: str) -> str:
    return _inchi(_parse_molblock(molfile))[0]


INCHI_S = chem.normalize_smiles(AMINE_S).inchi


class FakeChemDraw:
    """Answers like ChemDraw would for the registry's test compounds."""

    def __init__(self, names: dict[str, str] | None = None, error: ChemDrawError | None = None):
        self.calls: list[str] = []
        self.error = error
        self.names = names

    def __call__(self, molfile: str, **_kwargs) -> ChemDrawAnswer:
        self.calls.append(molfile)
        if self.error is not None:
            raise self.error
        inchi = inchi_of(molfile)
        if self.names is not None:
            name = self.names.get(inchi, "")
        elif "/C2H6O/" in inchi:
            name = NAME_ETHANOL
        elif inchi == INCHI_S:
            name = NAME_S
        else:
            name = NAME_R
        if "MDLV30/STERAC" in molfile:
            name = "rac-" + name
        elif "MDLV30/STEREL" in molfile:
            name = "rel-" + name
        return ChemDrawAnswer(name=name, inchi=inchi, atoms=9)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)


@pytest.fixture
def home(tmp_path) -> Path:
    """000001_01 (R)-amine, 000001_02 (S)-amine, 000001_03 racemic amine,
    000002_01 ethanol, 000003_01 a mixed AND/ABS variant."""
    path = tmp_path / "home"
    with RegistryService.open(path) as svc:
        svc.register(smiles=AMINE_R, experiment_ref="101")
        svc.register(smiles=AMINE_S, experiment_ref="102")
        svc.register(smiles=AMINE_R, mode="rac", experiment_ref="103")
        svc.register(smiles=ETHANOL, experiment_ref="104")
        svc.register(smiles=MIXED, experiment_ref="105")
    return path


@pytest.fixture
def fake(monkeypatch) -> FakeChemDraw:
    """Replaces the installed ChemDraw for services created by the CLI / host."""
    instance = FakeChemDraw()
    monkeypatch.setattr(chemdraw, "name_molfile", instance)
    return instance


@pytest.fixture
def backups(monkeypatch) -> list[int]:
    calls: list[int] = []

    def counting(repo, config, now=None):
        calls.append(1)
        return BackupOutcome(performed=False)

    monkeypatch.setattr(backup_mod, "maybe_backup", counting)
    return calls


def db(home: Path) -> Path:
    return home / "registry.sqlite"


def name_events(home: Path) -> list[tuple[str, dict]]:
    with closing(sqlite3.connect(db(home).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        rows = c.execute("SELECT event, details FROM events ORDER BY event_id").fetchall()
    return [(e, json.loads(d)) for e, d in rows if e in sqlite_repo.NAME_EVENTS]


def service(home: Path, asker=None) -> RegistryService:
    svc = RegistryService.open(home)
    svc.name_asker = asker
    return svc


# --------------------------------------------------------------------------- storage

def test_new_registry_has_the_source_column(home):
    with closing(sqlite3.connect(str(db(home)))) as c:
        columns = {r[1]: r for r in c.execute("PRAGMA table_info(names)")}
    assert "source" in columns
    assert columns["source"][2] == "TEXT" and columns["source"][3] == 1   # NOT NULL
    assert columns["source"][4] == "'user'"


def test_source_is_stored_and_read_back(home):
    with SqliteRepository(db(home)) as repo:
        rec, changed = repo.set_name("000001_01", NAME_R, source="chemdraw")
        assert changed and rec.source == "chemdraw" and rec.text == NAME_R
        assert repo.get_name("000001_01") == rec
        assert repo.get_names()["000001_01"].source == "chemdraw"
        # Same text, other source: a change.
        rec2, changed = repo.set_name("000001_01", NAME_R, source="user")
        assert changed and rec2.source == "user"
        # Same pair: nothing written.
        same, changed = repo.set_name("000001_01", f" {NAME_R} ")
        assert not changed and same == rec2
        # Removing ignores the source.
        gone, changed = repo.set_name("000001_01", None, source="chemdraw")
        assert changed and gone is None
        assert repo.set_name("000001_01", None) == (None, False)
    assert name_events(home) == [
        ("name_set", {"core_stereo_id": "000001_01", "name": NAME_R, "previous": None,
                      "source": "chemdraw"}),
        ("name_set", {"core_stereo_id": "000001_01", "name": NAME_R, "previous": NAME_R,
                      "source": "user"}),
        ("name_removed", {"core_stereo_id": "000001_01", "name": None, "previous": NAME_R}),
    ]


def test_default_source_is_user(home):
    with SqliteRepository(db(home)) as repo:
        rec, _ = repo.set_name("000002_01", "ethanol")
    assert rec.source == "user"
    assert NameRecord("x", "y", "z").source == "user"


@pytest.mark.parametrize("bad", ["", "User", "pubchem", None, 1])
def test_unknown_source_is_refused(home, bad):
    with SqliteRepository(db(home)) as repo:
        with pytest.raises(RegistryError) as info:
            repo.set_name("000001_01", "x", source=bad)
        assert info.value.code == "invalid_argument"
        assert repo.get_name("000001_01") is None
    assert name_events(home) == []


def test_a_names_table_without_source_gets_the_column_on_open(home):
    # As created by the release that introduced names.
    with closing(sqlite3.connect(str(db(home)), isolation_level=None)) as c:
        c.execute("DROP TABLE names")
        c.execute("""CREATE TABLE names (
    core_stereo_id  TEXT PRIMARY KEY REFERENCES stereos(core_stereo_id),
    name            TEXT NOT NULL CHECK (length(trim(name)) > 0),
    updated_at      TEXT NOT NULL
)""")
        c.execute("INSERT INTO names VALUES ('000001_01', 'old name', '2026-11-01T00:00:00Z')")
    with SqliteRepository(db(home)) as repo:
        assert repo.get_name("000001_01") == NameRecord("000001_01", "old name",
                                                        "2026-11-01T00:00:00Z", "user")
        rec, changed = repo.set_name("000001_02", NAME_S, source="chemdraw")
        assert changed and rec.source == "chemdraw"
        assert repo.get_meta("schema_version") == "1"
    with closing(sqlite3.connect(str(db(home)))) as c:
        rows = c.execute("SELECT core_stereo_id, source FROM names ORDER BY 1").fetchall()
        assert rows == [("000001_01", "user"), ("000001_02", "chemdraw")]
        assert c.execute("PRAGMA user_version").fetchone()[0] == sqlite_repo.SCHEMA_VERSION
    with SqliteRepository(db(home)) as repo:        # opening again changes nothing
        assert repo.integrity_check() == "ok"


def test_a_registry_without_names_gets_the_full_table(home):
    with closing(sqlite3.connect(str(db(home)), isolation_level=None)) as c:
        c.execute("DROP TABLE names")
    with SqliteRepository(db(home)) as repo:
        rec, _ = repo.set_name("000001_01", "x", source="chemdraw")
        assert rec.source == "chemdraw"


def test_get_stereo(home):
    with SqliteRepository(db(home)) as repo:
        s = repo.get_stereo(" 000001_03 ")
        assert s.core_stereo_id == "000001_03" and s.stereo_mode == "rac"
        assert "M  END" in s.molfile and s.stereo_key.startswith("[[") and s.batch_count == 1
        assert repo.get_stereo("000009_01") is None and repo.get_stereo(5) is None


# --------------------------------------------------------------------------- service

RESULT_KEYS = {"core_stereo_id", "name", "generated", "changed", "message", "warnings"}


def test_generate_and_store(home, backups):
    asker = FakeChemDraw()
    with service(home, asker) as svc:
        r = svc.generate_name(" 000001_01 ")
        assert set(r) == RESULT_KEYS
        assert r["core_stereo_id"] == "000001_01"
        assert r["generated"] is True and r["changed"] is True and r["message"] is None
        assert r["name"]["text"] == NAME_R and r["name"]["source"] == "chemdraw"
        assert r["warnings"] == [] and backups == [1] and len(asker.calls) == 1
        assert svc.get_name("000001_01")["name"]["source"] == "chemdraw"
        assert svc.analyze(smiles=AMINE_R)["name"]["source"] == "chemdraw"
        assert svc.find_by_id("000001_01", with_svg=False)["variants"][0]["name"] == r["name"]
    assert name_events(home)[-1] == ("name_set", {"core_stereo_id": "000001_01", "name": NAME_R,
                                                  "previous": None, "source": "chemdraw"})


def test_racemate_gets_a_rac_name(home):
    with service(home, FakeChemDraw()) as svc:
        r = svc.generate_name("000001_03")
    assert r["generated"] and r["name"]["text"] == NAME_RAC


def test_a_stored_name_is_kept_without_asking(home, backups):
    asker = FakeChemDraw()
    with service(home, asker) as svc:
        svc.set_name("000001_01", "my name")
        backups.clear()
        r = svc.generate_name("000001_01")
    assert r == {"core_stereo_id": "000001_01", "name": r["name"], "generated": False,
                 "changed": False, "warnings": [],
                 "message": "A name is already stored for 000001_01; it was kept."}
    assert r["name"]["text"] == "my name" and r["name"]["source"] == "user"
    assert asker.calls == [] and backups == []


def test_replace_overwrites_a_user_name(home):
    with service(home, FakeChemDraw()) as svc:
        svc.set_name("000001_01", NAME_R)            # same text, typed
        r = svc.generate_name("000001_01", replace=True)
        assert r["generated"] and r["changed"] and r["name"]["source"] == "chemdraw"
        again = svc.generate_name("000001_01", replace=True)
        assert again["generated"] is True and again["changed"] is False
        assert again["name"] == r["name"]
        # Editing a generated name makes it a user name.
        edited = svc.set_name("000001_01", NAME_R + " (checked)")
        assert edited["name"]["source"] == "user"
        # set_name with exactly the generated text makes it a user name too.
        svc.generate_name("000001_01", replace=True)
        assert svc.set_name("000001_01", NAME_R)["name"]["source"] == "user"


def test_unchanged_generated_name_does_not_back_up(home, backups):
    with service(home, FakeChemDraw()) as svc:
        svc.generate_name("000001_01")
        svc.generate_name("000001_01", replace=True)
    assert backups == [1]


def test_no_chemdraw_is_not_an_error(home, backups):
    # No asker: the installed ChemDraw, which tests switch off.
    with service(home) as svc:
        r = svc.generate_name("000001_01")
    assert set(r) == RESULT_KEYS
    assert r["generated"] is False and r["changed"] is False and r["name"] is None
    assert "ChemDraw was not found" in r["message"] and backups == []
    assert name_events(home) == []


@pytest.mark.parametrize("code", ["unavailable", "timeout", "failed"])
def test_chemdraw_errors_keep_the_stored_name(home, code):
    asker = FakeChemDraw(error=ChemDrawError(code, f"Problem {code}."))
    with service(home, asker) as svc:
        svc.set_name("000001_01", "typed")
        r = svc.generate_name("000001_01", replace=True)
    assert r["generated"] is False and r["changed"] is False
    assert r["message"].startswith(f"Problem {code}.")
    assert r["name"]["text"] == "typed" and r["name"]["source"] == "user"


def test_a_rejected_answer_is_not_stored(home):
    wrong = FakeChemDraw(names={})        # ChemDraw has no name
    with service(home, wrong) as svc:
        r = svc.generate_name("000002_01")
        assert r["generated"] is False and "has no name" in r["message"]

    def other_structure(molfile, **_):
        return ChemDrawAnswer(NAME_R, "InChI=1S/C8H11N/c1-7(9)8-5-3-2-4-6-8", 9)

    with service(home, other_structure) as svc:
        r = svc.generate_name("000001_01")
        assert r["generated"] is False and "differently" in r["message"]
        assert svc.get_name("000001_01")["name"] is None
    assert name_events(home) == []


def test_a_mixed_variant_is_skipped_without_asking(home):
    asker = FakeChemDraw()
    with service(home, asker) as svc:
        r = svc.generate_name(UNNAMED)
    assert r["generated"] is False and "mixed stereo groups" in r["message"]
    assert asker.calls == []


def test_a_name_that_is_not_one_line_is_not_stored(home):
    def odd(molfile, **_):
        # CSI (U+009B): a C1 control, which naming.check_answer lets through.
        return ChemDrawAnswer("ethan\x9bol", inchi_of(molfile), 3)

    with service(home, odd) as svc:
        r = svc.generate_name("000002_01")
    assert r["generated"] is False and r["name"] is None and "cannot be stored" in r["message"]
    assert name_events(home) == []


def test_bad_arguments(home):
    with service(home, FakeChemDraw()) as svc:
        with pytest.raises(NotFoundError):
            svc.generate_name("000009_01")
        with pytest.raises(NotFoundError):
            svc.generate_name("000001_01_001")
        for bad in ("", "  ", None, 5):
            with pytest.raises(RegistryError) as info:
                svc.generate_name(bad)
            assert info.value.code == "invalid_argument"
        with pytest.raises(RegistryError) as info:
            svc.generate_name("000001_01", replace="yes")
        assert info.value.code == "invalid_argument"


def test_backend_without_names_is_not_supported(home):
    class NoNames:
        def __init__(self, repo):
            self._repo = repo

        def __getattr__(self, name):
            if name in ("get_name", "set_name", "get_stereo", "get_names"):
                raise AttributeError(name)
            return getattr(self._repo, name)

    with service(home, FakeChemDraw()) as svc:
        real = svc.repo
        svc.repo = NoNames(real)
        try:
            with pytest.raises(RegistryError) as info:
                svc.generate_name("000001_01")
            assert info.value.code == "not_supported"
            with pytest.raises(RegistryError) as info:
                svc.generate_names()
            assert info.value.code == "not_supported"
        finally:
            svc.repo = real


def test_backup_warning_is_reported(home, monkeypatch):
    monkeypatch.setattr(backup_mod, "maybe_backup",
                        lambda repo, config, now=None: BackupOutcome(performed=False,
                                                                     warning="Backup failed."))
    with service(home, FakeChemDraw()) as svc:
        assert svc.generate_name("000001_01")["warnings"] == ["Backup failed."]


def test_generated_while_registering_is_blocked(home):
    with RegistryService.open(home) as svc:
        svc.repo.set_meta(FINGERPRINT_KEY, "rdkit=0;inchi=0;keyalg=0")
    journal = home / "issued-ids.jsonl"
    saved = journal.read_bytes()
    with service(home, FakeChemDraw()) as svc:
        with pytest.raises(RegistryError) as info:
            svc.register(smiles="CCCO", experiment_ref="9")
        assert info.value.code == "chemistry_version_changed"
        assert svc.generate_name("000001_01")["generated"] is True
    assert journal.read_bytes() == saved      # names never go to the issued-ID journal


def test_doctor_reports_the_name_generator(home, monkeypatch):
    with RegistryService.open(home) as svc:
        assert svc.doctor()["name_generator"] == {"available": False, "label": None}
        monkeypatch.setattr(chemdraw, "available", lambda: True)
        assert svc.doctor()["name_generator"] == {"available": True, "label": "ChemDraw"}


# --------------------------------------------------------------------------- generate_names

def test_generate_names_tries_every_unnamed_variant(home, backups):
    asker = FakeChemDraw()
    seen = []
    with service(home, asker) as svc:
        svc.set_name("000001_02", "typed")
        backups.clear()
        r = svc.generate_names(progress=seen.append)
        assert set(r) == {"tried", "generated", "remaining", "results", "stopped", "warnings"}
        ids = [e["core_stereo_id"] for e in r["results"]]
        assert ids == ["000001_01", "000001_03", "000002_01", UNNAMED]
        assert seen == r["results"]
        assert r["tried"] == 4 and r["generated"] == 3 and r["remaining"] == 0
        assert r["stopped"] is None
        assert not r["results"][-1]["generated"]
        names = {cs: d["name"]["text"] for cs, d in
                 ((cs, svc.get_name(cs)) for cs in ids[:3])}
        assert names == {"000001_01": NAME_R, "000001_03": NAME_RAC, "000002_01": NAME_ETHANOL}
        assert svc.get_name("000001_02")["name"]["text"] == "typed"   # never replaced
        # A second run tries only the one that is still unnamed.
        again = svc.generate_names()
        assert [e["core_stereo_id"] for e in again["results"]] == [UNNAMED]
    assert backups == [1, 1, 1]


def test_generate_names_limit(home):
    with service(home, FakeChemDraw()) as svc:
        r = svc.generate_names(limit=2)
        assert r["tried"] == 2 and r["remaining"] == 3
        for bad in (0, -1, True, 1.5, "2"):
            with pytest.raises(RegistryError) as info:
                svc.generate_names(limit=bad)
            assert info.value.code == "invalid_argument"


def test_generate_names_stops_without_chemdraw(home):
    with service(home) as svc:
        r = svc.generate_names()
    assert r["tried"] == 1 and r["generated"] == 0 and r["remaining"] == 4
    assert "ChemDraw was not found" in r["stopped"]


def test_generate_names_does_not_stop_on_other_failures(home):
    asker = FakeChemDraw(error=ChemDrawError("timeout", "Too slow."))
    with service(home, asker) as svc:
        r = svc.generate_names()
    assert r["tried"] == 5 and r["generated"] == 0 and r["stopped"] is None
    assert len(asker.calls) == 4          # the mixed variant is skipped without asking


# --------------------------------------------------------------------------- host

def ask(home, op, params, request_id="g1"):
    body = json.dumps({"id": request_id, "v": 1, "op": op, "params": params}).encode("utf-8")
    return host.handle_message(body, HomeChoice(home, "host-config")).response


def test_host_generate_name(home, fake):
    doc = ask(home, "generate_name", {"core_stereo_id": "000001_01"})
    assert doc["ok"] is True and doc["id"] == "g1"
    result = doc["result"]
    assert set(result) == RESULT_KEYS
    assert result["generated"] is True and result["name"]["text"] == NAME_R
    assert result["name"]["source"] == "chemdraw"
    kept = ask(home, "generate_name", {"core_stereo_id": "000001_01"})["result"]
    assert kept["generated"] is False and "kept" in kept["message"] and len(fake.calls) == 1
    replaced = ask(home, "generate_name", {"core_stereo_id": "000001_01", "replace": True})
    assert replaced["result"]["generated"] is True and len(fake.calls) == 2
    found = ask(home, "find", {"id": "000001_01"})["result"]["variants"][0]["name"]
    assert found["source"] == "chemdraw"


def test_host_generate_name_without_chemdraw(home):
    doc = ask(home, "generate_name", {"core_stereo_id": "000001_01"})
    assert doc["ok"] is True and doc["result"]["generated"] is False
    assert "ChemDraw was not found" in doc["result"]["message"]


def test_host_generate_name_errors(home, fake):
    missing = ask(home, "generate_name", {"core_stereo_id": "000009_01"})
    assert missing["ok"] is False and missing["error"]["code"] == "not_found"
    bad = ask(home, "generate_name", {"core_stereo_id": "000001_01", "replace": "yes"})
    assert bad["ok"] is False and bad["error"]["code"] == "bad_request"
    extra = ask(home, "generate_name", {"core_stereo_id": "000001_01", "name": "x"})
    assert extra["ok"] is False and extra["error"]["code"] == "bad_request"
    assert fake.calls == []


def test_host_never_creates_a_registry_for_generate_name(tmp_path, fake):
    nowhere = tmp_path / "nowhere"
    doc = ask(nowhere, "generate_name", {"core_stereo_id": "000001_01"})
    assert doc["error"]["code"] == "registry_missing" and not nowhere.exists()


def test_host_ping_reports_the_name_generator(home, monkeypatch):
    result = ask(home, "ping", {})["result"]
    assert result["name_generator"] == {"available": False, "label": None}
    monkeypatch.setattr(chemdraw, "available", lambda: True)
    assert ask(home, "ping", {})["result"]["name_generator"]["label"] == "ChemDraw"


# --------------------------------------------------------------------------- CLI

def test_cli_name_generate(home, fake, capsys):
    base = ["--home", str(home), "name", "000001_01"]
    assert main(base + ["--generate"]) == 0
    assert f"Name of 000001_01 generated by ChemDraw: {NAME_R}" in capsys.readouterr().out
    assert main(base) == 0
    assert "(stored by ChemDraw 20" in capsys.readouterr().out
    assert main(base + ["--generate"]) == 0
    out = capsys.readouterr().out
    assert "No name was generated for 000001_01: A name is already stored" in out
    assert f"Stored name by ChemDraw: {NAME_R}" in out
    assert main(base + ["--generate", "--replace"]) == 0
    assert "unchanged (generated again) by ChemDraw" in capsys.readouterr().out
    assert main(base + ["--generate", "--replace", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) == RESULT_KEYS and doc["generated"] is True and doc["changed"] is False


def test_cli_name_generate_without_chemdraw(home, capsys):
    assert main(["--home", str(home), "name", "000001_01", "--generate"]) == 0
    out = capsys.readouterr().out
    assert "No name was generated for 000001_01: ChemDraw was not found" in out


def test_cli_name_generate_usage_errors(home, capsys):
    base = ["--home", str(home), "name", "000001_01"]
    assert main(base + ["--replace"]) == 2
    assert main(base + ["x", "--generate"]) == 2
    assert main(base + ["--remove", "--generate"]) == 2
    capsys.readouterr()
    assert main(["--home", str(home), "name", "000009_01", "--generate"]) == 1
    assert "error [not_found]" in capsys.readouterr().err


def test_cli_generate_names(home, fake, capsys):
    assert main(["--home", str(home), "generate-names", "--limit", "2"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == [f"000001_01  {NAME_R}", f"000001_02  {NAME_S}"]
    assert "2 of 2 stereo variant(s) named." in out
    assert "3 variant(s) without a name were not tried." in out
    assert main(["--home", str(home), "generate-names"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"000001_03  {NAME_RAC}"
    assert out[1] == "000002_01  ethanol"
    assert out[2].startswith(f"{UNNAMED}  (no name) This compound has mixed stereo groups")
    assert "2 of 3 stereo variant(s) named." in out
    assert main(["--home", str(home), "generate-names"]) == 0
    assert f"{UNNAMED}  (no name)" in capsys.readouterr().out


def test_cli_generate_names_json(home, fake, capsys):
    assert main(["--home", str(home), "--json", "generate-names", "--limit", "1"]) == 0
    doc = json.loads(capsys.readouterr().out)        # exactly one JSON document
    assert doc["tried"] == 1 and doc["generated"] == 1 and doc["remaining"] == 4
    assert doc["results"][0]["name"]["source"] == "chemdraw"


def test_cli_generate_names_without_chemdraw(home, capsys):
    assert main(["--home", str(home), "generate-names"]) == 0
    out = capsys.readouterr().out
    assert "000001_01  (no name) ChemDraw was not found" in out
    assert "0 of 1 stereo variant(s) named." in out
    assert "Stopped early: ChemDraw was not found" in out
    assert "4 variant(s) without a name were not tried." in out


def test_cli_generate_names_nothing_to_do(home, fake, capsys):
    with RegistryService.open(home) as svc:
        for cs in ("000001_01", "000001_02", "000001_03", "000002_01", UNNAMED):
            svc.set_name(cs, "n")
    assert main(["--home", str(home), "generate-names"]) == 0
    assert "nothing to do" in capsys.readouterr().out and fake.calls == []


@pytest.mark.parametrize("limit", ["0", "-2", "x"])
def test_cli_generate_names_bad_limit(home, limit):
    assert main(["--home", str(home), "generate-names", "--limit", limit]) == 2


def test_cli_doctor_shows_the_name_generator(home, capsys):
    main(["--home", str(home), "doctor"])
    assert "Name generator  none found" in capsys.readouterr().out
