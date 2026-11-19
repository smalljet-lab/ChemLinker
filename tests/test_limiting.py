"""The starting material ("SM") of a run: the user's choice per experiment
(``set_limiting``) and how routes use it.

Storage (sqlite_repo ``get_limiting`` / ``set_limiting``, columns beside the
stored reaction, untouched by ``set_reaction``), the service
``set_limiting``, the route builder's rule (a choice that still applies, else
the automatic one with a note), ``route`` ``choices``, the host op and the
CLI ``limiting``. The example data is tests/data/reactions_example.json:
in ABC-002 both reactants have 1 equivalent.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest

from compound_registry import cli, host, routes, sqlite_repo
from compound_registry.cli import main
from compound_registry.config import load_config
from compound_registry.errors import NotFoundError, RegistryError
from compound_registry.host import HomeChoice
from compound_registry.route_data import RegistryRouteData
from compound_registry.route_model import Candidate
from compound_registry.service import RegistryService
from compound_registry.sqlite_repo import SqliteRepository
from conftest import build_example_registry, example_data

EXAMPLE = example_data()
EXPERIMENTS = EXAMPLE["experiments"]
ABC002 = EXPERIMENTS["ABC-002"]
REF = ABC002["experiment_ref"]             # "4120"
PHENYLETHYLAMINE = "(S)-(-)-1-Phenylethylamine"
DRAWN = ["ABC-001", "ABC-002", "ABC-004", "ABC-003", "ABC-006", "ABC-008"]
AUTO_NOTE = ("ABC-002: the starting material chosen earlier (row 2, (S)-(-)-1-Phenylethylamine) "
             "is no longer in the reaction; the automatic choice is used.")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)


def db(home: Path) -> Path:
    return home / "registry.sqlite"


def events(home: Path, name: str = sqlite_repo.LIMITING_EVENT) -> list[dict]:
    with closing(sqlite3.connect(db(home).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        rows = c.execute("SELECT event, full_id, details FROM events ORDER BY event_id").fetchall()
    out = []
    for event, full_id, details in rows:
        if event == name:
            assert full_id is None
            out.append(json.loads(details))
    return out


def renamed(reaction: dict, row: int, name: str | None) -> dict:
    """A copy of ``reaction`` whose reactant ``row`` has another name."""
    out = copy.deepcopy(reaction)
    for component in out["reactants"]:
        if component["row"] == row:
            component["name"] = name
    return out


def service(home: Path) -> RegistryService:
    return RegistryService(load_config(home), local_clock=lambda: datetime(2026, 11, 19, 14, 3, 5))


class Captured:
    """The real route builder with the ChemDraw writer replaced (the route
    drawn is kept), so only the builder and the service meet."""

    def __init__(self, monkeypatch):
        self.routes: list = []
        monkeypatch.setattr(RegistryService, "_route_tools",
                            staticmethod(lambda: (routes.build_route, self.render)))

    def render(self, route):
        self.routes.append(route)
        return "<CDXML/>"


def run_of(route, code: str):
    return next(r for s in route.steps for r in s.runs if r.experiment_code == code)


def choice_of(result: dict, code: str) -> dict:
    return next(c for c in result["choices"] if c["experiment_code"] == code)


# --------------------------------------------------------------------------- storage

def test_storage_get_set_and_events(example_home):
    with SqliteRepository(db(example_home)) as repo:
        assert repo.get_limiting(REF) is None
        assert repo.set_limiting(REF, 2, f"  {PHENYLETHYLAMINE} ") is True
        assert repo.get_limiting(f" {REF} ") == (2, PHENYLETHYLAMINE)
        assert repo.set_limiting(REF, 2, PHENYLETHYLAMINE) is False          # same: nothing
        assert repo.set_limiting(REF, 1, "ABC-001") is True
        assert repo.set_limiting(REF, None, "ignored") is True              # automatic again
        assert repo.get_limiting(REF) is None
        assert repo.set_limiting(REF, None) is False
        assert repo.get_limiting("") is None and repo.get_limiting("nope") is None
    assert events(example_home) == [
        {"experiment_ref": REF, "code": "ABC-002", "row": 2, "name": PHENYLETHYLAMINE},
        {"experiment_ref": REF, "code": "ABC-002", "row": 1, "name": "ABC-001"},
        {"experiment_ref": REF, "code": "ABC-002", "row": None, "name": None},
    ]


def test_storage_refuses_without_a_reaction_or_with_a_bad_row(tmp_path, capsys):
    home = build_example_registry(tmp_path / "example", reactions=False)
    capsys.readouterr()
    with SqliteRepository(db(home)) as repo:
        with pytest.raises(NotFoundError):
            repo.set_limiting(REF, 1, "x")
        for bad in (0, -1, True, 1.0, "1"):
            with pytest.raises(RegistryError) as info:
                repo.set_limiting(REF, bad, "x")                # type: ignore[arg-type]
            assert info.value.code == "invalid_argument"
        with pytest.raises(RegistryError) as info:
            repo.set_limiting("  ", 1, "x")
        assert info.value.code == "invalid_argument"
    assert events(home) == []


def test_set_reaction_never_touches_the_choice(example_home):
    with SqliteRepository(db(example_home)) as repo:
        repo.set_limiting(REF, 2, PHENYLETHYLAMINE)
        _, changed = repo.set_reaction(REF, None, None, renamed(ABC002["reaction"], 1, "X"))
        assert changed is True
        assert repo.get_limiting(REF) == (2, PHENYLETHYLAMINE)
        repo.set_reaction(REF, "ABC-002b", "https://eln.example/x", ABC002["reaction"])
        assert repo.get_limiting(REF) == (2, PHENYLETHYLAMINE)
        assert repo.get_reaction(REF)["data"] == ABC002["reaction"]


def test_columns_are_added_to_an_older_reactions_table(example_home):
    """A registry from before the choice: its reactions table gets the two
    columns on open, the stored reactions stay, no schema version change."""
    with closing(sqlite3.connect(str(db(example_home)), isolation_level=None)) as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        rows = c.execute("SELECT experiment_ref, experiment_code, experiment_url, data, "
                         "updated_at FROM reactions ORDER BY experiment_ref").fetchall()
        c.execute("DROP TABLE reactions")
        c.execute("""CREATE TABLE reactions (
    experiment_ref   TEXT PRIMARY KEY CHECK (length(experiment_ref) > 0),
    experiment_code  TEXT,
    experiment_url   TEXT,
    data             TEXT NOT NULL CHECK (json_valid(data)),
    updated_at       TEXT NOT NULL)""")
        c.executemany("INSERT INTO reactions VALUES (?, ?, ?, ?, ?)", rows)
    with SqliteRepository(db(example_home)) as repo:
        assert repo.get_limiting(REF) is None
        assert repo.set_limiting(REF, 2, PHENYLETHYLAMINE) is True
        assert repo.get_reaction(REF)["data"] == ABC002["reaction"]
    with closing(sqlite3.connect(str(db(example_home)))) as c:
        columns = [r[1] for r in c.execute("PRAGMA table_info(reactions)")]
        assert columns[-2:] == ["limiting_row", "limiting_name"]
        assert c.execute("PRAGMA user_version").fetchone()[0] == version
        assert c.execute("SELECT count(*) FROM reactions").fetchone()[0] == len(rows)
    with SqliteRepository(db(example_home)) as repo:                  # opening again: no change
        assert repo.get_limiting(REF) == (2, PHENYLETHYLAMINE)


# --------------------------------------------------------------------------- service

def test_service_set_limiting(example_home):
    with service(example_home) as svc:
        r = svc.set_limiting(REF, 2)
        assert r == {"experiment_ref": REF, "row": 2, "name": PHENYLETHYLAMINE, "changed": True,
                     "warnings": []}
        assert svc.set_limiting(f" {REF} ", 2)["changed"] is False
        assert svc.set_limiting(REF, None) == {"experiment_ref": REF, "row": None, "name": None,
                                               "changed": True, "warnings": []}
        assert svc.set_limiting(REF, None)["changed"] is False
    assert [e["row"] for e in events(example_home)] == [2, None]


def test_service_records_the_formula_when_the_row_has_no_name(example_home):
    with service(example_home) as svc:
        svc.set_reaction(REF, renamed(ABC002["reaction"], 2, None))
        assert svc.set_limiting(REF, 2)["name"] == "C8H11N"


def test_service_refusals(example_home):
    with service(example_home) as svc:
        with pytest.raises(NotFoundError):
            svc.set_limiting("424242", 1)                 # no reaction stored
        for row in (3, 999):                              # ABC-002 has rows 1 and 2
            with pytest.raises(RegistryError) as info:
                svc.set_limiting(REF, row)
            assert info.value.code == "invalid_argument"
            assert "has no reactant in row" in info.value.message
            assert "its reactant rows are 1, 2" in info.value.message
        for row in (0, 1000, True, "2", 1.5):
            with pytest.raises(RegistryError) as info:
                svc.set_limiting(REF, row)                # type: ignore[arg-type]
            assert info.value.code == "invalid_argument"
        for ref in (None, "", "   ", 5):
            with pytest.raises(RegistryError) as info:
                svc.set_limiting(ref, 1)                  # type: ignore[arg-type]
            assert info.value.code == "invalid_argument"
    assert events(example_home) == []


def test_service_without_a_stored_reaction_is_not_found(tmp_path, capsys):
    home = build_example_registry(tmp_path / "example", reactions=False)
    capsys.readouterr()
    with service(home) as svc:
        with pytest.raises(NotFoundError):
            svc.set_limiting(REF, 1)


def test_a_backend_without_choices_refuses_but_routes_still_work(example_home, monkeypatch):
    class Bare:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name in ("set_limiting", "get_limiting"):
                raise AttributeError(name)
            return getattr(self._inner, name)

    captured = Captured(monkeypatch)
    with service(example_home) as svc:
        inner = svc.repo
        svc.repo = Bare(inner)
        try:
            with pytest.raises(RegistryError) as info:
                svc.set_limiting(REF, 2)
            assert info.value.code == "not_supported"
            r = svc.route("EX0004_01", open=False)
        finally:
            svc.repo = inner
    assert choice_of(r, "ABC-002")["by"] == "auto"
    assert run_of(captured.routes[0], "ABC-002").line == "ABC-002, SM = 1.50 g, 17.25 %"


def test_route_data_limiting_choice(example_home):
    asked = []

    def get_limiting(ref):
        asked.append(ref)
        return {"a": (2, "X"), "b": None, "c": ("2", "X"), "d": (True, None)}[ref] \
            if ref != "boom" else 1 / 0

    data = RegistryRouteData([], {}, lambda ref: None, lambda m: None, get_limiting)
    assert data.limiting_choice("a") == (2, "X") and data.limiting_choice("a") == (2, "X")
    assert asked == ["a"]                                             # remembered
    for ref in ("b", "c", "d", "boom"):
        assert data.limiting_choice(ref) is None
    assert RegistryRouteData([], {}, lambda ref: None, lambda m: None).limiting_choice("a") \
        is None


# --------------------------------------------------------------------------- routes on the example data

def test_route_choices_automatic(example_home, monkeypatch):
    captured = Captured(monkeypatch)
    with service(example_home) as svc:
        r = svc.route("EX0004_01", open=False)
    assert [c["experiment_code"] for c in r["choices"]] == DRAWN == r["experiments"]
    assert [c["compound"] for c in r["choices"]] == ["EX0001_01", "EX0002_01", "EX0002_01",
                                                     "EX0003_01", "EX0003_01", "EX0004_01"]
    assert choice_of(r, "ABC-002") == {
        "experiment_ref": REF, "experiment_code": "ABC-002", "compound": "EX0002_01",
        "row": 1, "by": "auto", "auto_row": 1, "ambiguous": True,
        "candidates": [{"row": 1, "name": "ABC-001", "eq": 1.0, "mass": "1.50 g"},
                       {"row": 2, "name": PHENYLETHYLAMINE, "eq": 1.0, "mass": "997 mg"}]}
    # Every other run has exactly one reactant with 1 equivalent.
    for code in ("ABC-001", "ABC-004", "ABC-003", "ABC-006", "ABC-008"):
        c = choice_of(r, code)
        assert c["by"] == "auto" and c["row"] == c["auto_row"] and c["ambiguous"] is False, c
    assert choice_of(r, "ABC-003")["row"] == 2
    assert choice_of(r, "ABC-003")["candidates"] == [
        {"row": 1, "name": "Methyllithium", "eq": 2.1, "mass": "421 mg"},
        {"row": 2, "name": "ABC-002", "eq": 1.0, "mass": "125 mg"}]
    run = run_of(captured.routes[0], "ABC-002")
    assert run.line == "ABC-002, SM = 1.50 g, 17.25 %"
    assert (run.compound, run.limiting_row, run.limiting_by, run.auto_row, run.ambiguous) == \
        ("EX0002_01", 1, "auto", 1, True)
    assert run.candidates[1] == Candidate(2, PHENYLETHYLAMINE, 1.0, "997 mg")
    json.dumps(r)                                                      # plain JSON


def test_route_uses_the_users_choice(example_home, monkeypatch):
    captured = Captured(monkeypatch)
    with service(example_home) as svc:
        svc.set_limiting(REF, 2)
        r = svc.route("EX0004_01", open=False)
    run = run_of(captured.routes[0], "ABC-002")
    assert run.line == "ABC-002, SM = 997 mg, 17.25 %"
    assert run.sm_grams == pytest.approx(0.997)
    c = choice_of(r, "ABC-002")
    assert (c["row"], c["by"], c["auto_row"], c["ambiguous"]) == (2, "user", 1, True)
    step = captured.routes[0].steps[1]
    assert [t.text for t in step.below] == ["EtOH - shared stock", "ABC-002, SM = 997 mg, 17.25 %",
                                            "ABC-004, SM = 1.25 g, 64.5 %"]
    assert not any("chosen earlier" in n for n in r["notes"])
    assert r["experiments"] == DRAWN

    # Choosing the automatic row explicitly is still the user's choice.
    with service(example_home) as svc:
        svc.set_limiting(REF, 1)
        assert choice_of(svc.route("EX0004_01", open=False), "ABC-002")["by"] == "user"
        svc.set_limiting(REF, None)
        assert choice_of(svc.route("EX0004_01", open=False), "ABC-002")["by"] == "auto"


def test_the_choice_survives_a_refresh_and_ignores_case(example_home, monkeypatch):
    captured = Captured(monkeypatch)
    with service(example_home) as svc:
        svc.set_limiting(REF, 2)
        # The extension sends the reaction again (same reactant, other case).
        svc.set_reaction(REF, renamed(ABC002["reaction"], 2, PHENYLETHYLAMINE.upper()))
        r = svc.route("EX0004_01", open=False)
    assert choice_of(r, "ABC-002")["by"] == "user"
    assert run_of(captured.routes[-1], "ABC-002").line == "ABC-002, SM = 997 mg, 17.25 %"


def test_a_stale_choice_is_ignored_with_a_note(example_home, monkeypatch):
    captured = Captured(monkeypatch)
    with service(example_home) as svc:
        svc.set_limiting(REF, 2)
        svc.set_reaction(REF, renamed(ABC002["reaction"], 2, "(R)-(+)-1-Phenylethylamine"))
        r = svc.route("EX0004_01", open=False)
        assert AUTO_NOTE in r["notes"]
        c = choice_of(r, "ABC-002")
        assert (c["row"], c["by"], c["auto_row"]) == (1, "auto", 1)
        assert c["candidates"][1]["name"] == "(R)-(+)-1-Phenylethylamine"
        assert run_of(captured.routes[-1], "ABC-002").line == "ABC-002, SM = 1.50 g, 17.25 %"
        # The choice itself is kept: once the row holds that reactant again it applies.
        assert svc.repo.get_limiting(REF) == (2, PHENYLETHYLAMINE)
        svc.set_reaction(REF, ABC002["reaction"])
        again = svc.route("EX0004_01", open=False)
    assert AUTO_NOTE not in again["notes"]
    assert choice_of(again, "ABC-002")["by"] == "user"


def test_a_choice_whose_row_is_gone_is_ignored(example_home, monkeypatch):
    Captured(monkeypatch)
    with service(example_home) as svc:
        svc.set_limiting(REF, 2)
        shorter = copy.deepcopy(ABC002["reaction"])
        shorter["reactants"] = shorter["reactants"][:1]
        svc.set_reaction(REF, shorter)
        r = svc.route("EX0004_01", open=False)
    assert AUTO_NOTE in r["notes"]
    c = choice_of(r, "ABC-002")
    assert c["by"] == "auto" and c["ambiguous"] is False and len(c["candidates"]) == 1


def test_end_to_end_with_the_real_writer(example_home):
    pytest.importorskip("compound_registry.cdxml")
    with service(example_home) as svc:
        svc.set_limiting(REF, 2)
        r = svc.route("EX0004_01", open=False)
    assert r["steps"] == 4 and r["file"] is not None and r["warnings"] == []
    assert choice_of(r, "ABC-002")["by"] == "user"
    assert "997 mg" in r["cdxml"]


# --------------------------------------------------------------------------- the builder alone

class _Data:
    """test_routes' Data plus a ``limiting_choice``."""

    def __init__(self, world, choices):
        from test_routes import Data

        self._inner = Data(world)
        self._choices = choices

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def limiting_choice(self, ref):
        value = self._choices.get(ref)
        if isinstance(value, Exception):
            raise value
        return value


