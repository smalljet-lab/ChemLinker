"""Synthesis routes on the registry side: the RouteData adapter over the
repository (route_data.py), ``RegistryService.route`` (file written into the
compound's folder, never overwritten, opened or not), host ``route`` and the
CLI ``route``.

Most tests replace the route builder and the ChemDraw writer
(``RegistryService._route_tools``) with small fakes, so they check only what
the service does around them. The end-to-end tests at the bottom use the real
``routes`` and ``cdxml`` modules on the example data (tests/data/reactions_example.json).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

from compound_registry import cli, host, protocol
from compound_registry import service as service_mod
from compound_registry.cli import main
from compound_registry.config import load_config
from compound_registry.errors import NotFoundError, RegistryError
from compound_registry.host import HomeChoice
from compound_registry.route_data import RegistryRouteData
from compound_registry.route_model import (
    MissingReaction,
    Participant,
    Route,
    Run,
    Step,
    Text,
)
from compound_registry.service import NO_OPEN_ENV, RegistryService
from conftest import example_data

EXAMPLE = example_data()
EXPERIMENTS = EXAMPLE["experiments"]
MOMENT = datetime(2026, 11, 19, 14, 3, 5)
FILE_NAME = "EX0004_01_route_2026-11-19_140305.cdxml"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)


# --------------------------------------------------------------------------- fakes

def run(code: str, ref: str, percent: float, line: str) -> Run:
    return Run(experiment_ref=ref, experiment_code=code, experiment_url=None,
               yield_percent=percent, yield_kind="isolated", sm_grams=None, line=line)


def participant(cs: str) -> Participant:
    return Participant(key=f"cs:{cs}", molfile=None, name=None, label=cs, core_stereo_id=cs)


FAKE_ROUTE = Route(
    target=participant("EX0004_01"),
    steps=(
        Step(product=participant("EX0003_01"), reactants=(participant("EX0002_01"),),
             above=(), below=(Text("THF"),),
             runs=(run("ABC-003", "4130", 37.8, "ABC-003, 37.8 %"),
                   run(None, "4160", 76.2, "4160, 76.2 %"))),       # no ELN number: ref
        Step(product=participant("EX0004_01"), reactants=(participant("EX0003_01"),),
             above=(), below=(), runs=(run("ABC-008", "4180", 70.0, "ABC-008, 70 %"),)),
    ),
    missing=(MissingReaction("4120", "ABC-002", None, "EX0002_01"),),
    notes=("A note about ABC-002.",),
)
FAKE_TEXT = '<?xml version="1.0" encoding="UTF-8" ?>\n<CDXML><page>°C →</page></CDXML>\n'


class Fakes:
    """Stands in for routes.build_route and cdxml.render_route."""

    def __init__(self, route: Route = FAKE_ROUTE, text: str = FAKE_TEXT):
        self.route, self.text = route, text
        self.built: list[tuple[str, object]] = []
        self.rendered: list[Route] = []

    def build(self, target, data):
        self.built.append((target, data))
        return self.route

    def render(self, route):
        self.rendered.append(route)
        return self.text

    def install(self, monkeypatch) -> "Fakes":
        monkeypatch.setattr(RegistryService, "_route_tools",
                            staticmethod(lambda: (self.build, self.render)))
        return self


class Opener:
    def __init__(self, error: BaseException | None = None):
        self.calls: list[str] = []
        self.error = error

    def __call__(self, path: str) -> None:
        self.calls.append(path)
        if self.error is not None:
            raise self.error


def service(home: Path, opener=None, clock=lambda: MOMENT) -> RegistryService:
    return RegistryService(load_config(home), opener=opener, local_clock=clock)


def folder(home: Path, cs: str = "EX0004_01") -> Path:
    return load_config(home).files_dir / cs


# --------------------------------------------------------------------------- route data

@pytest.fixture
def open_service(example_home):
    with service(example_home) as svc:
        yield svc


def test_route_data_batches_and_yields(open_service):
    data = open_service._route_data()
    of = data.batches_of("EX0001_01")
    assert [b.full_id for b in of] == ["EX0001_01_001", "EX0001_01_002"]
    assert of[0].experiment_code == "ABC-001" and of[0].isolated == 52.4
    assert of[0].analytical is None and of[1].product_index == 2
    assert of[0].experiment_url == EXPERIMENTS["ABC-001"]["experiment_url"]
    # By product index within the experiment.
    assert [(b.full_id, b.product_index) for b in data.batches_in("4120")] == [
        ("EX0002_01_001", 1), ("EX0001_01_002", 2)]
    assert data.batches_of("EX0099_01") == () and data.batches_in("nope") == ()
    assert data.stereo_label("EX0003_01") == "EX0003_01"
    first = next(b for b in EXAMPLE["batches"] if b["full_id"] == "EX0003_01_001")
    assert data.stereo_molfile("EX0003_01") == first["molfile"]
    assert data.stereo_molfile("EX0099_01") is None


def test_route_data_leaves_out_retired_batches(example_home):
    with service(example_home) as svc:
        svc.retire("EX0003_01_001", "test")
        data = svc._route_data()
    assert [b.full_id for b in data.batches_of("EX0003_01")] == ["EX0003_01_002"]
    assert data.batches_in("4160") == ()
    second = next(b for b in EXAMPLE["batches"] if b["full_id"] == "EX0003_01_002")
    assert data.stereo_molfile("EX0003_01") == second["molfile"]


def test_route_data_reaction_is_a_fresh_copy(open_service):
    data = open_service._route_data()
    first = data.reaction("4120")
    assert first == EXPERIMENTS["ABC-002"]["reaction"]
    first["reactants"].clear()
    assert data.reaction("4120") == EXPERIMENTS["ABC-002"]["reaction"]
    assert data.reaction("nope") is None


def test_route_data_identify(open_service):
    data = open_service._route_data()
    abc008 = EXPERIMENTS["ABC-008"]["reaction"]["reactants"][0]
    assert abc008["source_experiment_ref"] is None             # no ELN link: by structure
    assert data.identify(abc008["molfile"]) == "EX0003_01"
    linked = EXPERIMENTS["ABC-003"]["reaction"]["reactants"][1]
    assert data.identify(linked["molfile"]) == "EX0002_01"
    bromide = EXPERIMENTS["ABC-001"]["reaction"]["reactants"][0]
    assert data.identify(bromide["molfile"]) is None            # not registered
    for junk in ("", "   ", "not a molfile", None, 5):
        assert data.identify(junk) is None                      # never raises


def test_route_data_identify_never_raises():
    def broken(molfile):
        raise RuntimeError("boom")

    data = RegistryRouteData([], {}, lambda ref: None, broken)
    assert data.identify("x") is None


# --------------------------------------------------------------------------- service

def test_route_writes_a_new_file_and_reports(example_home, monkeypatch):
    fakes = Fakes().install(monkeypatch)
    with service(example_home) as svc:
        r = svc.route("EX0004_01", open=False)
    assert set(r) == {"core_stereo_id", "file", "opened", "open_message", "steps",
                      "experiments", "missing", "notes", "choices", "cdxml", "warnings"}
    path = folder(example_home) / FILE_NAME
    assert r["file"] == {"path": str(path), "name": FILE_NAME}
    assert path.read_bytes() == FAKE_TEXT.encode("utf-8")       # UTF-8, newlines kept
    assert r["core_stereo_id"] == "EX0004_01" and r["steps"] == 2
    assert r["experiments"] == ["ABC-003", "4160", "ABC-008"]
    assert r["missing"] == [{"experiment_ref": "4120", "experiment_code": "ABC-002",
                             "experiment_url": None, "compound": "EX0002_01"}]
    assert r["notes"] == ["A note about ABC-002."]
    # One choice per run drawn; a run without the limiting fields (the fake
    # builder's) gives "nothing chosen" and the step's product as compound.
    assert [(c["experiment_ref"], c["experiment_code"], c["compound"]) for c in r["choices"]]         == [("4130", "ABC-003", "EX0003_01"), ("4160", None, "EX0003_01"),
            ("4180", "ABC-008", "EX0004_01")]
    assert r["choices"][0] == {"experiment_ref": "4130", "experiment_code": "ABC-003",
                               "compound": "EX0003_01", "row": None, "by": None,
                               "auto_row": None, "ambiguous": False, "candidates": []}
    assert r["cdxml"] == FAKE_TEXT and r["warnings"] == []
    assert r["opened"] is False and r["open_message"] is None
    target, data = fakes.built[0]
    assert target == "EX0004_01" and isinstance(data, RegistryRouteData)
    assert fakes.rendered == [FAKE_ROUTE]
    assert (folder(example_home) / "EX0004_01.mol").is_file()       # the folder as register makes it


def test_route_never_overwrites(example_home, monkeypatch):
    Fakes().install(monkeypatch)
    base = folder(example_home)
    base.mkdir(parents=True, exist_ok=True)
    (base / FILE_NAME).write_text("mine", encoding="utf-8")
    with service(example_home) as svc:
        names = [svc.route("EX0004_01", open=False)["file"]["name"] for _ in range(3)]
    assert names == ["EX0004_01_route_2026-11-19_140305_2.cdxml",
                     "EX0004_01_route_2026-11-19_140305_3.cdxml",
                     "EX0004_01_route_2026-11-19_140305_4.cdxml"]
    assert (base / FILE_NAME).read_text(encoding="utf-8") == "mine"


def test_route_uses_the_local_time_now_by_default(example_home, monkeypatch):
    Fakes().install(monkeypatch)
    before = datetime.now().replace(microsecond=0)
    with RegistryService(load_config(example_home)) as svc:
        name = svc.route("EX0004_01", open=False)["file"]["name"]
    stamp = datetime.strptime(name, "EX0004_01_route_%Y-%m-%d_%H%M%S.cdxml")
    assert before <= stamp <= datetime.now()


def test_route_without_steps_writes_nothing(example_home, monkeypatch):
    empty = Route(target=participant("EX0001_01"), steps=(),
                  missing=(MissingReaction("4110", "ABC-001", None, "EX0001_01"),))
    fakes = Fakes(route=empty).install(monkeypatch)
    with service(example_home, opener=Opener()) as svc:
        quiet = svc.route("EX0001_01", open=False)
        loud = svc.route("EX0001_01")
    assert fakes.rendered == []
    for r in (quiet, loud):
        assert r["file"] is None and r["steps"] == 0 and r["experiments"] == []
        assert r["cdxml"] is None and r["opened"] is False and r["warnings"] == []
        assert r["missing"][0]["experiment_code"] == "ABC-001"
    assert quiet["open_message"] is None
    assert "nothing to draw" in loud["open_message"]
    assert not any(folder(example_home, "EX0001_01").glob("*.cdxml"))


def test_route_opening(example_home, monkeypatch):
    Fakes().install(monkeypatch)
    opener = Opener()
    with service(example_home, opener=opener) as svc:
        # The test environment forbids opening (tests/conftest.py).
        r = svc.route("EX0004_01")
        assert r["opened"] is False and NO_OPEN_ENV in r["open_message"] and opener.calls == []
        monkeypatch.delenv(NO_OPEN_ENV)
        r = svc.route("EX0004_01")
        assert r["opened"] is True and r["open_message"] is None
        assert opener.calls == [r["file"]["path"]]
        r = svc.route("EX0004_01", open=False)
        assert r["opened"] is False and r["open_message"] is None and len(opener.calls) == 1
        monkeypatch.setenv(NO_OPEN_ENV, "")          # empty counts as not set
        assert svc.route("EX0004_01")["opened"] is True


def test_route_opening_failure_is_a_message(example_home, monkeypatch):
    Fakes().install(monkeypatch)
    monkeypatch.delenv(NO_OPEN_ENV)
    failing = Opener(OSError(1155, "No application is associated with the specified file"))
    with service(example_home, opener=failing) as svc:
        r = svc.route("EX0004_01")
    assert r["opened"] is False and r["file"] is not None
    assert "No application is associated" in r["open_message"]
    assert "ChemDraw" in r["open_message"] and r["warnings"] == []


def test_route_default_opener_is_windows_only(example_home, monkeypatch):
    Fakes().install(monkeypatch)
    monkeypatch.delenv(NO_OPEN_ENV)
    monkeypatch.delattr(os, "startfile", raising=False)
    with service(example_home) as svc:
        r = svc.route("EX0004_01")
    assert r["opened"] is False and "only opened automatically on Windows" in r["open_message"]


def test_route_default_opener_on_windows_leaves_the_job(example_home, monkeypatch):
    """Firefox kills the host's Job object when it closes the host, so the
    default opener must not be os.startfile (ChemDraw would die with the
    host) but open_outside_job."""
    Fakes().install(monkeypatch)
    monkeypatch.delenv(NO_OPEN_ENV)
    monkeypatch.setattr(os, "name", "nt")
    started: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p), raising=False)
    used = Opener()
    monkeypatch.setattr(service_mod, "open_outside_job", used)
    with service(example_home) as svc:
        r = svc.route("EX0004_01")
    assert r["opened"] is True and used.calls == [r["file"]["path"]] and started == []


class FakeProcess:
    def __init__(self, returncode: int = 0, err: bytes = b"", timeout: bool = False):
        self.returncode, self.err, self.timeout = returncode, err, timeout
        self.waited: list[float | None] = []

    def communicate(self, timeout=None):
        self.waited.append(timeout)
        if self.timeout:
            raise subprocess.TimeoutExpired("helper", timeout)
        return None, self.err


class FakePopen:
    """Records every start; ``refuse`` makes the helper start fail (as when
    the job does not allow breakaway)."""

    def __init__(self, process: FakeProcess | None = None, refuse: BaseException | None = None):
        self.process = process or FakeProcess()
        self.refuse = refuse
        self.calls: list[tuple[object, dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.refuse is not None and isinstance(args, list):
            raise self.refuse
        return self.process


PATH = r"C:\R&D, 100%\EX0004_01\EX0004_01_route.cdxml"


def test_open_outside_job_breaks_away_from_the_job():
    popen = FakePopen()
    service_mod.open_outside_job(PATH, popen=popen, executable=r"C:\py\python.exe")
    (args, kwargs), = popen.calls
    assert args == [r"C:\py\python.exe", "-I", "-c", service_mod._OPEN_HELPER, PATH]
    assert kwargs["creationflags"] & service_mod.CREATE_BREAKAWAY_FROM_JOB
    assert kwargs["creationflags"] & service_mod.CREATE_NO_WINDOW
    # The host's stdin/stdout carry the native messages: never handed down.
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.PIPE and kwargs["close_fds"] is True
    assert popen.process.waited == [service_mod._OPEN_HELPER_TIMEOUT]
    assert service_mod.CREATE_BREAKAWAY_FROM_JOB == 0x01000000
    assert service_mod.CREATE_NO_WINDOW == 0x08000000


def test_open_outside_job_uses_this_python_by_default():
    popen = FakePopen()
    service_mod.open_outside_job(PATH, popen=popen)
    assert popen.calls[0][0][0] == sys.executable


@pytest.mark.parametrize("refusal", [PermissionError(13, "Access is denied"),
                                     FileNotFoundError(2, "No such file")])
def test_open_outside_job_falls_back_to_explorer(refusal):
    popen = FakePopen(refuse=refusal)
    service_mod.open_outside_job(PATH, popen=popen, executable="python.exe")
    assert len(popen.calls) == 2
    args, kwargs = popen.calls[1]
    assert args == f'explorer.exe "{PATH}"'          # quoted whole: ',' and '&' stay in the path
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["stdout"] is subprocess.DEVNULL
    assert not kwargs["creationflags"] & service_mod.CREATE_BREAKAWAY_FROM_JOB


def test_open_outside_job_without_a_python_uses_explorer():
    popen = FakePopen()
    service_mod.open_outside_job(PATH, popen=popen, executable="")
    assert [args for args, _ in popen.calls] == [f'explorer.exe "{PATH}"']


def test_open_outside_job_reports_why_windows_did_not_open():
    err = ("Traceback (most recent call last):\r\n  File \"<string>\", line 1, in <module>\r\n"
           "OSError: [WinError 1155] No application is associated with the specified file "
           f"for this operation: '{PATH}'\r\n").encode("utf-8")
    popen = FakePopen(FakeProcess(returncode=1, err=err))
    with pytest.raises(OSError) as caught:
        service_mod.open_outside_job(PATH, popen=popen, executable="python.exe")
    assert str(caught.value) == ("No application is associated with the specified file "
                                 "for this operation")
    silent = FakePopen(FakeProcess(returncode=3))
    with pytest.raises(OSError, match="ended with code 3"):
        service_mod.open_outside_job(PATH, popen=silent, executable="python.exe")


def test_open_outside_job_slow_helper_counts_as_opened():
    popen = FakePopen(FakeProcess(timeout=True))
    service_mod.open_outside_job(PATH, popen=popen, executable="python.exe")
    assert len(popen.calls) == 1                     # no second try through explorer


@pytest.mark.skipif(os.name != "nt", reason="the helper uses os.startfile (Windows only)")
def test_open_outside_job_real_helper_reports_a_missing_file(tmp_path):
    """The real helper process, started with the breakaway flag, on a file
    that does not exist: nothing is opened and the reason comes back."""
    asked_explorer: list[object] = []

    def popen(args, **kwargs):
        if not isinstance(args, list):
            asked_explorer.append(args)              # never really run explorer here
            return FakeProcess()
        return subprocess.Popen(args, **kwargs)

    with pytest.raises(OSError) as caught:
        service_mod.open_outside_job(str(tmp_path / "missing.cdxml"), popen=popen)
        if asked_explorer:
            pytest.skip("this test runner's job does not allow breakaway")
    assert "cannot find the file" in str(caught.value)
    assert asked_explorer == []


def test_route_folder_problem_is_a_warning(example_home, monkeypatch):
    Fakes().install(monkeypatch)
    blocker = folder(example_home)
    shutil.rmtree(blocker)
    blocker.write_text("a file in the way", encoding="utf-8")
    with service(example_home, opener=Opener()) as svc:
        r = svc.route("EX0004_01")
    assert r["file"] is None and r["opened"] is False
    assert len(r["warnings"]) == 1 and "in the way" in r["warnings"][0]
    assert "not written" in r["warnings"][0]
    assert r["open_message"] == "The ChemDraw file could not be written, so nothing was opened."
    assert r["steps"] == 2 and r["cdxml"] == FAKE_TEXT         # the text is still sent


def test_route_write_problem_leaves_no_partial_file(example_home, monkeypatch):
    Fakes(text="<CDXML>\ud800</CDXML>").install(monkeypatch)   # cannot be encoded as UTF-8
    with service(example_home) as svc:
        r = svc.route("EX0004_01", open=False)
    assert r["file"] is None and "could not be written" in r["warnings"][0]
    assert not any(folder(example_home).glob("*.cdxml"))


def test_route_document_too_large_to_send(example_home, monkeypatch):
    Fakes().install(monkeypatch)
    monkeypatch.setattr(protocol, "MAX_ROUTE_CDXML_CHARS", 10)
    with service(example_home) as svc:
        r = svc.route("EX0004_01", open=False)
    assert r["cdxml"] is None and r["file"] is not None
    assert "too large" in r["warnings"][0]


def test_route_unknown_or_blank_stereo_variant(example_home, monkeypatch):
    fakes = Fakes().install(monkeypatch)
    with service(example_home) as svc:
        with pytest.raises(NotFoundError):
            svc.route("EX0099_01")
        with pytest.raises(NotFoundError):
            svc.route("EX0004_01_001")             # a full ID is not a stereo variant
        with pytest.raises(RegistryError) as info:
            svc.route("  ")
        assert info.value.code == "invalid_argument"
    assert fakes.built == []


def registry_rows(home: Path) -> dict[str, list]:
    with closing(sqlite3.connect((home / "registry.sqlite").resolve().as_uri() + "?mode=ro",
                                 uri=True)) as c:
        return {table: c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("events", "batches", "stereos", "yields", "reactions", "names")}


def test_route_changes_nothing_in_the_registry(example_home, monkeypatch):
    Fakes().install(monkeypatch)
    before = registry_rows(example_home)
    journal = (example_home / "issued-ids.jsonl").read_bytes()
    with service(example_home) as svc:
        svc.route("EX0004_01", open=False)
    assert registry_rows(example_home) == before
    assert (example_home / "issued-ids.jsonl").read_bytes() == journal


# --------------------------------------------------------------------------- host

def ask(home, op, params, **kwargs):
    body = json.dumps({"id": "q1", "v": 1, "op": op, "params": params}).encode("utf-8")
    return host.handle_message(body, HomeChoice(home, "host-config"), **kwargs).response


def test_host_route(example_home, monkeypatch):
    Fakes().install(monkeypatch)
    opener = Opener()
    monkeypatch.delenv(NO_OPEN_ENV)
    opened = ask(example_home, "route", {"core_stereo_id": "EX0004_01"},
                 open_service=lambda home: service(home, opener=opener))
    assert opened["ok"] is True and opened["result"]["opened"] is True
    assert opener.calls == [opened["result"]["file"]["path"]]
    doc = ask(example_home, "route", {"core_stereo_id": "EX0004_01", "open": False},
              open_service=lambda home: service(home, opener=opener))
    assert doc["ok"] is True and doc["result"]["opened"] is False and len(opener.calls) == 1
    assert doc["result"]["file"]["name"] == "EX0004_01_route_2026-11-19_140305_2.cdxml"
    assert ask(example_home, "route", {"core_stereo_id": "EX0099_01"})["error"]["code"] == \
        "not_found"
    for bad in ({}, {"core_stereo_id": "EX0004_01", "open": "yes"},
                {"core_stereo_id": "EX0004_01", "other": 1}):
        assert ask(example_home, "route", bad)["error"]["code"] == "bad_request"


def test_host_drops_the_document_when_the_answer_is_too_large():
    result = {"core_stereo_id": "X_01", "file": {"path": "p", "name": "n"}, "opened": False,
              "open_message": None, "steps": 1, "experiments": ["ABC-001"], "missing": [],
              "notes": [], "cdxml": "x" * 5000, "warnings": []}
    response = protocol.ok_response(7, result)
    data = host.encode_response(json.loads(json.dumps(response)), "route", max_bytes=2000)
    doc = json.loads(data)
    assert doc["ok"] is True and doc["result"]["cdxml"] is None
    assert doc["result"]["warnings"] == [host.ROUTE_DOCUMENT_DROPPED]
    assert doc["result"]["file"] == {"path": "p", "name": "n"}
    # Small enough: sent as is.
    assert json.loads(host.encode_response(response, "route"))["result"]["cdxml"] == "x" * 5000
    # Still too large without the document: response_too_large.
    result["notes"] = ["n" * 3000]
    doc = json.loads(host.encode_response(protocol.ok_response(7, result), "route",
                                          max_bytes=2000))
    assert doc["ok"] is False and doc["error"]["code"] == "response_too_large"


# --------------------------------------------------------------------------- CLI

def test_cli_route(example_home, monkeypatch, capsys):
    Fakes().install(monkeypatch)
    base = ["--home", str(example_home)]
    assert main(base + ["route", "EX0004_01", "--no-open"]) == 0
    text = capsys.readouterr().out
    assert "2 step(s) drawn for EX0004_01: ABC-003, 4160, ABC-008" in text
    assert f"File: {folder(example_home)}" in text and "Not opened." in text
    assert "  - ABC-002 (makes EX0002_01)" in text and "  - A note about ABC-002." in text

    assert main(base + ["route", "EX0004_01"]) == 0           # opening is off in tests
    assert NO_OPEN_ENV in capsys.readouterr().out

    assert main(base + ["--json", "route", "EX0004_01", "--no-open"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["steps"] == 2 and doc["file"]["name"].endswith(".cdxml")
    assert len(list(folder(example_home).glob("*.cdxml"))) == 3

    assert main(base + ["route", "EX0099_01"]) == 1
    assert "error [not_found]" in capsys.readouterr().err


def test_cli_route_exit_code_when_the_file_cannot_be_written(example_home, monkeypatch, capsys):
    Fakes().install(monkeypatch)
    blocker = folder(example_home)
    shutil.rmtree(blocker)
    blocker.write_text("in the way", encoding="utf-8")
    assert main(["--home", str(example_home), "route", "EX0004_01", "--no-open"]) == 1
    assert "Warnings:" in capsys.readouterr().out


# --------------------------------------------------------------------------- end to end

def real_tools():
    pytest.importorskip("compound_registry.routes")
    pytest.importorskip("compound_registry.cdxml")


def test_the_real_builder_over_the_registry(example_home, monkeypatch):
    """routes.build_route over this adapter gives the 4 steps of the example (the
    ChemDraw writer replaced, so only the builder and the service meet)."""
    routes = pytest.importorskip("compound_registry.routes")
    rendered = []

    def render(route):
        rendered.append(route)
        return "<CDXML/>"

    monkeypatch.setattr(RegistryService, "_route_tools",
                        staticmethod(lambda: (routes.build_route, render)))
    with service(example_home) as svc:
        r = svc.route("EX0004_01", open=False)
    assert r["steps"] == 4 and r["missing"] == []
    assert r["experiments"] == ["ABC-001", "ABC-002", "ABC-004", "ABC-003", "ABC-006", "ABC-008"]
    assert any("ABC-002" in note for note in r["notes"])
    steps = rendered[0].steps
    assert [s.product.core_stereo_id for s in steps] == ["EX0001_01", "EX0002_01",
                                                         "EX0003_01", "EX0004_01"]
    assert [t.text for t in steps[0].below] == ["DME", "ABC-001, SM = 2.31 g, 52.4 %"]
    assert [t.text for t in steps[1].below] == ["EtOH - shared stock",
                                                "ABC-002, SM = 1.50 g, 17.25 %",
                                                "ABC-004, SM = 1.25 g, 64.5 %"]
    assert [t.text for t in steps[3].above] == ["Cyclohexene", "Palladium hydroxide on carbon 20wt%"]
    assert [t.text for t in steps[3].below] == ["ABC-008, SM = 85.0 mg, 70 %"]
    assert (folder(example_home) / FILE_NAME).read_text(encoding="utf-8") == "<CDXML/>"


def test_end_to_end_ex0004(example_home, monkeypatch):
    """The example data: EX0004_01 was made in 4 steps from ABC-001 on."""
    real_tools()
    with service(example_home) as svc:
        r = svc.route("EX0004_01", open=False)
    assert r["steps"] == 4, r
    assert r["experiments"] == ["ABC-001", "ABC-002", "ABC-004", "ABC-003", "ABC-006", "ABC-008"]
    assert r["missing"] == [] and r["warnings"] == []
    assert any("ABC-002" in note for note in r["notes"]), r["notes"]
    path = Path(r["file"]["path"])
    assert path.parent == folder(example_home) and path.name == FILE_NAME and path.is_file()
    root = ET.parse(path).getroot()
    assert root.tag == "CDXML"
    assert r["cdxml"] == path.read_text(encoding="utf-8")

    # A second call writes a second file; the first one is untouched.
    first = path.read_bytes()
    with service(example_home) as svc:
        again = svc.route("EX0004_01", open=False)
    assert again["file"]["name"] == "EX0004_01_route_2026-11-19_140305_2.cdxml"
    assert path.read_bytes() == first
    assert len(list(folder(example_home).glob("*.cdxml"))) == 2


def test_end_to_end_missing_reaction(example_home):
    """Without the reaction of ABC-003 the route lists it as missing."""
    real_tools()
    with closing(sqlite3.connect(str(example_home / "registry.sqlite"), isolation_level=None)) as c:
        c.execute("DELETE FROM reactions WHERE experiment_ref = ?",
                  (EXPERIMENTS["ABC-003"]["experiment_ref"],))
    with service(example_home) as svc:
        r = svc.route("EX0004_01", open=False)
    assert [m["experiment_code"] for m in r["missing"]] == ["ABC-003"]
    assert r["missing"][0] == {"experiment_ref": EXPERIMENTS["ABC-003"]["experiment_ref"],
                               "experiment_code": "ABC-003",
                               "experiment_url": EXPERIMENTS["ABC-003"]["experiment_url"],
                               "compound": "EX0003_01"}
    assert "ABC-003" not in r["experiments"] and "ABC-006" in r["experiments"]


def test_end_to_end_through_the_host_and_cli(example_home, capsys):
    real_tools()
    doc = ask(example_home, "route", {"core_stereo_id": "EX0004_01", "open": False})
    assert doc["ok"] is True and doc["result"]["steps"] == 4
    assert ET.fromstring(doc["result"]["cdxml"]).tag == "CDXML"
    assert main(["--home", str(example_home), "route", "EX0004_01", "--no-open"]) == 0
    assert "4 step(s) drawn for EX0004_01: ABC-001, ABC-002, ABC-004, ABC-003, ABC-006, " \
           "ABC-008" in capsys.readouterr().out
