"""Search by ID: every experiment in which a compound was made.

Covers ``IdFormat.parse_partial``, ``RegistryService.find_by_id``, the CLI
command ``experiments`` and the host operation ``find``.
"""

from __future__ import annotations

import json

import pytest

from compound_registry import cli, host, protocol
from compound_registry.cli import main
from compound_registry.errors import IdOverflowError, NotFoundError, RegistryError
from compound_registry.host import HomeChoice
from compound_registry.idformat import IdFormat
from compound_registry.service import RegistryService

AMINE_R = "C[C@H](N)c1ccccc1"
AMINE_S = "C[C@@H](N)c1ccccc1"
ETHANOL = "CCO"
URL = "https://sciformation.chemie.unibas.ch/startUseCase?useCase=performSearch&table=ElnReaction&pk="


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)


@pytest.fixture
def home(tmp_path):
    """A registry with: (R)-amine made in ABC-001 and ABC-003 (the ABC-003 batch
    retired and made again), (S)-amine in ABC-002, ethanol in ABC-003."""
    path = tmp_path / "home"
    with RegistryService.open(path) as svc:
        def reg(smiles, ref, code, index=1):
            return svc.register(smiles=smiles, experiment_ref=ref, experiment_code=code,
                                experiment_url=URL + ref, product_index=index, source="extension")["id"]

        assert reg(AMINE_R, "101", "ABC-001") == "000001_01_001"
        assert reg(AMINE_S, "102", "ABC-002") == "000001_02_001"
        assert reg(AMINE_R, "103", "ABC-003") == "000001_01_002"
        assert reg(ETHANOL, "103", "ABC-003", index=2) == "000002_01_001"
        svc.retire("000001_01_002", "wrong vial")
        assert reg(AMINE_R, "103", "ABC-003") == "000001_01_003"
    return path


# --------------------------------------------------------------------------- IdFormat.parse_partial

def test_parse_partial_accepts_the_three_forms():
    f = IdFormat()
    assert f.parse_partial("000042_02_003") == (42, 2, 3)
    assert f.parse_partial("000042_02") == (42, 2, None)
    assert f.parse_partial("000042") == (42, None, None)
    assert f.parse_partial("42") == (42, None, None)


@pytest.mark.parametrize("text", ["", "abc", "000042_", "000042_2", "42_02", "000042_02_3",
                                  "000042_02_003_1", "0000042", "000000", "000042_00",
                                  "000042_02_000", " 000042", "000042\n", "٤٢", None, 42])
def test_parse_partial_refuses_everything_else(text):
    with pytest.raises(NotFoundError):
        IdFormat().parse_partial(text)


def test_parse_partial_follows_a_custom_format():
    f = IdFormat("C{core:04d}-{stereo:01d}/{batch:02d}")
    assert f.parse_partial("C0007-1/02") == (7, 1, 2)
    assert f.parse_partial("C0007-1") == (7, 1, None)
    assert f.parse_partial("C7") == (7, None, None)
    # A lone CORE number may also be typed without the format's leading text.
    assert f.parse_partial("0007") == (7, None, None)
    assert f.parse_partial("7") == (7, None, None)
    for text in ("0007-1", "0007-1/02", "c7", "C", "C00007", "00007"):
        with pytest.raises(NotFoundError):
            f.parse_partial(text)


def test_a_prefixed_format_gives_prefixed_ids():
    f = IdFormat("EX{core:04d}_{stereo:02d}_{batch:03d}")
    assert f.format(1, 1, 1) == "EX0001_01_001"
    assert f.format_core_stereo(42, 2) == "EX0042_02" and f.format_core(42) == "EX0042"
    assert f.max_core == 9999
    assert f.parse("EX0042_02_003") == (42, 2, 3)
    assert f.parse_partial("EX0042_02") == (42, 2, None)
    assert f.parse_partial("EX0042") == f.parse_partial("EX42") == f.parse_partial("42") \
        == (42, None, None)
    for text in ("000042_02_003", "0042_02", "EX00042", "ex0042", "EX0000"):
        with pytest.raises(NotFoundError):
            f.parse_partial(text)
    with pytest.raises(IdOverflowError):
        f.format(10000, 1, 1)


# --------------------------------------------------------------------------- service

def codes(result):
    return [e["experiment_code"] for e in result["experiments"]]