def _two_ones_world(n1=1.0, n2=1.0, reference=(1.0, "mmol")):
    from test_routes import World, comp

    w = World()
    w.compound("X0001_01", "CCO")
    w.batch("X0001_01", "r1", "R-1", isolated=70.0)
    w.reaction("r1", [comp(1, "Alpha", "CC(=O)O", eq=1, n=n1, m=60),
                      comp(2, None, "OCC=O", formula="C2H4O2", eq=1, n=n2, m=1500),
                      comp(3, "Gamma", "CCCO", eq=2, n=2 * n1, m=120)],
               reference=reference)
    return w


def test_builder_tie_goes_to_the_first_row_and_is_ambiguous():
    (step,) = routes.build_route("X0001_01", _Data(_two_ones_world(), {})).steps
    run = step.runs[0]
    assert (run.limiting_row, run.limiting_by, run.auto_row, run.ambiguous) == (1, "auto", 1, True)
    assert run.line == "R-1, SM = 60.0 mg, 70 %"
    assert run.compound == "X0001_01"
    assert run.candidates == (Candidate(1, "Alpha", 1.0, "60.0 mg"),
                              Candidate(2, "C2H4O2", 1.0, "1.50 g"),
                              Candidate(3, "Gamma", 2.0, "120 mg"))


