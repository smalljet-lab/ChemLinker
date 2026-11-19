"""The compound list: ``RegistryService.compounds``, CLI ``compounds``, host ``compounds``.

The matching rules are protocol.py, "Compound search"; each reason, the AND
between text and drawn structure, paging, ordering, notes and
``variants[].matched`` are covered here. Also the oversize handling of the
host for ``compounds`` and ``find``.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import sqlite3
import struct
import types
from contextlib import closing
from pathlib import Path

import pytest
from rdkit import Chem
from rdkit.Chem import rdDepictor

from compound_registry import cli, host, protocol
from compound_registry.cli import main
from compound_registry.config import load_config
from compound_registry.errors import RegistryError
from compound_registry.host import HomeChoice, encode_response, serve
from compound_registry.service import RegistryService

AMINE_R = "C[C@H](N)c1ccccc1"
AMINE_S = "C[C@@H](N)c1ccccc1"
AMINE_FLAT = "CC(N)c1ccccc1"
ETHANOL = "CCO"
PROPANOL = "CCCO"
NAME_S = "(1S)-1-phenylethan-1-amine"
URL = "https://sciformation.chemie.unibas.ch/exp?pk="

ITEM_KEYS = {"core_no", "core_id", "formula", "properties", "svg", "variants", "batch_count",
             "experiment_count", "experiments", "first_created_at", "last_created_at",
             "matched_by"}
VARIANT_KEYS = {"core_stereo_id", "stereo_no", "stereo_mode", "smiles", "inchi", "inchikey",
                "name", "batch_count", "matched"}


def molfile(smiles: str, explicit_h: bool = False) -> str:
    mol = Chem.MolFromSmiles(smiles)
    if explicit_h:
        mol = Chem.AddHs(mol)
    rdDepictor.Compute2DCoords(mol)
    return Chem.MolToMolBlock(mol)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)


@pytest.fixture(scope="module")
def _template(tmp_path_factory):
    """Built once per module (registering is the slow part), copied per test.

    000001 (R)-amine: _01 in ABC-001 and ABC-004 (that batch retired),
                      _02 = (S)-amine in ABC-002, named NAME_S
    000002 ethanol in ABC-003 (product 1), named "Ethanol absolute"
    000003 (R)-amine hydrochloride in ABC-003 (product 2)
    000004 propan-1-ol in XYZ-010
    """
    base = tmp_path_factory.mktemp("compounds")
    path = base / "home"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("COMPOUND_REGISTRY_HOME", str(base / "env-home"))
        for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            mp.delenv(var, raising=False)
        with RegistryService.open(path) as svc:
            def reg(smiles, ref, code, index=1):
                return svc.register(smiles=smiles, experiment_ref=ref, experiment_code=code,
                                    experiment_url=URL + ref, product_index=index)["id"]

            assert reg(AMINE_R, "101", "ABC-001") == "000001_01_001"
            assert reg(AMINE_S, "102", "ABC-002") == "000001_02_001"
            assert reg(ETHANOL, "103", "ABC-003") == "000002_01_001"
            assert reg(AMINE_R + ".Cl", "103", "ABC-003", 2) == "000003_01_001"
            assert reg(AMINE_R, "104", "ABC-004") == "000001_01_002"
            svc.retire("000001_01_002", "wrong vial")
            assert reg(PROPANOL, "105", "XYZ-010") == "000004_01_001"
            svc.set_name("000001_02", NAME_S)
            svc.set_name("000002_01", "Ethanol absolute")
    return path


@pytest.fixture
def home(_template, tmp_path) -> Path:
    target = tmp_path / "home"
    shutil.copytree(_template, target)
    return target


@pytest.fixture
def svc(home):
    s = RegistryService.open(home)
    yield s
    s.close()


def find(svc, **kw) -> dict:
    kw.setdefault("with_svg", False)
    return svc.compounds(**kw)


def cores(result) -> list[int]:
    return [i["core_no"] for i in result["items"]]


def item(result, core_no) -> dict:
    return next(i for i in result["items"] if i["core_no"] == core_no)


def matched(result, core_no) -> dict[str, bool]:
    return {v["core_stereo_id"]: v["matched"] for v in item(result, core_no)["variants"]}


# --------------------------------------------------------------------------- plain listing

def test_plain_listing(svc):
    r = svc.compounds()
    assert set(r) == {"total", "registry_total", "offset", "limit", "items", "notes", "warnings"}
    assert (r["total"], r["registry_total"], r["offset"], r["limit"]) == (
        4, 4, 0, protocol.DEFAULT_PAGE_SIZE)
    assert cores(r) == [1, 2, 3, 4] and r["notes"] == [] and r["warnings"] == []
    one = r["items"][0]
    assert set(one) == ITEM_KEYS
    assert one["core_id"] == "000001" and one["formula"] == "C8H11N" and one["matched_by"] == []
    assert one["properties"]["sum_formula"] == "C8H11N"
    assert one["properties"]["mol_weight"] == 121.183
    assert one["svg"].startswith("<svg")
    assert one["batch_count"] == 3 and one["experiment_count"] == 3   # retired batch included
    assert {e["experiment_code"] for e in one["experiments"]} == {"ABC-001", "ABC-002", "ABC-004"}
    assert one["experiments"][0] == {"experiment_ref": "101", "experiment_code": "ABC-001",
                                     "experiment_url": URL + "101"}
    assert one["first_created_at"] <= one["last_created_at"]
    v1, v2 = one["variants"]
    assert set(v1) == VARIANT_KEYS
    assert (v1["core_stereo_id"], v1["stereo_no"], v1["batch_count"]) == ("000001_01", 1, 2)
    assert v1["name"] is None and v2["name"]["text"] == NAME_S
    assert v1["inchi"].startswith("InChI=1S/C8H11N/") and v1["inchi"] != v2["inchi"]
    assert v1["matched"] is True and v2["matched"] is True    # always true without a search
    salt = r["items"][2]
    assert salt["formula"] == "C8H11N.ClH" and salt["properties"]["sum_formula"] == "C8H12ClN"
    assert [e["experiment_code"] for e in salt["experiments"]] == ["ABC-003"]
    json.dumps(r)


def test_without_svg(svc):
    assert all(i["svg"] is None for i in find(svc)["items"])


def test_paging_and_order(svc):
    assert cores(find(svc, offset=1, limit=2)) == [2, 3]
    r = find(svc, order="desc", limit=3)
    assert cores(r) == [4, 3, 2] and r["total"] == 4 and r["limit"] == 3
    assert cores(find(svc, order="desc", offset=3)) == [1]
    beyond = find(svc, offset=10)
    assert beyond["items"] == [] and beyond["total"] == 4 and beyond["offset"] == 10
    every = find(svc, limit=None)
    assert cores(every) == [1, 2, 3, 4] and every["limit"] == 4


def test_paging_applies_after_the_search(svc):
    r = find(svc, text="ABC-00", order="desc", limit=1, offset=1)
    assert r["total"] == 3 and cores(r) == [2]   # ABC-00x: cores 1, 2, 3


def test_at_most_ten_experiments_per_item(tmp_path):
    with RegistryService.open(tmp_path / "h") as svc:
        for n in range(12):
            svc.register(smiles=ETHANOL, experiment_ref=f"E{n}", experiment_code=f"C-{n}")
        r = svc.compounds(with_svg=False)
    e = r["items"][0]
    assert e["experiment_count"] == 12 and len(e["experiments"]) == protocol.MAX_ITEM_EXPERIMENTS
    assert e["batch_count"] == 12 and e["variants"][0]["batch_count"] == 12


def test_empty_registry(tmp_path):
    with RegistryService.open(tmp_path / "empty") as svc:
        r = svc.compounds()
        assert (r["total"], r["registry_total"], r["items"], r["notes"]) == (0, 0, [], [])
        s = svc.compounds(text="CCO", smiles="c1ccccc1")
        assert s["total"] == 0 and s["items"] == [] and s["notes"]


def test_compounds_changes_nothing(svc, home):
    journal = (home / "issued-ids.jsonl").read_bytes()
    with closing(sqlite3.connect(str(home / "registry.sqlite"))) as c:
        before = c.execute("SELECT count(*) FROM events").fetchone()[0]
    find(svc, text="ABC-003", smiles="c1ccccc1")
    find(svc, smiles=AMINE_S, match="exact")
    find(svc, text="C8H12ClN")
    with closing(sqlite3.connect(str(home / "registry.sqlite"))) as c:
        assert c.execute("SELECT count(*) FROM events").fetchone()[0] == before
    assert (home / "issued-ids.jsonl").read_bytes() == journal


# --------------------------------------------------------------------------- text: each reason

def test_id_reasons(svc):
    r = find(svc, text="000002")
    assert cores(r) == [2] and r["items"][0]["matched_by"] == ["id"]
    assert matched(r, 2) == {"000002_01": False}   # the CORE matched, not the variant
    assert "Read as an ID of CORE 000002." in r["notes"]

    r = find(svc, text="000001_02")
    assert cores(r) == [1] and matched(r, 1) == {"000001_01": False, "000001_02": True}

    r = find(svc, text="000001_01_002")      # a retired batch still names its compound
    assert cores(r) == [1] and matched(r, 1) == {"000001_01": True, "000001_02": False}

    r = find(svc, text="3")                   # CORE 3, and "3" occurs in "ABC-003"
    assert cores(r) == [2, 3] and item(r, 3)["matched_by"] == ["id", "experiment"]


@pytest.mark.parametrize("text", ["000001_01_009", "000001_05", "000009", "99"])
def test_id_that_is_not_registered(svc, text):
    r = find(svc, text=text)
    assert r["total"] == 0
    assert "Read as an ID, but nothing with this ID is registered." in r["notes"]


def test_inchikey_reasons(svc):
    s = svc.analyze(smiles=AMINE_S)["structure"]
    r = find(svc, text=s["inchikey"].lower())       # case does not matter for a full key
    assert cores(r) == [1] and item(r, 1)["matched_by"] == ["inchikey"]
    assert matched(r, 1) == {"000001_01": False, "000001_02": True}
    assert r["notes"] == ["Read as an InChIKey."]

    r = find(svc, text=s["inchikey"][:14])          # first block: every stereo variant
    assert cores(r) == [1] and matched(r, 1) == {"000001_01": True, "000001_02": True}
    assert "first block" in r["notes"][0]

    r = find(svc, text=s["core_inchikey"])          # the CORE's own InChIKey
    assert cores(r) == [1] and matched(r, 1) == {"000001_01": False, "000001_02": False}

    assert find(svc, text="AAAAAAAAAAAAAA-BBBBBBBBBB-N")["total"] == 0


def test_lowercase_word_of_14_letters_is_not_read_as_an_inchikey_block(svc):
    r = find(svc, text="trimethylamine")
    assert r["total"] == 0 and r["notes"] == []


def test_structure_reason_smiles(svc):
    r = find(svc, text=AMINE_FLAT)
    assert cores(r) == [1] and item(r, 1)["matched_by"] == ["structure"]
    assert matched(r, 1) == {"000001_01": False, "000001_02": False}
    assert r["notes"][0].startswith("Read as SMILES (")

    r = find(svc, text=AMINE_S)                       # stereo ignored, but the exact variant is marked
    assert cores(r) == [1] and matched(r, 1) == {"000001_01": False, "000001_02": True}

    assert cores(find(svc, text=AMINE_R + ".Cl")) == [3]    # a salt is another CORE
    assert cores(find(svc, text="OCC")) == [2]


def test_structure_reason_inchi(svc):
    s = svc.analyze(smiles=AMINE_S)["structure"]
    r = find(svc, text=s["inchi"])
    assert cores(r) == [1] and item(r, 1)["matched_by"] == ["structure"]
    assert matched(r, 1) == {"000001_01": False, "000001_02": True}
    assert r["notes"][0].startswith("Read as an InChI")
    ethanol = svc.analyze(smiles=ETHANOL)["structure"]["inchi"]
    assert cores(find(svc, text=ethanol)) == [2]


def test_unreadable_inchi_and_unusable_smiles_are_notes(svc, capfd):
    r = find(svc, text="InChI=1S/garbage")
    assert r["total"] == 0
    assert r["notes"] == ["The text starts with 'InChI=' but could not be read as an InChI."]
    r = find(svc, text="*CCO")
    assert r["total"] == 0 and r["notes"][0].startswith("Read as SMILES, but it cannot be compared")
    captured = capfd.readouterr()
    assert captured.out == "" and captured.err == ""   # RDKit stays quiet


def test_name_reason(svc):
    r = find(svc, text="PHENYLETHAN")
    assert cores(r) == [1] and item(r, 1)["matched_by"] == ["name"]
    assert matched(r, 1) == {"000001_01": False, "000001_02": True}
    assert r["notes"] == []
    assert cores(find(svc, text="ethanol abs")) == [2]
    assert find(svc, text="butanol")["total"] == 0


def test_formula_reason(svc):
    r = find(svc, text="C8H11N")
    assert cores(r) == [1] and item(r, 1)["matched_by"] == ["formula"]   # not the salt
    assert matched(r, 1) == {"000001_01": False, "000001_02": False}
    assert "Read as a formula." in r["notes"]
    assert cores(find(svc, text="C8H11N.ClH")) == [3]    # the stored formula
    assert cores(find(svc, text="C8H12ClN")) == [3]      # the sum formula
    assert cores(find(svc, text="ClC8H12N")) == [3]      # any element order
    assert cores(find(svc, text="C3H8O")) == [4]
    assert find(svc, text="C3H8O+")["total"] == 0       # the charge must match too
    assert find(svc, text="C8H13ClN")["total"] == 0


def test_experiment_reason(svc):
    r = find(svc, text="abc-003")
    assert cores(r) == [2, 3] and all(i["matched_by"] == ["experiment"] for i in r["items"])
    assert matched(r, 2) == {"000002_01": True}
    r = find(svc, text="ABC-004")                       # the experiment of a retired batch
    assert cores(r) == [1] and matched(r, 1) == {"000001_01": True, "000001_02": False}
    r = find(svc, text="105")                           # equal to the internal key
    assert cores(r) == [4] and item(r, 4)["matched_by"] == ["experiment"]
    assert find(svc, text="10")["total"] == 1          # "XYZ-010" contains it; no key equals it


def test_several_reasons_at_once(svc):
    r = find(svc, text="1")
    # CORE 1 by number; "1" also occurs in a name and in "ABC-001"; "XYZ-010" holds CORE 4
    assert cores(r) == [1, 4]
    assert item(r, 1)["matched_by"] == ["id", "name", "experiment"]
    assert item(r, 4)["matched_by"] == ["experiment"]
    assert matched(r, 1) == {"000001_01": True, "000001_02": True}


# --------------------------------------------------------------------------- drawn structure

def test_substructure(svc):
    r = find(svc, smiles="c1ccccc1")
    assert cores(r) == [1, 3] and all(i["matched_by"] == ["drawn"] for i in r["items"])
    assert matched(r, 1) == {"000001_01": True, "000001_02": True}
    assert r["notes"][0].startswith("Substructure search")
    assert cores(find(svc, smiles="CO")) == [2, 4]
    assert cores(find(svc, molfile=molfile("C1=CC=CC=C1"))) == [1, 3]   # Kekule drawing


def test_substructure_ignores_stereo_and_drawn_hydrogens(svc):
    assert cores(find(svc, smiles=AMINE_S)) == [1, 3]
    assert cores(find(svc, molfile=molfile("CO", explicit_h=True))) == [2, 4]
    assert cores(find(svc, molfile=molfile("CCO", explicit_h=True))) == [2, 4]


def test_exact(svc):
    r = find(svc, smiles=AMINE_S, match="exact")
    assert cores(r) == [1] and item(r, 1)["matched_by"] == ["drawn"]   # not the salt
    assert matched(r, 1) == {"000001_01": False, "000001_02": True}
    assert r["notes"][0].startswith("Exact search")
    r = find(svc, molfile=molfile(AMINE_FLAT), match="exact")
    assert cores(r) == [1] and matched(r, 1) == {"000001_01": False, "000001_02": False}
    assert find(svc, smiles="c1ccccc1", match="exact")["total"] == 0


@pytest.mark.parametrize("query, note", [
    ({"smiles": "C1CC("}, "The drawn structure could not be read, so nothing matches it."),
    ({"molfile": "this is not a molfile"},
     "The drawn structure could not be read, so nothing matches it."),
    ({"molfile": "\n  x\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n"},
     "The drawn structure is empty, so nothing matches it."),
])
@pytest.mark.parametrize("match", protocol.MATCH_MODES)
def test_unusable_query_matches_nothing(svc, query, note, match):
    r = find(svc, match=match, **query)
    assert r["total"] == 0 and r["items"] == [] and r["notes"] == [note]
    assert r["registry_total"] == 4


def test_exact_with_a_placeholder_is_a_note(svc):
    r = find(svc, smiles="*c1ccccc1", match="exact")
    assert r["total"] == 0 and r["notes"][0].startswith(
        "The drawn structure cannot be used for an exact search")


def test_blank_query_is_no_query(svc):
    assert find(svc, smiles="   ")["total"] == 4
    assert find(svc, molfile="", text=" ")["total"] == 4


# --------------------------------------------------------------------------- text AND structure

def test_text_and_structure_must_both_match(svc):
    r = find(svc, text="ABC-003", smiles="c1ccccc1")
    assert cores(r) == [3] and item(r, 3)["matched_by"] == ["experiment", "drawn"]
    assert len(r["notes"]) == 1 and r["notes"][0].startswith("Substructure")
    assert find(svc, text="propan", smiles="c1ccccc1")["total"] == 0


def test_variant_matched_combines_the_parts(svc):
    # name (variant 02) AND substructure (both variants): only 02
    r = find(svc, text="phenylethan", smiles="c1ccccc1")
    assert matched(r, 1) == {"000001_01": False, "000001_02": True}
    # formula (the CORE only) AND exact drawing of variant 02: 02
    r = find(svc, text="C8H11N", smiles=AMINE_S, match="exact")
    assert item(r, 1)["matched_by"] == ["formula", "drawn"]
    assert matched(r, 1) == {"000001_01": False, "000001_02": True}
    # text matching both variants themselves (a name, an experiment) AND an exact
    # flat drawing (the CORE only): both
    r = find(svc, text="1", smiles=AMINE_FLAT, match="exact")
    assert matched(r, 1) == {"000001_01": True, "000001_02": True}
    # the CORE only, twice: neither
    r = find(svc, text="C8H11N", smiles=AMINE_FLAT, match="exact")
    assert matched(r, 1) == {"000001_01": False, "000001_02": False}


# --------------------------------------------------------------------------- arguments

@pytest.mark.parametrize("kw", [
    {"match": "similar"}, {"order": "up"}, {"offset": -1}, {"offset": 1.5}, {"offset": True},
    {"limit": 0}, {"limit": "5"}, {"smiles": "CCO", "molfile": molfile("CCO")},
    {"text": 5}, {"text": "a\nb"}, {"text": "x" * (protocol.MAX_SEARCH_TEXT + 1)},
    {"smiles": 5}, {"molfile": ["M  END"]},
])
def test_bad_arguments(svc, kw):
    with pytest.raises(RegistryError) as info:
        svc.compounds(**kw)
    assert info.value.code == "invalid_argument"


def test_backend_without_listings_is_not_supported(home):
    bare = types.SimpleNamespace(close=lambda: None)
    svc = RegistryService(load_config(home, create=False), repo=bare)
    with pytest.raises(RegistryError) as info:
        svc.compounds()
    assert info.value.code == "not_supported"


# --------------------------------------------------------------------------- CLI

def test_cli_compounds(home, capsys, tmp_path):
    base = ["--home", str(home), "compounds"]
    assert main(base) == 0
    out = capsys.readouterr().out
    out.encode("ascii")
    assert out.startswith("4 compound(s)")
    assert "000001  C8H11N  121.183 g/mol  3 batch(es) in 3 experiment(s)" in out
    assert "000001_02" in out and f"name: {NAME_S}" in out and "000004" in out

    assert main(base + ["--text", "ABC-003", "--smiles", "c1ccccc1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("1 of 4 compound(s) match")
    assert "[matched: experiment, drawn]" in out and "*000003_01" in out
    assert "Substructure search" in out

    mol = tmp_path / "q.mol"
    mol.write_text(molfile(AMINE_S), encoding="utf-8")
    assert main(base + ["--molfile", str(mol), "--match", "exact", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [i["core_no"] for i in doc["items"]] == [1] and doc["items"][0]["svg"] is None

    assert main(base + ["--smiles", "CCO", "--molfile", str(mol)]) == 2
    assert main(base + ["--match", "similar"]) == 2


# --------------------------------------------------------------------------- host

def ask(home, params, op="compounds"):
    body = json.dumps({"id": "c1", "v": 1, "op": op, "params": params}).encode("utf-8")
    return host.handle_message(body, HomeChoice(home, "host-config")).response


def test_host_compounds(home):
    doc = ask(home, {})
    assert doc["ok"] is True and doc["id"] == "c1"
    r = doc["result"]
    assert r["total"] == 4 and r["limit"] == protocol.DEFAULT_PAGE_SIZE
    assert all(i["svg"].startswith("<svg") for i in r["items"])
    r = ask(home, {"text": " abc-003 ", "smiles": "c1ccccc1", "order": "desc", "limit": 1})["result"]
    assert [i["core_no"] for i in r["items"]] == [3] and r["total"] == 1
    r = ask(home, {"molfile": molfile(AMINE_S), "match": "exact"})["result"]
    assert [i["core_no"] for i in r["items"]] == [1]
    bad = ask(home, {"smiles": "C1CC("})
    assert bad["ok"] is True and bad["result"]["notes"] and bad["result"]["total"] == 0


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": protocol.MAX_PAGE_SIZE + 1},
                                    {"match": "fuzzy"}, {"text": "a\nb"},
                                    {"smiles": "C", "molfile": "M  END"}, {"with_svg": True}])
def test_host_compounds_bad_params(home, params):
    doc = ask(home, params)
    assert doc["ok"] is False and doc["error"]["code"] == "bad_request"


def test_host_never_creates_a_registry_for_compounds(tmp_path):
    doc = ask(tmp_path / "nowhere", {})
    assert doc["error"]["code"] == "registry_missing" and not (tmp_path / "nowhere").exists()


def _serve(home, op, params, max_bytes):
    body = json.dumps({"id": 1, "v": 1, "op": op, "params": params}).encode("utf-8")
    out = io.BytesIO()
    code = serve(io.BytesIO(struct.pack("=I", len(body)) + body), out,
                 HomeChoice(home, "host-config"), max_response_bytes=max_bytes)
    assert code == 0
    data = out.getvalue()
    return json.loads(data[4:].decode("utf-8"))


@pytest.mark.parametrize("op, params, key", [("compounds", {}, "items"),
                                             ("find", {"id": "1"}, "variants")])
def test_too_large_answers_lose_their_thumbnails_first(home, op, params, key):
    result = ask(home, params, op)["result"]
    size = len(encode_response(protocol.ok_response(1, json.loads(json.dumps(result))), op))
    trimmed = json.loads(json.dumps(result))
    for entry in trimmed[key]:
        entry["svg"] = None
    trimmed["warnings"].append(host.THUMBNAILS_DROPPED)
    small = len(encode_response(protocol.ok_response(1, trimmed), op))
    assert small < size

    doc = _serve(home, op, params, size - 1)
    assert doc["ok"] is True
    assert all(e["svg"] is None for e in doc["result"][key])
    assert doc["result"]["warnings"] == [host.THUMBNAILS_DROPPED]

    doc = _serve(home, op, params, small)
    assert doc["ok"] is True

    doc = _serve(home, op, params, small - 1)
    assert doc["ok"] is False and doc["error"]["code"] == "response_too_large"


def test_log_has_no_search_text_or_names(home, caplog):
    with caplog.at_level(logging.INFO, logger=host.LOGGER_NAME):
        _serve(home, "compounds", {"text": "secret-search", "smiles": "c1ccccc1"}, 900_000)
        _serve(home, "set_name", {"core_stereo_id": "000004_01", "name": "secret-name"}, 900_000)
    text = caplog.text
    assert "op=compounds" in text and "op=set_name" in text
    for secret in ("secret-search", "c1ccccc1", "secret-name", "000004_01"):
        assert secret not in text