def test_a_full_id_finds_every_experiment_of_that_compound(home):
    with RegistryService.open(home) as svc:
        r = svc.find_by_id("000001_01_001")
    assert r["level"] == "batch" and r["matched_id"] == "000001_01_001" and r["core_no"] == 1
    assert [v["core_stereo_id"] for v in r["variants"]] == ["000001_01"]
    v = r["variants"][0]
    assert [b["full_id"] for b in v["batches"]] == ["000001_01_001", "000001_01_002", "000001_01_003"]
    assert v["batch_count"] == 3 and v["stereo_mode"] == "abs" and v["svg"].startswith("<svg")
    assert "molfile" not in v["batches"][0]
    assert codes(r) == ["ABC-001", "ABC-003"]
    abc3 = r["experiments"][1]
    assert abc3["experiment_ref"] == "103" and abc3["experiment_url"] == URL + "103"
    assert [(b["full_id"], b["retired"]) for b in abc3["batches"]] == [
        ("000001_01_002", True), ("000001_01_003", False)]
    # the other enantiomer is named but its experiments are not mixed in
    assert [o["core_stereo_id"] for o in r["other_variants"]] == ["000001_02"]
    assert "batches" not in r["other_variants"][0]


def test_core_stereo_and_core_searches(home):
    with RegistryService.open(home) as svc:
        stereo = svc.find_by_id(" 000001_02 ")
        core = svc.find_by_id("1")
        padded = svc.find_by_id("000001")
        ethanol = svc.find_by_id("000002_01", with_svg=False)
    assert stereo["level"] == "stereo" and stereo["matched_id"] is None
    assert codes(stereo) == ["ABC-002"] and stereo["query"] == "000001_02"
    assert core["level"] == "core" and core["other_variants"] == []
    assert [v["core_stereo_id"] for v in core["variants"]] == ["000001_01", "000001_02"]
    # (the fixture registers everything within one second, so only the set is fixed)
    assert sorted(codes(core)) == ["ABC-001", "ABC-002", "ABC-003"] and len(core["experiments"]) == 3
    assert padded["experiments"] == core["experiments"]
    assert codes(ethanol) == ["ABC-003"] and ethanol["variants"][0]["svg"] is None
    assert ethanol["experiments"][0]["batches"][0]["product_index"] == 2


@pytest.mark.parametrize("text, part", [
    ("000009", "CORE number 9"), ("000001_05", "no stereo variant 5"),
    ("000001_01_009", "No batch"), ("not-an-id", "is not an ID"),
])
def test_find_reports_what_does_not_exist(home, text, part):
    with RegistryService.open(home) as svc, pytest.raises(NotFoundError) as info:
        svc.find_by_id(text)
    assert part in info.value.message


def test_find_needs_text(home):
    with RegistryService.open(home) as svc:
        for bad in ("", "   ", None, 12):
            with pytest.raises(RegistryError) as info:
                svc.find_by_id(bad)
            assert info.value.code == "invalid_argument"


def test_find_changes_nothing(home):
    journal = (home / "issued-ids.jsonl").read_bytes()
    with RegistryService.open(home) as svc:
        before = (svc.doctor()["stats"], [b["full_id"] for b in svc.experiment("103")])
        svc.find_by_id("000001")
        svc.find_by_id("000001_01_002")
        after = (svc.doctor()["stats"], [b["full_id"] for b in svc.experiment("103")])
    assert after == before
    assert (home / "issued-ids.jsonl").read_bytes() == journal


# --------------------------------------------------------------------------- CLI

def test_cli_experiments(home, capsys):
    assert main(["--home", str(home), "experiments", "000001_01_003"]) == 0
    out = capsys.readouterr().out
    assert "000001_01" in out and "ABC-001" in out and "ABC-003" in out and "ABC-002" not in out
    assert "000001_01_002 [RETIRED]" in out and URL + "101" in out
    assert "Other stereo variants" in out and "000001_02" in out
    out.encode("ascii")

    assert main(["--home", str(home), "--json", "experiments", "1"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["level"] == "core" and len(doc["experiments"]) == 3

    assert main(["--home", str(home), "experiments", "000007"]) == 1
    assert "error [not_found]" in capsys.readouterr().err


# --------------------------------------------------------------------------- host

def ask(home, op, params, v=protocol.PROTOCOL_VERSION):
    body = json.dumps({"id": "t1", "v": v, "op": op, "params": params}).encode("utf-8")
    return host.handle_message(body, HomeChoice(home, "host-config")).response


def test_host_find(home):
    doc = ask(home, "find", {"id": "000001_01"})
    assert doc["ok"] is True and doc["id"] == "t1"
    assert [e["experiment_code"] for e in doc["result"]["experiments"]] == ["ABC-001", "ABC-003"]
    assert doc["result"]["variants"][0]["svg"].startswith("<svg")
    json.dumps(doc)

    missing = ask(home, "find", {"id": "000050"})
    assert missing["ok"] is False and missing["error"]["code"] == "not_found"


@pytest.mark.parametrize("params", [{}, {"id": ""}, {"id": 5}, {"id": "x" * 201},
                                    {"id": "000001", "extra": 1}, {"id": "0000\n01"}])
def test_host_find_refuses_bad_params(home, params):
    doc = ask(home, "find", params)
    assert doc["ok"] is False and doc["error"]["code"] == "bad_request"