def test_builder_no_automatic_choice_leaves_sm_out():
    world = _two_ones_world(n1=5.0, n2=7.0)            # neither matches the reference amount
    (step,) = routes.build_route("X0001_01", _Data(world, {})).steps
    run = step.runs[0]
    assert (run.limiting_row, run.limiting_by, run.auto_row, run.ambiguous) == \
        (None, None, None, True)
    assert run.sm_grams is None and run.line == "R-1, 70 %"
    # The user's choice still applies (by its formula, the row has no name).
    (step,) = routes.build_route("X0001_01", _Data(world, {"r1": (2, "c2h4o2")})).steps
    run = step.runs[0]
    assert (run.limiting_row, run.limiting_by, run.auto_row) == (2, "user", None)
    assert run.line == "R-1, SM = 1.50 g, 70 %"


def test_builder_stale_or_broken_choices():
    world = _two_ones_world()
    route = routes.build_route("X0001_01", _Data(world, {"r1": (2, "Beta")}))
    assert route.steps[0].runs[0].limiting_by == "auto"
    assert route.notes == ("R-1: the starting material chosen earlier (row 2, Beta) is no "
                           "longer in the reaction; the automatic choice is used.",)
    route = routes.build_route("X0001_01", _Data(world, {"r1": (9, None)}))
    assert route.notes == ("R-1: the starting material chosen earlier (row 9) is no longer in "
                           "the reaction; the automatic choice is used.",)
    for broken in (RuntimeError("db"), ("1", "Alpha"), (True, None), [1, 2, 3], "x"):
        route = routes.build_route("X0001_01", _Data(world, {"r1": broken}))
        assert route.steps[0].runs[0].limiting_by == "auto" and route.notes == (), broken


