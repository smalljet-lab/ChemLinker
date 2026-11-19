"""Tests for compound_registry.cli (``main([...])`` with capsys, plus one subprocess run)."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from rdkit import Chem
from rdkit.Chem import rdDepictor

from compound_registry import cli
from compound_registry.cli import main

AMINE_R = "C[C@H](N)c1ccccc1"
AMINE_S = "C[C@@H](N)c1ccccc1"
ETHANOL = "CCO"


def molfile(smiles: str) -> str:
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    return Chem.MolToMolBlock(mol)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    # Never prompt unless a test says so.
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)


@pytest.fixture
def home(tmp_path) -> Path:
    """An existing, empty registry (created explicitly: no command but
    ``init`` creates one)."""
    path = tmp_path / "home"
    assert main(["--home", str(path), "init"]) == 0
    return path


@pytest.fixture
def no_registry(tmp_path) -> Path:
    """A home folder in which no registry exists."""
    return tmp_path / "nothing-here"


@pytest.fixture
def run(home, capsys):
    """run(*args) -> (exit code, stdout, stderr) with --home set."""

    def _run(*args: str, json_mode: bool = False):
        argv = ["--home", str(home)] + (["--json"] if json_mode else []) + list(args)
        code = main(argv)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def run_json(run, *args):
    code, out, err = run(*args, json_mode=True)
    return code, json.loads(out), err


def batch_count(run) -> int:
    code, doc, _ = run_json(run, "doctor")
    assert code == 0
    return doc["stats"]["batches"]


def assert_ascii(text: str) -> None:
    text.encode("ascii")
    assert "\x1b[" not in text  # no colour codes


# --------------------------------------------------------------------------- register

def test_register_prints_the_id(run):
    code, out, err = run("register", "--smiles", AMINE_R, "--experiment-ref", "4180",
                         "--experiment-code", "ABC-008", "--yes")
    assert code == 0, err
    assert "000001_01_001" in out and "NEW" in out
    assert "ABC-008" in out
    assert_ascii(out)

    code, out, _ = run("register", "--smiles", AMINE_R, "--experiment-ref", "4180")
    assert code == 0
    assert "000001_01_001" in out and "already registered" in out


def test_register_json(run):
    code, doc, _ = run_json(run, "register", "--smiles", AMINE_R, "--experiment-ref", "E1",
                            "--mode", "abs")
    assert code == 0
    assert doc["id"] == "000001_01_001" and doc["created"] is True
    assert doc["backup"]["performed"] is True


def test_json_option_after_subcommand(run, home, capsys):
    code = main(["register", "--home", str(home), "--json", "--smiles", ETHANOL,
                 "--experiment-ref", "E1", "--yes"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["id"] == "000001_01_001"


def test_register_from_molfile_and_stdin(run, tmp_path, monkeypatch):
    path = tmp_path / "amine.mol"
    path.write_text(molfile(AMINE_R), encoding="utf-8")
    code, out, err = run("register", "--molfile", str(path), "--experiment-ref", "E1", "--yes")
    assert code == 0, err
    assert "000001_01_001" in out

    monkeypatch.setattr(sys, "stdin", io.StringIO(molfile(AMINE_R)))
    code, out, err = run("register", "--stdin", "--experiment-ref", "E2")
    assert code == 0, err
    assert "000001_01_002" in out  # known compound: no confirmation needed

    monkeypatch.setattr(sys, "stdin", io.StringIO(ETHANOL + "\n"))
    code, out, err = run("register", "--stdin", "--experiment-ref", "E3", "--yes")
    assert code == 0, err
    assert "000002_01_001" in out


def test_register_refuses_new_variant_without_mode_or_yes(run):
    code, out, err = run("register", "--smiles", AMINE_R, "--experiment-ref", "E1")
    assert code == 1
    assert "error [confirmation_required]" in err
    assert "--mode" in err and "--yes" in err
    assert "Traceback" not in err
    assert batch_count(run) == 0

    # A new stereo variant of a known core also needs confirmation.
    assert run("register", "--smiles", AMINE_R, "--experiment-ref", "E1", "--yes")[0] == 0
    code, _, err = run("register", "--smiles", AMINE_S, "--experiment-ref", "E2")
    assert code == 1 and "confirmation_required" in err
    code, doc, _ = run_json(run, "register", "--smiles", AMINE_S, "--experiment-ref", "E2")
    assert code == 1 and doc["error"]["code"] == "confirmation_required"
    assert batch_count(run) == 1

    # --mode alone is enough.
    code, out, _ = run("register", "--smiles", AMINE_S, "--experiment-ref", "E2", "--mode", "abs")
    assert code == 0 and "000001_02_001" in out


def test_register_achiral_new_compound_asks_for_yes(run):
    code, _, err = run("register", "--smiles", ETHANOL, "--experiment-ref", "E1")
    assert code == 1 and "no stereo elements" in err and "--yes" in err


def test_register_interactive_choose_other_mode(run, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    answers = iter(["rac", "y"])
    prompts: list[str] = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    code, out, err = run("register", "--smiles", AMINE_R, "--experiment-ref", "E1")
    assert code == 0, err
    assert len(prompts) == 2
    assert "'abs'" in prompts[0] and "'rac'" in prompts[1]  # re-analysed before final yes
    assert "Stereo mode  rac (detected: abs)" in out
    assert "000001_01_001" in out
    code, doc, _ = run_json(run, "lookup", "--id", "000001_01_001")
    assert doc["stereo_mode"] == "rac"


def test_register_interactive_accept_and_cancel(run, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    answers = iter(["n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    code, _, err = run("register", "--smiles", AMINE_R, "--experiment-ref", "E1")
    assert code == 1 and "error [cancelled]" in err
    assert batch_count(run) == 0

    answers = iter(["maybe", "y"])
    code, out, err = run("register", "--smiles", AMINE_R, "--experiment-ref", "E1")
    assert code == 0, err
    assert "Please answer" in out and "000001_01_001" in out
    code, doc, _ = run_json(run, "lookup", "--id", "000001_01_001")
    assert doc["stereo_mode"] == "abs"


def test_register_slot_conflict(run):
    assert run("register", "--smiles", AMINE_R, "--experiment-ref", "E1", "--yes")[0] == 0
    code, _, err = run("register", "--smiles", ETHANOL, "--experiment-ref", "E1", "--yes")
    assert code == 1 and "error [slot_conflict]" in err and "000001_01_001" in err
    code, doc, _ = run_json(run, "register", "--smiles", ETHANOL, "--experiment-ref", "E1")
    assert code == 1 and doc["error"]["code"] == "slot_conflict"
    code, out, _ = run("register", "--smiles", ETHANOL, "--experiment-ref", "E1",
                       "--product-index", "2", "--yes")
    assert code == 0 and "000002_01_001" in out


def test_register_errors(run, tmp_path):
    code, out, err = run("register", "--smiles", "C1CC(", "--experiment-ref", "E1", "--yes")
    assert code == 1 and "error [structure_parse_failed]" in err and out == ""
    assert "Traceback" not in err
    code, doc, err = run_json(run, "register", "--smiles", "C1CC(", "--experiment-ref", "E1")
    assert code == 1 and doc == {"error": {"code": "structure_parse_failed",
                                           "message": doc["error"]["message"]}}
    assert err == ""
    code, _, err = run("register", "--molfile", str(tmp_path / "missing.mol"),
                       "--experiment-ref", "E1", "--yes")
    assert code == 1 and "error [file_error]" in err
    code, _, err = run("register", "--smiles", ETHANOL, "--experiment-ref", "  ", "--yes")
    assert code == 1 and "error [invalid_argument]" in err


@pytest.mark.parametrize("argv", [
    ["register", "--smiles", "CCO"],                                   # no --experiment-ref
    ["register", "--experiment-ref", "E1"],                            # no structure
    ["register", "--smiles", "CCO", "--molfile", "x", "--experiment-ref", "E1"],
    ["register", "--smiles", "CCO", "--experiment-ref", "E1", "--mode", "racemic"],
    ["register", "--smiles", "CCO", "--experiment-ref", "E1", "--product-index", "two"],
    ["lookup"],
    ["lookup", "--id", "000001_01_001", "--experiment-ref", "E1"],
    ["lookup", "--id", "000001_01_001", "--mode", "rac"],
    ["export", "--format", "xlsx", "--output", "x"],
    ["backup", "--list", "--verify", "x"],
    ["retire", "000001_01_001"],                                       # no --reason
    ["frobnicate"],
    [],
])
def test_usage_errors_exit_2(run, argv):
    code, _, err = run(*argv)
    assert code == 2
    assert "usage:" in err


def test_help_exits_0(run):
    code, out, _ = run("--help")
    assert code == 0 and "register" in out and "rekey-check" in out


# --------------------------------------------------------------------------- lookup

@pytest.fixture
def populated(run):
    for argv in (
        ["--smiles", AMINE_R, "--experiment-ref", "E1", "--experiment-code", "ABC-001"],
        ["--smiles", AMINE_R, "--experiment-ref", "E2", "--experiment-code", "ABC-002"],
        ["--smiles", AMINE_S, "--experiment-ref", "E3", "--mode", "abs"],
        ["--smiles", ETHANOL, "--experiment-ref", "E1", "--product-index", "2"],
    ):
        code, _, err = run("register", *argv, "--yes")
        assert code == 0, err
    return run


def test_lookup_structure(populated):
    code, out, _ = populated("lookup", "--smiles", AMINE_R)
    assert code == 0
    assert "known compound" in out
    assert "000001_01_001" in out and "ABC-001" in out
    assert "000001_01_002" in out and "ABC-002" in out
    assert "000001_02" in out  # other stereo variant
    assert_ascii(out)

    code, out, _ = populated("lookup", "--smiles", AMINE_R, "--experiment-ref", "E2")
    assert "already registered in this experiment" in out and "ID here        000001_01_002" in out

    code, out, _ = populated("lookup", "--smiles", "C[C@H](N)c1ccccc1", "--mode", "rac")
    assert "known core, NEW stereo variant" in out

    code, out, _ = populated("lookup", "--smiles", "c1ccccc1")
    assert code == 0 and "NEW compound" in out


def test_lookup_structure_json(populated):
    code, doc, _ = run_json(populated, "lookup", "--smiles", AMINE_R, "--experiment-ref", "E1")
    assert code == 0
    assert doc["status"] == "registered_here" and doc["id"] == "000001_01_001"
    assert [b["full_id"] for b in doc["batches"]] == ["000001_01_001", "000001_01_002"]
    assert doc["other_stereos"][0]["core_stereo_id"] == "000001_02"


def test_lookup_id_and_experiment(populated):
    code, out, _ = populated("lookup", "--id", "000001_01_002")
    assert code == 0 and "000001_01_002" in out and "ABC-002" in out
    code, doc, _ = run_json(populated, "lookup", "--id", "000001_01_002")
    assert doc["full_id"] == "000001_01_002" and doc["retired"] is False
    code, _, err = populated("lookup", "--id", "000009_01_001")
    assert code == 1 and "error [not_found]" in err

    code, out, _ = populated("lookup", "--experiment-ref", "E1")
    assert code == 0 and "2 batch(es)" in out
    assert "000001_01_001" in out and "000002_01_001" in out
    code, doc, _ = run_json(populated, "lookup", "--experiment-ref", "E1")
    assert [b["full_id"] for b in doc["batches"]] == ["000001_01_001", "000002_01_001"]


def _dump(db: Path) -> list[str]:
    """Every row of the database as SQL text (the file header is not compared:
    opening the registry rewrites its change counter)."""
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return list(conn.iterdump())


def test_lookup_is_read_only(populated, home):
    db = home / "registry.sqlite"
    before = _dump(db)
    populated("lookup", "--smiles", "c1ccccc1", "--experiment-ref", "E7")
    populated("lookup", "--smiles", AMINE_R, "--experiment-ref", "E1")
    populated("lookup", "--experiment-ref", "E1")
    populated("lookup", "--id", "000001_01_001")
    assert _dump(db) == before


# --------------------------------------------------------------------------- export

def test_export(populated, tmp_path):
    out_csv = tmp_path / "all.csv"
    code, out, _ = populated("export", "--format", "csv", "--output", str(out_csv))
    assert code == 0 and "Exported 4 batches" in out and out_csv.is_file()

    populated("retire", "000002_01_001", "--reason", "test", "--yes")
    out_sdf = tmp_path / "active.sdf"
    code, doc, _ = run_json(populated, "export", "--format", "sdf", "--output", str(out_sdf),
                            "--active-only")
    assert code == 0 and doc["count"] == 3 and doc["include_retired"] is False
    assert len(list(Chem.SDMolSupplier(str(out_sdf)))) == 3

    code, _, err = populated("export", "--format", "csv", "--output",
                             str(tmp_path / "missing" / "x.csv"))
    assert code == 1 and "error [export_failed]" in err


# --------------------------------------------------------------------------- backup

def test_backup_commands(populated, home, tmp_path):
    code, out, _ = populated("backup")
    assert code == 0 and "Backup written:" in out
    path = out.split("Backup written:", 1)[1].strip().splitlines()[0]
    assert Path(path).is_file()

    code, doc, _ = run_json(populated, "backup", "--list")
    assert code == 0 and len(doc["backups"]) >= 1
    assert path in [b["path"] for b in doc["backups"]]
    code, out, _ = populated("backup", "--list")
    assert code == 0 and Path(path).name in out

    code, out, _ = populated("backup", "--verify", path)
    assert code == 0 and out.startswith("OK:")
    code, doc, _ = run_json(populated, "backup", "--verify", path)
    assert doc["ok"] is True

    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database at all")
    code, _, err = populated("backup", "--verify", str(junk))
    assert code == 1 and "error [backup_invalid]" in err
    code, doc, _ = run_json(populated, "backup", "--verify", str(tmp_path / "nope.sqlite"))
    assert code == 1 and doc["error"]["code"] == "backup_invalid"


def test_backup_failure_is_exit_1(run, home, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    (home / "config.toml").write_text(
        f"[backup]\nfolder = '{(blocker / 'backups').as_posix()}'\n", encoding="utf-8")
    code, _, err = run("backup")
    assert code == 1 and "error [backup_failed]" in err


# --------------------------------------------------------------------------- retire

def test_retire(populated):
    code, _, err = populated("retire", "000001_01_001", "--reason", "wrong drawing")
    assert code == 1 and "error [confirmation_required]" in err and "--yes" in err
    code, doc, _ = run_json(populated, "lookup", "--id", "000001_01_001")
    assert doc["retired"] is False

    code, out, _ = populated("retire", "000001_01_001", "--reason", "wrong drawing", "--yes")
    assert code == 0 and "Retired 000001_01_001: wrong drawing" in out
    code, doc, _ = run_json(populated, "lookup", "--id", "000001_01_001")
    assert doc["retired"] is True and doc["retired_reason"] == "wrong drawing"

    code, _, err = populated("retire", "000001_01_001", "--reason", "again", "--yes")
    assert code == 1 and "error [already_retired]" in err
    code, doc, _ = run_json(populated, "retire", "000009_01_001", "--reason", "x", "--yes")
    assert code == 1 and doc["error"]["code"] == "not_found"
    code, _, err = populated("retire", "000001_01_002", "--reason", "  ", "--yes")
    assert code == 1 and "error [invalid_argument]" in err


def test_retire_interactive(populated, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    code, _, err = populated("retire", "000001_01_002", "--reason", "test")
    assert code == 1 and "error [cancelled]" in err
    code, out, _ = populated("retire", "000001_01_002", "--reason", "test")
    assert code == 0 and "Retired 000001_01_002" in out


def test_retire_json_never_prompts(populated, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("prompted in --json"))
    code, doc, _ = run_json(populated, "retire", "000001_01_002", "--reason", "x")
    assert code == 1 and doc["error"]["code"] == "confirmation_required"


# --------------------------------------------------------------------------- maintenance

def test_rekey_check(populated):
    code, out, _ = populated("rekey-check")
    assert code == 0 and "Checked 4 batch(es): 4 OK, 0 mismatch(es)" in out
    code, doc, _ = run_json(populated, "rekey-check")
    assert code == 0 and doc["checked"] == 4 and doc["mismatches"] == []


def test_doctor(populated, home):
    code, out, _ = populated("doctor")
    assert code == 0
    assert str(home) in out
    assert "{core:06d}_{stereo:02d}_{batch:03d}" in out
    assert "2 cores, 3 stereo variants, 4 batches (0 retired)" in out
    assert "Integrity       ok" in out
    assert_ascii(out)
    code, doc, _ = run_json(populated, "doctor")
    assert code == 0 and doc["integrity"] == "ok" and doc["stats"]["batches"] == 4


def test_home_from_environment(monkeypatch, tmp_path, capsys):
    env_home = tmp_path / "from-env"
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(env_home))
    assert main(["--json", "init"]) == 0
    assert json.loads(capsys.readouterr().out)["home"] == str(env_home)
    assert main(["--json", "doctor"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["home"] == str(env_home)


# --------------------------------------------------------------------------- init / missing registry

def test_no_command_but_init_creates_a_registry(no_registry, capsys):
    """A missing registry (moved folder, unplugged drive) must never be replaced
    by a new, empty one: that would issue IDs that already exist."""
    base = ["--home", str(no_registry)]
    commands = [
        ["doctor"],
        ["register", "--smiles", ETHANOL, "--experiment-ref", "E1", "--yes"],
        ["lookup", "--smiles", ETHANOL],
        ["lookup", "--id", "000001_01_001"],
        ["export", "--format", "csv", "--output", str(no_registry.parent / "x.csv")],
        ["backup"],
        ["retire", "000001_01_001", "--reason", "x", "--yes"],
        ["rekey-check"],
        ["recover", "--yes"],
        ["compounds"],
        ["compounds", "--text", "ABC", "--smiles", ETHANOL],
        ["name", "000001_01"],
        ["name", "000001_01", "ethanol"],
        ["name", "000001_01", "--remove"],
    ]
    for command in commands:
        assert main(base + command) == 1, command
        err = capsys.readouterr().err
        assert "error [registry_missing]" in err and "compound-registry init" in err, command
        assert not no_registry.exists(), command
    assert main(base + ["--json", "doctor"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "registry_missing"
    assert not no_registry.exists()


def test_init_creates_once_and_is_harmless_when_repeated(no_registry, capsys):
    base = ["--home", str(no_registry)]
    assert main(base + ["init"]) == 0
    out = capsys.readouterr().out
    assert "A NEW, EMPTY registry was created" in out and str(no_registry) in out
    assert (no_registry / "config.toml").is_file() and (no_registry / "registry.sqlite").is_file()
    assert main(base + ["register", "--smiles", ETHANOL, "--experiment-ref", "E1", "--yes"]) == 0
    capsys.readouterr()
    assert main(base + ["--json", "init"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["created"] is False and doc["batches"] == 1 and doc["integrity"] == "ok"
    assert main(base + ["init"]) == 0
    assert "already exists" in capsys.readouterr().out


PREFIX_FORMAT = "EX{core:04d}_{stereo:02d}_{batch:03d}"


def test_init_with_an_id_format_creates_a_registry_that_issues_such_ids(no_registry, capsys):
    base = ["--home", str(no_registry)]
    assert main(base + ["--json", "init", "--id-format", PREFIX_FORMAT]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["created"] is True and doc["id_format"] == PREFIX_FORMAT and doc["batches"] == 0
    assert f'format = "{PREFIX_FORMAT}"' in (no_registry / "config.toml").read_text(encoding="utf-8")
    assert main(base + ["--json", "register", "--smiles", ETHANOL, "--experiment-ref", "E1",
                        "--yes"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "EX0001_01_001"
    for text in ("EX0001_01_001", "EX0001_01", "EX0001", "1"):
        assert main(base + ["--json", "experiments", text]) == 0, text
        assert json.loads(capsys.readouterr().out)["core_no"] == 1
    assert main(base + ["--json", "doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["id_format"] == PREFIX_FORMAT


def test_init_id_format_is_refused_for_an_existing_registry(no_registry, capsys):
    base = ["--home", str(no_registry)]
    assert main(base + ["init"]) == 0
    capsys.readouterr()
    assert main(base + ["init", "--id-format", PREFIX_FORMAT]) == 1
    assert "error [invalid_argument]" in capsys.readouterr().err
    assert main(base + ["--json", "doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["id_format"] == "{core:06d}_{stereo:02d}_{batch:03d}"


@pytest.mark.parametrize("template", ["EX{core:04d}", "E3X{core:04d}_{stereo:02d}_{batch:03d}",
                                      "{core}_{stereo:02d}_{batch:03d}", ""])
def test_init_refuses_an_unusable_id_format_and_creates_nothing(no_registry, capsys, template):
    assert main(["--home", str(no_registry), "init", "--id-format", template]) == 1
    assert "error [" in capsys.readouterr().err
    assert not no_registry.exists()


def test_registry_whose_database_vanished_is_not_recreated(home, run):
    code, _, err = run("register", "--smiles", ETHANOL, "--experiment-ref", "E1", "--yes")
    assert code == 0, err
    (home / "registry.sqlite").rename(home / "registry.sqlite.moved")
    code, _, err = run("register", "--smiles", AMINE_R, "--experiment-ref", "E2", "--yes")
    assert code == 1 and "error [registry_missing]" in err
    assert not (home / "registry.sqlite").exists()
    # Creating a new database next to the journal of the old one is possible,
    # but init says what to do first and the journal guard blocks new IDs.
    code, out, _ = run("init")
    assert code == 0 and "compound-registry recover" in out
    code, _, err = run("register", "--smiles", AMINE_R, "--experiment-ref", "E2", "--yes")
    assert code == 1 and "error [database_older_than_journal]" in err


def test_non_ascii_output_is_escaped(run):
    code, out, err = run("register", "--smiles", ETHANOL, "--experiment-ref", "E1",
                         "--experiment-code", "José-001", "--yes")
    assert code == 0, err
    assert_ascii(out)
    assert "Jos\\xe9-001" in out
    code, out, _ = run("lookup", "--id", "000001_01_001", json_mode=True)
    assert_ascii(out)
    assert json.loads(out)["experiment_code"] == "José-001"


def test_relative_home_is_rejected(capsys):
    code = main(["--home", "relative-registry", "doctor"])
    err = capsys.readouterr().err
    assert code == 1 and "error [config_invalid]" in err and "absolute" in err
    assert not Path("relative-registry").exists()


def test_help_lists_recover(run):
    code, out, _ = run("--help")
    assert code == 0 and "recover" in out


def test_help_lists_reaction_and_route(run):
    code, out, _ = run("--help")
    assert code == 0 and "reaction" in out and "route" in out
    code, out, _ = run("route", "--help")
    assert code == 0 and "--no-open" in out and "CORE_STEREO" in out


@pytest.mark.parametrize("argv", [("route",), ("reaction",), ("route", "X_01", "--open")])
def test_reaction_and_route_usage_errors(run, argv):
    code, _, err = run(*argv)
    assert code == 2 and "usage:" in err


# --------------------------------------------------------------------------- stale database / recover

def test_stale_database_and_recover(run, home, monkeypatch):
    code, first, _ = run_json(run, "register", "--smiles", AMINE_R, "--experiment-ref", "E1", "--yes")
    assert code == 0 and first["backup"]["performed"]
    for argv in (["--smiles", ETHANOL, "--experiment-ref", "E2"],
                 ["--smiles", AMINE_S, "--experiment-ref", "E3", "--mode", "abs"]):
        assert run("register", *argv, "--yes")[0] == 0
    db = home / "registry.sqlite"
    shutil.copy2(first["backup"]["path"], db)  # restore the old backup

    code, _, err = run("register", "--smiles", "c1ccccc1", "--experiment-ref", "E9", "--yes")
    assert code == 1 and "error [database_older_than_journal]" in err
    assert "000002_01_001" in err and "compound-registry recover" in err
    code, doc, _ = run_json(run, "retire", "000001_01_001", "--reason", "x", "--yes")
    assert code == 1 and doc["error"]["code"] == "database_older_than_journal"
    code, out, _ = run("lookup", "--id", "000001_01_001")
    assert code == 0
    code, out, _ = run("doctor")
    assert "Missing in DB   2 ID(s): 000002_01_001, 000001_02_001" in out
    assert "Journal         " in out

    code, _, err = run("recover")
    assert code == 1 and "error [confirmation_required]" in err and "--yes" in err
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    code, out, err = run("recover")
    assert code == 1 and "error [cancelled]" in err and "000002_01_001" in out
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    code, out, err = run("recover")
    assert code == 0, err
    assert "Recovered 2 ID(s): 000002_01_001, 000001_02_001" in out
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)

    code, doc, _ = run_json(run, "recover", "--yes")
    assert code == 0 and doc["recovered"] == [] and doc["already_present"] == 3
    code, doc, _ = run_json(run, "lookup", "--id", "000001_02_001")
    assert code == 0 and doc["stereo_mode"] == "abs"
    code, out, _ = run("register", "--smiles", "c1ccccc1", "--experiment-ref", "E9", "--yes")
    assert code == 0 and "000003_01_001" in out


def test_recover_json_never_prompts(run, monkeypatch, home):
    code, first, _ = run_json(run, "register", "--smiles", ETHANOL, "--experiment-ref", "E1", "--yes")
    assert run("register", "--smiles", "CCCC", "--experiment-ref", "E2", "--yes")[0] == 0
    shutil.copy2(first["backup"]["path"], home / "registry.sqlite")
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("prompted in --json"))
    code, doc, _ = run_json(run, "recover")
    assert code == 1 and doc["error"]["code"] == "confirmation_required"
    code, doc, _ = run_json(run, "recover", "--yes")
    assert code == 0 and doc["recovered"] == ["000002_01_001"]


# --------------------------------------------------------------------------- chemistry version

def test_rekey_check_accept_after_chemistry_change(populated, home):
    from compound_registry.service import FINGERPRINT_KEY, chemistry_fingerprint
    from compound_registry.sqlite_repo import SqliteRepository

    with SqliteRepository(home / "registry.sqlite") as repo:
        repo.set_meta(FINGERPRINT_KEY, "rdkit=2020.03.1;inchi=1.05;keyalg=1")
    code, _, err = populated("register", "--smiles", "c1ccccc1", "--experiment-ref", "E9", "--yes")
    assert code == 1 and "error [chemistry_version_changed]" in err
    assert "rekey-check --accept" in err
    code, out, _ = populated("doctor")
    assert "rdkit=2020.03.1" in out and "CHANGED" in out and chemistry_fingerprint() in out

    code, out, _ = populated("rekey-check")
    assert code == 0 and "Accepted" not in out
    code, out, _ = populated("rekey-check", "--accept")
    assert code == 0 and "Accepted" in out
    code, out, _ = populated("register", "--smiles", "c1ccccc1", "--experiment-ref", "E9", "--yes")
    assert code == 0 and "000003_01_001" in out
    code, doc, _ = run_json(populated, "doctor")
    assert doc["chemistry_fingerprint"]["changed"] is False


# --------------------------------------------------------------------------- ignored stereo

IGNORED = ("allene axis C2=C3=C4",)


@pytest.fixture
def ignoring(monkeypatch):
    from dataclasses import replace

    from compound_registry.service import RegistryService

    original = RegistryService._normalize
    monkeypatch.setattr(RegistryService, "_normalize",
                        lambda self, *a, **k: replace(original(self, *a, **k), ignored_stereo=IGNORED))


def test_register_ignored_stereo_non_interactive(run, ignoring):
    code, _, err = run("register", "--smiles", ETHANOL, "--experiment-ref", "E1", "--yes")
    assert code == 1 and "error [stereo_not_in_key]" in err and IGNORED[0] in err
    assert batch_count(run) == 0
    code, doc, _ = run_json(run, "register", "--smiles", ETHANOL, "--experiment-ref", "E1", "--yes",
                            "--accept-ignored-stereo")
    assert code == 0 and doc["accepted_ignored_stereo"] == list(IGNORED)
    # idempotent repeat: no acceptance needed
    code, out, err = run("register", "--smiles", ETHANOL, "--experiment-ref", "E1")
    assert code == 0, err
    assert "000001_01_001" in out and "already registered" in out


def test_register_ignored_stereo_interactive(run, ignoring, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    prompts: list[str] = []
    answers = iter(["y", "y"])  # stereo mode confirmed, but 'y' is not an explicit 'yes'

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    code, out, err = run("register", "--smiles", AMINE_R, "--experiment-ref", "E1")
    assert code == 1 and "error [cancelled]" in err
    assert IGNORED[0] in out and "SAME ID" in out
    assert len(prompts) == 2 and "'yes'" in prompts[1]
    assert batch_count(run) == 0

    answers = iter(["y", "yes"])
    code, out, err = run("register", "--smiles", AMINE_R, "--experiment-ref", "E1")
    assert code == 0, err
    assert "000001_01_001" in out and "Accepted     stereo not in the key: " + IGNORED[0] in out

    # --yes covers the stereo mode only; the ignored stereo is still asked.
    answers = iter(["yes"])
    prompts.clear()
    code, out, err = run("register", "--smiles", AMINE_S, "--experiment-ref", "E2", "--yes")
    assert code == 0, err
    assert len(prompts) == 1 and "000001_02_001" in out


# --------------------------------------------------------------------------- subprocess

def test_end_to_end_subprocess(tmp_path):
    home = tmp_path / "e2e-home"
    env = dict(os.environ, COMPOUND_REGISTRY_HOME=str(tmp_path / "unused"))
    base = [sys.executable, "-m", "compound_registry.cli", "--home", str(home)]

    def call(*args, stdin=None):
        return subprocess.run(base + list(args), input=stdin, capture_output=True, text=True,
                              env=env, timeout=120)

    p = call("doctor")
    assert p.returncode == 1 and "error [registry_missing]" in p.stderr and not home.exists()
    p = call("init")
    assert p.returncode == 0 and "NEW, EMPTY registry" in p.stdout

    p = call("register", "--smiles", AMINE_R, "--experiment-ref", "E1")
    assert p.returncode == 1 and "confirmation_required" in p.stderr  # not a terminal

    p = call("register", "--stdin", "--experiment-ref", "E1", "--yes", stdin=molfile(AMINE_R))
    assert p.returncode == 0, p.stderr
    assert "000001_01_001" in p.stdout

    p = call("--json", "lookup", "--smiles", AMINE_R, "--experiment-ref", "E1")
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["status"] == "registered_here" and doc["id"] == "000001_01_001"

    p = call("lookup", "--smiles", "C1CC(")
    assert p.returncode == 1 and "error [structure_parse_failed]" in p.stderr
    assert "Traceback" not in p.stderr

    p = call("doctor")
    assert p.returncode == 0 and "1 batches" in p.stdout
    assert (home / "registry.sqlite").is_file()
    assert not (tmp_path / "unused").exists()