def test_builder_choice_of_a_row_without_name_or_formula():
    from test_routes import World, comp

    w = World()
    w.compound("X0001_01", "CCO")
    w.batch("X0001_01", "r1", "R-1", isolated=70.0)
    w.reaction("r1", [comp(1, "Alpha", "CC(=O)O", eq=1, n=1.0, m=60),
                      comp(2, None, "OCC=O", eq=1.5, n=1.5, m=90)], reference=(1.0, "mmol"))
    (step,) = routes.build_route("X0001_01", _Data(w, {"r1": (2, None)})).steps
    run = step.runs[0]
    assert (run.limiting_row, run.limiting_by, run.auto_row, run.ambiguous) == (2, "user", 1, False)
    assert run.candidates[1] == Candidate(2, None, 1.5, "90.0 mg")
    # A recorded name no longer matches a row that lost its name.
    route = routes.build_route("X0001_01", _Data(w, {"r1": (2, "Beta")}))
    assert route.steps[0].runs[0].limiting_by == "auto" and len(route.notes) == 1


# --------------------------------------------------------------------------- host

def ask(home, op, params, **kwargs):
    body = json.dumps({"id": "l1", "v": 1, "op": op, "params": params}).encode("utf-8")
    return host.handle_message(body, HomeChoice(home, "host-config"), **kwargs).response


def test_host_set_limiting_round_trip(example_home, monkeypatch):
    doc = ask(example_home, "set_limiting", {"experiment_ref": REF, "row": 2})
    assert doc["ok"] is True and doc["id"] == "l1"
    assert doc["result"] == {"experiment_ref": REF, "row": 2, "name": PHENYLETHYLAMINE,
                             "changed": True, "warnings": []}
    assert ask(example_home, "set_limiting", {"experiment_ref": REF, "row": 2})["result"][
        "changed"] is False
    Captured(monkeypatch)
    routed = ask(example_home, "route", {"core_stereo_id": "EX0004_01", "open": False})
    assert routed["ok"] is True
    assert choice_of(routed["result"], "ABC-002")["by"] == "user"
    doc = ask(example_home, "set_limiting", {"experiment_ref": REF, "row": None})
    assert doc["result"]["row"] is None and doc["result"]["name"] is None
    assert doc["result"]["changed"] is True
    assert [e["row"] for e in events(example_home)] == [2, None]


def test_host_set_limiting_errors(example_home, tmp_path):
    assert ask(example_home, "set_limiting", {"experiment_ref": "424242", "row": 1})["error"][
        "code"] == "not_found"
    assert ask(example_home, "set_limiting", {"experiment_ref": REF, "row": 3})["error"][
        "code"] == "invalid_argument"
    for params in ({"experiment_ref": REF}, {"row": 1}, {"experiment_ref": REF, "row": 0},
                   {"experiment_ref": REF, "row": 1000}, {"experiment_ref": REF, "row": "2"},
                   {"experiment_ref": REF, "row": True}, {"experiment_ref": REF, "row": 1.5},
                   {"experiment_ref": REF, "row": 1, "other": 1},
                   {"experiment_ref": "", "row": 1}):
        res = ask(example_home, "set_limiting", params)
        assert res["ok"] is False and res["error"]["code"] == "bad_request", params
    assert events(example_home) == []
    nowhere = tmp_path / "nowhere"
    assert ask(nowhere, "set_limiting", {"experiment_ref": REF, "row": 1})["error"][
        "code"] == "registry_missing"
    assert not nowhere.exists()


def test_host_process_set_limiting(example_home):
    """The real host process: frames over pipes."""
    from test_host_subprocess import MODULE, frame, parse_frames, request

    env = dict(os.environ)
    env["COMPOUND_REGISTRY_HOME"] = str(example_home)
    data = (frame(request("set_limiting", {"experiment_ref": REF, "row": 2}, request_id=1))
            + frame(request("set_limiting", {"experiment_ref": REF, "row": 7}, request_id=2)))
    p = subprocess.run(MODULE, input=data, capture_output=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    docs = parse_frames(p.stdout)
    assert [d["id"] for d in docs] == [1, 2]
    assert docs[0]["ok"] is True and docs[0]["result"]["name"] == PHENYLETHYLAMINE
    assert docs[1]["ok"] is False and docs[1]["error"]["code"] == "invalid_argument"
    log = (example_home / "logs" / "host.log").read_text(encoding="utf-8")
    assert "op=set_limiting" in log and "Phenylethylamine" not in log


# --------------------------------------------------------------------------- CLI

def test_cli_limiting(example_home, capsys):
    base = ["--home", str(example_home)]
    assert main(base + ["limiting", "ABC-002", "2"]) == 0
    assert capsys.readouterr().out == (f"Starting material of ABC-002 (internal key {REF}): "
                                       f"reactant row 2 ({PHENYLETHYLAMINE}).\n")
    assert main(base + ["limiting", REF, "2"]) == 0
    assert capsys.readouterr().out == (f"Starting material of {REF}: reactant row 2 "
                                       f"({PHENYLETHYLAMINE}) (already so; nothing changed).\n")
    assert main(base + ["limiting", "abc-002", "AUTO", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"experiment_ref": REF, "row": None,
                                                   "name": None, "changed": True,
                                                   "warnings": []}
    assert main(base + ["limiting", "ABC-002", "auto"]) == 0
    assert "chosen automatically (already so; nothing changed)." in capsys.readouterr().out

    assert main(base + ["limiting", "ABC-002", "3"]) == 1
    assert "error [invalid_argument]" in capsys.readouterr().err
    assert main(base + ["--json", "limiting", "ABC-099", "1"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
    for bad in ("x", "0", "1000", "1.5"):
        assert main(base + ["limiting", "ABC-002", bad]) == 2           # usage error
        assert "ROW|auto" in capsys.readouterr().err
    assert [e["row"] for e in events(example_home)] == [2, None]
