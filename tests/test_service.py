"""Tests for compound_registry.service (real chemistry, real SQLite in tmp_path)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
import rdkit
from rdkit import Chem
from rdkit.Chem import rdDepictor

from compound_registry.backup import list_backups
from compound_registry.config import load_config
from compound_registry.errors import (
    ChemistryError,
    NotFoundError,
    RegistryError,
    SlotConflictError,
)
from compound_registry.models import ExperimentRef, Normalized
from compound_registry.service import RegistryService

AMINE_R = "C[C@H](N)c1ccccc1"      # 1-phenylethylamine, one enantiomer
AMINE_S = "C[C@@H](N)c1ccccc1"     # the other enantiomer
AMINE_FLAT = "CC(N)c1ccccc1"       # stereocentre not defined
AMINE_RAC = "C[C@H](N)c1ccccc1 |&1:1|"
ETHANOL = "CCO"
ACETIC_ACID = "CC(=O)O"


def molfile(smiles: str, v3000: bool = False) -> str:
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    return Chem.MolToV3KMolBlock(mol) if v3000 else Chem.MolToMolBlock(mol)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def svc(home):
    s = RegistryService.open(home)
    yield s
    s.close()


def write_config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")


def file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def event_count(db: Path) -> int:
    with closing(sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        return c.execute("SELECT count(*) FROM events").fetchone()[0]


# --------------------------------------------------------------------------- numbering

def test_first_registration(svc):
    r = svc.register(smiles=ETHANOL, experiment_ref="4180", experiment_code="ABC-008",
                     experiment_url="https://eln.example/4180")
    assert r["id"] == "000001_01_001"
    assert r["created"] and r["core_created"] and r["stereo_created"]
    b = r["batch"]
    assert b["full_id"] == "000001_01_001"
    assert (b["core_no"], b["stereo_no"], b["batch_no"]) == (1, 1, 1)
    assert b["core_stereo_id"] == "000001_01"
    assert b["experiment_ref"] == "4180" and b["experiment_code"] == "ABC-008"
    assert b["experiment_url"] == "https://eln.example/4180"
    assert b["product_index"] == 1 and b["source"] == "cli"
    assert b["retired"] is False and b["retired_at"] is None
    assert "molfile" not in b
    s = r["structure"]
    assert "svg" not in s
    assert s["inchikey"] == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert s["stereo_mode"] == "none" and s["formula"] == "C2H6O"
    assert set(r) == {"id", "created", "core_created", "stereo_created", "batch", "structure",
                      "accepted_ignored_stereo", "warnings", "backup", "folder"}
    assert r["accepted_ignored_stereo"] == [] and s["ignored_stereo"] == []


def test_second_experiment_same_compound_gets_next_batch(svc):
    first = svc.register(smiles=AMINE_R, experiment_ref="E1")
    second = svc.register(smiles=AMINE_R, experiment_ref="E2")
    assert first["id"] == "000001_01_001"
    assert second["id"] == "000001_01_002"
    assert second["created"] and not second["core_created"] and not second["stereo_created"]
    assert second["batch"]["core_stereo_id"] == first["batch"]["core_stereo_id"]


def test_same_experiment_twice_returns_same_id(svc):
    first = svc.register(smiles=AMINE_R, experiment_ref="E1")
    again = svc.register(smiles=AMINE_R, experiment_ref="  E1 ")  # stripped
    assert again["id"] == first["id"] == "000001_01_001"
    assert again["created"] is False
    assert again["backup"]["performed"] is False
    other_slot = svc.register(smiles=AMINE_R, experiment_ref="E1", product_index=3)
    assert other_slot["id"] == first["id"] and other_slot["created"] is False
    assert any("already registered in this experiment" in w for w in other_slot["warnings"])
    assert svc.repo.stats()["batches"] == 1


def test_reaction_with_two_products(svc):
    p1 = svc.register(smiles=AMINE_R, experiment_ref="E1", product_index=1)
    p2 = svc.register(smiles=ETHANOL, experiment_ref="E1", product_index=2)
    assert p1["id"] == "000001_01_001" and p2["id"] == "000002_01_001"
    a1 = svc.analyze(smiles=AMINE_R, experiment_ref="E1", product_index=1)
    a2 = svc.analyze(smiles=ETHANOL, experiment_ref="E1", product_index=2)
    assert (a1["status"], a1["id"]) == ("registered_here", p1["id"])
    assert (a2["status"], a2["id"]) == ("registered_here", p2["id"])
    assert [b["full_id"] for b in svc.experiment("E1")] == [p1["id"], p2["id"]]


def test_enantiomers_share_core_with_two_stereo_numbers(svc):
    r = svc.register(smiles=AMINE_R, experiment_ref="E1")
    s = svc.register(smiles=AMINE_S, experiment_ref="E2")
    assert r["id"] == "000001_01_001"
    assert s["id"] == "000001_02_001"
    assert s["stereo_created"] and not s["core_created"]


def test_racemic_single_enantiomer_and_undefined_are_three_stereos(svc):
    drawn = molfile(AMINE_R)
    single = svc.register(molfile=drawn, experiment_ref="E1")
    rac = svc.register(molfile=drawn, mode="rac", experiment_ref="E2")
    undefined = svc.register(molfile=molfile(AMINE_FLAT), experiment_ref="E3")
    assert [single["id"], rac["id"], undefined["id"]] == \
        ["000001_01_001", "000001_02_001", "000001_03_001"]
    assert [x["batch"]["stereo_mode"] for x in (single, rac, undefined)] == \
        ["abs", "rac", "undefined"]
    assert rac["structure"]["detected_mode"] == "abs" and rac["structure"]["mode_overridden"]
    # The same racemate from CXSMILES enhanced stereo, and drawn as the other enantiomer.
    cx = svc.register(smiles=AMINE_RAC, experiment_ref="E4")
    mirror = svc.register(molfile=molfile(AMINE_S), mode="rac", experiment_ref="E5")
    assert cx["id"] == "000001_02_002"
    assert mirror["id"] == "000001_02_003"


def test_free_base_and_salts_are_three_cores(svc):
    base = svc.register(smiles=AMINE_R, experiment_ref="E1")
    hcl = svc.register(smiles=AMINE_R + ".Cl", experiment_ref="E2")
    two = svc.register(smiles=AMINE_R + ".Cl.Cl", experiment_ref="E3")
    assert [base["id"], hcl["id"], two["id"]] == \
        ["000001_01_001", "000002_01_001", "000003_01_001"]
    assert all(x["core_created"] for x in (base, hcl, two))


def test_molfile_and_smiles_routes_register_the_same_compound(svc):
    via_molfile = svc.register(molfile=molfile(AMINE_R).replace("\n", "\r\n"),
                               experiment_ref="E1")
    via_smiles = svc.register(smiles=AMINE_R, experiment_ref="E2")
    via_v3000 = svc.register(molfile=molfile(AMINE_R, v3000=True), experiment_ref="E3")
    assert via_molfile["id"] == "000001_01_001"
    assert via_smiles["id"] == "000001_01_002"
    assert via_v3000["id"] == "000001_01_003"
    assert via_smiles["structure"]["inchikey"] == via_molfile["structure"]["inchikey"]


# --------------------------------------------------------------------------- analyze

def test_every_analyze_status(svc):
    new = svc.analyze(smiles=AMINE_R, experiment_ref="E1")
    assert new["status"] == "new"
    assert new["id"] is None and new["core_stereo_id"] is None and new["core_no"] is None
    assert new["batches"] == [] and new["other_stereos"] == [] and new["slot_conflict"] is None
    assert new["mode_options"] == ["abs", "rac", "rel", "undefined"]
    assert new["structure"]["svg"].startswith("<svg")
    assert svc.repo.stats()["batches"] == 0  # analyze never writes

    svc.register(smiles=AMINE_R, experiment_ref="E1")
    here = svc.analyze(smiles=AMINE_R, experiment_ref="E1")
    assert (here["status"], here["id"], here["core_stereo_id"], here["core_no"]) == \
        ("registered_here", "000001_01_001", "000001_01", 1)

    elsewhere = svc.analyze(smiles=AMINE_R, experiment_ref="E2")
    assert (elsewhere["status"], elsewhere["id"]) == ("known_compound", None)
    assert elsewhere["core_stereo_id"] == "000001_01"

    no_experiment = svc.analyze(smiles=AMINE_R)
    assert (no_experiment["status"], no_experiment["id"]) == ("known_compound", None)

    other_stereo = svc.analyze(smiles=AMINE_S, experiment_ref="E1")
    assert other_stereo["status"] == "known_core"
    assert other_stereo["core_no"] == 1 and other_stereo["core_stereo_id"] is None
    assert other_stereo["id"] is None

    assert svc.analyze(smiles=ETHANOL)["status"] == "new"
    assert set(new) == {"structure", "status", "id", "core_stereo_id", "core_no", "name",
                        "batches", "other_stereos", "slot_conflict", "mode_options", "warnings"}
    assert set(new["structure"]) == {"inchikey", "key_digest", "core_inchikey", "formula", "smiles", "inchi",
                                     "stereo_mode", "detected_mode", "mode_overridden",
                                     "warnings", "ignored_stereo", "svg", "properties"}
    assert new["name"] is None


def test_analyze_batches_and_other_stereos(svc):
    svc.register(smiles=AMINE_R, experiment_ref="E1", experiment_code="ABC-001")
    svc.register(smiles=AMINE_R, experiment_ref="E2")
    svc.register(smiles=AMINE_S, experiment_ref="E3")
    svc.register(smiles=AMINE_FLAT, experiment_ref="E4")
    svc.retire("000001_01_001", "mislabelled")

    a = svc.analyze(smiles=AMINE_R)
    assert [b["full_id"] for b in a["batches"]] == ["000001_01_001", "000001_01_002"]
    assert [b["retired"] for b in a["batches"]] == [True, False]
    assert a["batches"][0]["retired_reason"] == "mislabelled"
    assert a["batches"][0]["experiment_code"] == "ABC-001"
    assert all("molfile" not in b for b in a["batches"])

    others = a["other_stereos"]
    assert [o["core_stereo_id"] for o in others] == ["000001_02", "000001_03"]
    assert [o["stereo_no"] for o in others] == [2, 3]
    assert [o["stereo_mode"] for o in others] == ["abs", "undefined"]
    assert [o["batch_count"] for o in others] == [1, 1]
    assert all(o["svg"].startswith("<svg") for o in others)
    assert others[0]["inchikey"] == svc.analyze(smiles=AMINE_S)["structure"]["inchikey"]
    assert set(others[0]) == {"core_stereo_id", "stereo_no", "stereo_mode", "smiles",
                              "inchikey", "batch_count", "svg"}

    lean = svc.analyze(smiles=AMINE_R, with_svg=False, with_molfile=True)
    assert lean["structure"]["svg"] is None
    assert all(o["svg"] is None for o in lean["other_stereos"])
    assert all(b["molfile"].strip() for b in lean["batches"])


def test_analyze_does_not_write(svc):
    svc.register(smiles=AMINE_R, experiment_ref="E1")
    before = (file_digest(svc.config.db_path), event_count(svc.config.db_path))
    svc.analyze(smiles=AMINE_R, experiment_ref="E1")
    svc.analyze(smiles=AMINE_S, experiment_ref="E9", product_index=1)
    assert (file_digest(svc.config.db_path), event_count(svc.config.db_path)) == before


# --------------------------------------------------------------------------- slot conflict / retire

def test_slot_conflict_retire_and_reregister(svc):
    wrong = svc.register(smiles=AMINE_R, experiment_ref="E1", product_index=1)
    assert wrong["id"] == "000001_01_001"

    a = svc.analyze(smiles=ETHANOL, experiment_ref="E1", product_index=1)
    assert a["status"] == "new" and a["slot_conflict"]["full_id"] == "000001_01_001"
    with pytest.raises(SlotConflictError) as exc:
        svc.register(smiles=ETHANOL, experiment_ref="E1", product_index=1)
    assert exc.value.code == "slot_conflict"
    assert exc.value.existing.full_id == "000001_01_001"
    assert svc.repo.stats()["cores"] == 1  # nothing allocated

    retired = svc.retire("000001_01_001", "wrong structure drawn")
    assert retired["id"] == "000001_01_001"
    assert retired["batch"]["retired"] is True
    assert retired["batch"]["retired_reason"] == "wrong structure drawn"
    assert set(retired["backup"]) >= {"performed", "path", "warning"}

    right = svc.register(smiles=ETHANOL, experiment_ref="E1", product_index=1)
    assert right["id"] == "000002_01_001" and right["created"]

    again = svc.register(smiles=AMINE_R, experiment_ref="E1", product_index=2)
    assert again["id"] == "000001_01_002" and again["created"]  # new number, old one kept
    old = svc.get("000001_01_001")
    assert old["retired"] is True and old["retired_reason"] == "wrong structure drawn"
    assert [b["full_id"] for b in svc.experiment("E1")] == \
        ["000001_01_001", "000002_01_001", "000001_01_002"]

    with pytest.raises(RegistryError) as twice:
        svc.retire("000001_01_001", "again")
    assert twice.value.code == "already_retired"
    with pytest.raises(NotFoundError):
        svc.retire("000099_01_001", "nope")
    with pytest.raises(RegistryError) as blank:
        svc.retire("000001_01_002", "   ")
    assert blank.value.code == "invalid_argument"


def test_get_and_experiment(svc):
    svc.register(smiles=AMINE_R, experiment_ref="E1")
    got = svc.get(" 000001_01_001 ")
    assert got["full_id"] == "000001_01_001" and "molfile" not in got
    assert svc.get("000001_01_001", with_molfile=True)["molfile"].rstrip().endswith("M  END")
    with pytest.raises(NotFoundError) as exc:
        svc.get("000002_01_001")
    assert exc.value.code == "not_found"
    with pytest.raises(NotFoundError):
        svc.get("not-an-id")
    assert svc.experiment("unknown") == []


# --------------------------------------------------------------------------- input validation

@pytest.mark.parametrize("kwargs", [
    {},
    {"molfile": molfile(ETHANOL), "smiles": ETHANOL},
])
def test_exactly_one_structure(svc, kwargs):
    for call in (lambda: svc.analyze(**kwargs),
                 lambda: svc.register(**kwargs, experiment_ref="E1")):
        with pytest.raises(RegistryError) as exc:
            call()
        assert exc.value.code == "invalid_argument"


@pytest.mark.parametrize("ref", ["", "   ", None, 5])
def test_blank_experiment_ref_is_rejected(svc, ref):
    with pytest.raises(RegistryError) as exc:
        svc.register(smiles=ETHANOL, experiment_ref=ref)
    assert exc.value.code == "invalid_argument"
    if ref is not None:
        with pytest.raises(RegistryError):
            svc.analyze(smiles=ETHANOL, experiment_ref=ref)


@pytest.mark.parametrize("index", [0, -1, True, "1", 1.0])
def test_bad_product_index(svc, index):
    with pytest.raises(RegistryError) as exc:
        svc.register(smiles=ETHANOL, experiment_ref="E1", product_index=index)
    assert exc.value.code == "invalid_argument"


def test_bad_mode_and_bad_structure(svc):
    with pytest.raises(ChemistryError) as exc:
        svc.analyze(smiles=AMINE_R, mode="racemic")
    assert exc.value.code == "invalid_mode"
    with pytest.raises(ChemistryError) as exc:
        svc.register(smiles="C1CC(", experiment_ref="E1")
    assert exc.value.code == "structure_parse_failed"
    with pytest.raises(ChemistryError):
        svc.analyze(molfile="")
    assert svc.repo.stats()["batches"] == 0


def test_bad_export_format(svc, tmp_path):
    with pytest.raises(RegistryError) as exc:
        svc.export("xlsx", tmp_path / "x.xlsx")
    assert exc.value.code == "invalid_argument"


# --------------------------------------------------------------------------- JSON

def test_every_result_is_json_serialisable(svc, tmp_path):
    results = [
        svc.analyze(smiles=AMINE_R, experiment_ref="E1"),
        svc.register(smiles=AMINE_R, experiment_ref="E1"),
        svc.register(smiles=AMINE_S, experiment_ref="E2"),
        svc.analyze(smiles=AMINE_R, experiment_ref="E1", product_index=1, with_molfile=True),
        svc.get("000001_01_001"),
        svc.experiment("E1"),
        svc.retire("000001_02_001", "test"),
        svc.export("csv", tmp_path / "x.csv"),
        svc.export("sdf", tmp_path / "x.sdf", include_retired=False),
        svc.backup(),
        svc.list_backups(),
        svc.rekey_check(),
        svc.doctor(),
    ]
    results.append(svc.verify_backup(results[9]["path"]))
    for r in results:
        assert json.loads(json.dumps(r)) == r


# --------------------------------------------------------------------------- default stereo mode

def test_default_stereo_mode_from_config(home):
    write_config(home, '[chemistry]\ndefault_stereo_mode = "rac"\n')
    with RegistryService.open(home) as svc:
        assert svc.analyze(smiles=AMINE_R)["structure"]["stereo_mode"] == "rac"

        plain = svc.register(smiles=AMINE_R, experiment_ref="E1")
        s = plain["structure"]
        assert (s["stereo_mode"], s["detected_mode"], s["mode_overridden"]) == ("rac", "abs", True)
        assert plain["batch"]["stereo_mode"] == "rac"

        # Undefined and achiral drawings keep their detected mode.
        flat = svc.register(smiles=AMINE_FLAT, experiment_ref="E2")
        assert flat["structure"]["stereo_mode"] == "undefined"
        assert not flat["structure"]["mode_overridden"]
        achiral = svc.register(smiles=ETHANOL, experiment_ref="E3")
        assert achiral["structure"]["stereo_mode"] == "none"
        assert not achiral["structure"]["mode_overridden"]

        # A drawing with enhanced stereo is read as drawn (OR group -> rel).
        rel = svc.analyze(smiles="C[C@H](N)c1ccccc1 |o1:1|")
        assert rel["structure"]["stereo_mode"] == "rel"
        assert not rel["structure"]["mode_overridden"]

        # Partially defined drawings get the default too.
        partial = svc.analyze(smiles="C[C@H](N)CC(C)O")
        assert partial["structure"]["detected_mode"] == "partial"
        assert partial["structure"]["mode_overridden"]

        # An explicit mode always wins.
        explicit = svc.register(smiles=AMINE_R, mode="abs", experiment_ref="E4")
        assert explicit["structure"]["stereo_mode"] == "abs"
        assert explicit["id"] == "000001_03_001"

        # Racemate drawn as R and registered with the default = same variant.
        assert svc.register(smiles=AMINE_RAC, experiment_ref="E5")["id"] == "000001_01_002"


def test_default_abs_config_does_not_override(svc):
    r = svc.register(smiles=AMINE_R, experiment_ref="E1")
    assert r["structure"]["stereo_mode"] == "abs"
    assert r["structure"]["mode_overridden"] is False


# --------------------------------------------------------------------------- backup

def test_backup_on_first_write_but_not_second(svc):
    assert list_backups(svc.config) == []
    first = svc.register(smiles=AMINE_R, experiment_ref="E1")
    bk = first["backup"]
    assert bk["performed"] is True and bk["warning"] is None
    assert Path(bk["path"]).is_file()
    assert [str(p) for p in list_backups(svc.config)] == [bk["path"]]

    second = svc.register(smiles=ETHANOL, experiment_ref="E2")
    assert second["backup"] == {"performed": False, "path": None, "warning": None, "removed": []}
    retire = svc.retire(second["id"], "test")
    assert retire["backup"]["performed"] is False
    assert len(list_backups(svc.config)) == 1

    forced = svc.backup(force=True)
    assert forced["performed"] is True and Path(forced["path"]).is_file()
    assert svc.backup(force=False)["performed"] is False


def test_broken_backup_folder_gives_warning_and_still_returns_id(home, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the backup folder should be", encoding="utf-8")
    config = replace(load_config(home), backup_dir=blocker / "backups")
    with RegistryService(config) as svc:
        r = svc.register(smiles=AMINE_R, experiment_ref="E1")
        assert r["id"] == "000001_01_001" and r["created"]
        assert r["backup"]["performed"] is False
        assert r["backup"]["warning"].startswith("Backup failed")
        assert r["backup"]["warning"] in r["warnings"]
        assert svc.get("000001_01_001")["full_id"] == "000001_01_001"
        forced = svc.backup()
        assert forced["performed"] is False and forced["warning"]


# --------------------------------------------------------------------------- rekey check

def populate(svc: RegistryService) -> int:
    svc.register(molfile=molfile(AMINE_R), experiment_ref="E1")
    svc.register(smiles=AMINE_R, experiment_ref="E2")
    svc.register(smiles=AMINE_S, experiment_ref="E3")
    svc.register(molfile=molfile(AMINE_R), mode="rac", experiment_ref="E4")
    svc.register(molfile=molfile(AMINE_R), mode="rel", experiment_ref="E5")
    svc.register(smiles=AMINE_RAC, experiment_ref="E6")
    svc.register(molfile=molfile(AMINE_FLAT), experiment_ref="E7")
    svc.register(molfile=molfile(AMINE_R), mode="undefined", experiment_ref="E8")
    svc.register(smiles=AMINE_R + ".Cl", experiment_ref="E9")
    svc.register(smiles=ETHANOL, experiment_ref="E9", product_index=2)
    svc.register(molfile=molfile("C[C@H](N)C[C@H](C)O |o1:1,4|", v3000=True), experiment_ref="E10")
    svc.register(smiles=ACETIC_ACID, experiment_ref="E11")
    svc.retire("000001_01_001", "test retirement")
    return svc.repo.stats()["batches"]


def test_rekey_check_passes_and_writes_nothing(svc):
    n = populate(svc)
    db = svc.config.db_path
    before = (file_digest(db), event_count(db), db.stat().st_size)
    result = svc.rekey_check()
    assert result["checked"] == n == 12
    assert result["ok"] == n
    assert result["mismatches"] == []
    assert result["rdkit_version"] == rdkit.__version__
    assert result["inchi_version"]
    assert (file_digest(db), event_count(db), db.stat().st_size) == before


def test_rekey_check_with_default_mode_config(home):
    write_config(home, '[chemistry]\ndefault_stereo_mode = "rac"\n')
    with RegistryService.open(home) as svc:
        populate(svc)
        result = svc.rekey_check()
        assert result["mismatches"] == [] and result["ok"] == result["checked"]


def test_rekey_check_reports_a_mismatch(svc):
    svc.register(smiles=AMINE_R, experiment_ref="E1")
    # A batch whose stored keys do not match its molfile (as if chemistry changed).
    fake = Normalized(core_key="InChI=1S/C2H6O/fake", stereo_key="[]", stereo_mode="none",
                      inchikey="FAKE", smiles="CCO", molblock=molfile(ETHANOL))
    bad = svc.repo.register(fake, ExperimentRef("E2"), 1).batch.full_id
    result = svc.rekey_check()
    assert result["checked"] == 2 and result["ok"] == 1
    assert len(result["mismatches"]) == 1
    m = result["mismatches"][0]
    assert m["id"] == bad and m["stored"] == "000002_01" and m["recomputed"] is None
    assert "core key" in m["reason"]


def test_rekey_check_reports_unparsable_molfile(svc):
    fake = Normalized(core_key="InChI=1S/X", stereo_key="[]", stereo_mode="none",
                      inchikey="X", smiles="X", molblock="not a molfile")
    svc.repo.register(fake, ExperimentRef("E1"), 1)
    m = svc.rekey_check()["mismatches"]
    assert len(m) == 1 and m[0]["reason"].startswith("structure_parse_failed")


# --------------------------------------------------------------------------- doctor / warnings

def test_doctor(svc):
    svc.register(smiles=AMINE_R, experiment_ref="E1")
    svc.register(smiles=ETHANOL, experiment_ref="E2")
    svc.retire("000002_01_001", "test")
    d = svc.doctor()
    assert {"home", "config_path", "db_path", "db_exists", "id_format", "stats", "integrity",
            "backup_dir", "last_backup_at", "backups", "rdkit_version", "inchi_version",
            "python", "warnings"} <= set(d)
    assert d["home"] == str(svc.config.home)
    assert d["db_path"] == str(svc.config.db_path) and d["db_exists"] is True
    assert d["id_format"] == "{core:06d}_{stereo:02d}_{batch:03d}"
    assert d["stats"] == {"cores": 2, "stereos": 2, "batches": 2, "retired": 1}
    assert d["integrity"] == "ok"
    assert d["backups"] == 1 and d["last_backup_at"]
    assert d["rdkit_version"] == rdkit.__version__
    assert d["warnings"] == []


def test_doctor_on_fresh_registry_warns_about_missing_backup(svc):
    d = svc.doctor()
    assert d["stats"]["batches"] == 0 and d["backups"] == 0 and d["last_backup_at"] is None
    assert d["db_created_now"] is True
    assert any("No backup" in w for w in d["warnings"])


def test_sync_folder_warning_is_reported(monkeypatch, tmp_path):
    synced = tmp_path / "OneDrive - Lab"
    monkeypatch.setenv("OneDrive", str(synced))
    with RegistryService.open(synced / "CompoundRegistry") as svc:
        for warnings in (svc.analyze(smiles=ETHANOL)["warnings"],
                         svc.register(smiles=ETHANOL, experiment_ref="E1")["warnings"],
                         svc.doctor()["warnings"]):
            assert any("OneDrive" in w for w in warnings)


def test_context_manager_closes(home):
    with RegistryService.open(home) as svc:
        svc.register(smiles=ETHANOL, experiment_ref="E1")
    with pytest.raises(RegistryError):
        svc.get("000001_01_001")
    svc.close()  # second close is harmless


def test_sync_folder_warning_is_exactly_the_config_warning(monkeypatch, tmp_path):
    from compound_registry.config import sync_folder_warning

    synced = tmp_path / "Dropbox"
    with RegistryService.open(synced / "reg") as svc:
        expected = sync_folder_warning(svc.config.db_path)
        assert expected is not None
        assert expected in svc.analyze(smiles=ETHANOL)["warnings"]
        assert expected in svc.register(smiles=ETHANOL, experiment_ref="E1")["warnings"]
        assert expected in svc.doctor()["warnings"]
        assert expected in svc.retire("000001_01_001", "test")["warnings"]


def test_experiment_ref_whitespace_is_one_experiment(svc):
    first = svc.register(smiles=ETHANOL, experiment_ref="4180")
    again = svc.register(smiles=ETHANOL, experiment_ref="4180 ")
    assert again["id"] == first["id"] and again["created"] is False
    assert svc.repo.stats()["batches"] == 1


# --------------------------------------------------------------------------- issued-ID journal

def journal_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def journal_files(svc: RegistryService) -> tuple[Path, Path]:
    return svc.config.home / "issued-ids.jsonl", svc.config.backup_dir / "issued-ids.jsonl"


def db_dump(db: Path) -> list[str]:
    with closing(sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        return list(c.iterdump())


def test_journal_written_and_mirrored(svc):
    main, mirror = journal_files(svc)
    r = svc.register(smiles=AMINE_R, experiment_ref=" E1 ", experiment_code="ABC-1",
                     experiment_url="https://eln/1", product_index=2)
    (entry,) = journal_lines(main)
    assert journal_lines(mirror) == [entry]
    b = svc.get(r["id"], with_molfile=True)
    assert entry["event"] == "register" and entry["id"] == r["id"] == "000001_01_001"
    assert entry["at"] == b["created_at"]
    assert (entry["experiment_ref"], entry["experiment_code"], entry["experiment_url"],
            entry["product_index"], entry["source"]) == ("E1", "ABC-1", "https://eln/1", 2, "cli")
    assert entry["molblock"] == b["molfile"] and entry["smiles"] == b["smiles"]
    assert entry["inchikey"] == b["inchikey"] and entry["stereo_mode"] == "abs"
    found = svc.repo.lookup(Normalized(core_key=entry["core_key"], stereo_key=entry["stereo_key"],
                                       stereo_mode=entry["stereo_mode"], inchikey="", smiles=""))
    assert found.stereo is not None and found.stereo.core_stereo_id == "000001_01"
    for name in ("core_inchikey", "inchi", "formula", "detected_mode", "mode_overridden",
                 "input_format", "rdkit_version", "inchi_version"):
        assert name in entry
    assert entry["accepted_ignored_stereo"] == []

    # Only real changes: an idempotent repeat and a refused registration add nothing.
    svc.register(smiles=AMINE_R, experiment_ref="E1")
    with pytest.raises(SlotConflictError):
        svc.register(smiles=ETHANOL, experiment_ref="E1", product_index=2)
    assert len(journal_lines(main)) == 1
    svc.retire("000001_01_001", "  wrong drawing ")
    retired = svc.get("000001_01_001")
    last = journal_lines(main)[-1]
    assert last == {"event": "retire", "id": "000001_01_001", "at": retired["retired_at"],
                    "reason": "wrong drawing", "registry_id": svc.repo.registry_id}
    assert entry["registry_id"] == svc.repo.registry_id
    assert journal_lines(mirror) == journal_lines(main)


def test_mirror_failure_is_only_a_warning(home, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the backup folder should be", encoding="utf-8")
    config = replace(load_config(home), backup_dir=blocker / "backups")
    with RegistryService(config) as svc:
        r = svc.register(smiles=ETHANOL, experiment_ref="E1")
        assert r["created"] and r["id"] == "000001_01_001"
        assert any("journal mirror" in w for w in r["warnings"])
        assert [e["id"] for e in journal_lines(home / "issued-ids.jsonl")] == ["000001_01_001"]
        assert svc.register(smiles=ACETIC_ACID, experiment_ref="E2")["id"] == "000002_01_001"


def test_truncated_journal_line_does_not_block(svc):
    main, mirror = journal_files(svc)
    svc.register(smiles=ETHANOL, experiment_ref="E1")
    with open(main, "ab") as fh:
        fh.write(b'{"event": "register", "id": "0000')  # a crash in the middle of a write
    svc.close()
    with RegistryService.open(svc.config.home) as again:
        assert any("unreadable" in w for w in again.analyze(smiles=ETHANOL)["warnings"])
        assert again.doctor()["journal"]["unreadable_lines"] == 1
        assert again.register(smiles=ACETIC_ACID, experiment_ref="E2")["id"] == "000002_01_001"
    assert [e["id"] for e in journal_lines_tolerant(main)] == ["000001_01_001", "000002_01_001"]


def journal_lines_tolerant(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def build_history(svc: RegistryService) -> Path:
    """Register a few compounds; return the backup written after the FIRST one."""
    first = svc.register(smiles=AMINE_R, experiment_ref="E1")             # 000001_01_001
    assert first["backup"]["performed"]
    svc.register(smiles=AMINE_S, experiment_ref="E2", mode="abs")          # 000001_02_001
    svc.register(smiles=ETHANOL, experiment_ref="E3")                      # 000002_01_001
    svc.register(smiles=AMINE_R, experiment_ref="E4", experiment_code="X")  # 000001_01_002
    svc.retire("000001_01_001", "wrong structure")
    svc.register(smiles=ACETIC_ACID, experiment_ref="E1")                  # 000003_01_001 (freed slot)
    return Path(first["backup"]["path"])


def all_batches(svc: RegistryService) -> dict[str, dict]:
    return {b.full_id: svc.get(b.full_id, with_molfile=True) for b in svc.repo.iter_batches()}


LOST = ["000001_02_001", "000002_01_001", "000001_01_002", "000003_01_001"]


@pytest.fixture
def stale(home):
    """A registry whose live database was replaced by the backup taken after
    the first registration. Returns (home, batches before the restore)."""
    with RegistryService.open(home) as svc:
        old_backup = build_history(svc)
        before = all_batches(svc)
        db = svc.config.db_path
    assert sorted(before) == sorted(["000001_01_001"] + LOST)
    shutil.copy2(old_backup, db)
    return home, before


def test_stale_database_blocks_writes_but_not_reads(stale, tmp_path):
    home, before = stale
    with RegistryService.open(home) as svc:
        db = svc.config.db_path
        dump = db_dump(db)
        with pytest.raises(RegistryError) as exc:
            svc.register(smiles="c1ccccc1", experiment_ref="E9")
        e = exc.value
        assert e.code == "database_older_than_journal"
        assert "4 issued ID(s)" in e.message and "1 retirement(s)" in e.message
        for full_id in LOST:
            assert full_id in e.message
        assert "older backup" in e.message and "compound-registry recover" in e.message
        with pytest.raises(RegistryError) as exc:
            svc.retire("000001_01_001", "again")
        assert exc.value.code == "database_older_than_journal"
        with pytest.raises(RegistryError) as exc:
            svc.check_writable()
        assert exc.value.code == "database_older_than_journal"

        # Reads keep working.
        a = svc.analyze(smiles=AMINE_R, experiment_ref="E1")
        assert a["status"] == "registered_here"
        assert any("compound-registry recover" in w for w in a["warnings"])
        assert svc.get("000001_01_001")["retired"] is False  # the old state
        with pytest.raises(NotFoundError):
            svc.get("000002_01_001")
        assert svc.experiment("E1")[0]["full_id"] == "000001_01_001"
        assert svc.export("csv", tmp_path / "x.csv")["count"] == 1
        assert svc.rekey_check()["mismatches"] == []
        d = svc.doctor()
        assert d["journal"]["missing_in_database"] == LOST
        assert d["journal"]["retirements_missing_in_database"] == ["000001_01_001"]
        assert d["journal"]["path"] == str(home / "issued-ids.jsonl")
        assert d["journal"]["mirror_path"] == str(svc.config.backup_dir / "issued-ids.jsonl")
        assert d["journal"]["entries"] == 6
        assert any("compound-registry recover" in w for w in d["warnings"])
        assert db_dump(db) == dump  # nothing was written


def test_recover_reproduces_exact_ids_and_timestamps(stale):
    home, before = stale
    main = home / "issued-ids.jsonl"
    lines_before = main.read_bytes()
    with RegistryService.open(home) as svc:
        plan = svc.recover_plan()
        assert plan["to_register"] == LOST and plan["to_retire"] == ["000001_01_001"]
        result = svc.recover()
        assert result["recovered"] == LOST
        assert result["retired"] == ["000001_01_001"]
        assert result["already_present"] == 1
        assert all_batches(svc) == before  # same IDs, same rows, same timestamps
        assert svc.repo.integrity_check() == "ok"
        assert svc.doctor()["journal"]["missing_in_database"] == []
        # writes work again, in this instance ...
        new = svc.register(smiles="c1ccccc1", experiment_ref="E9")
        assert new["id"] == "000004_01_001"
    assert main.read_bytes().startswith(lines_before)
    assert len(journal_lines(main)) == 7  # recover itself appended nothing
    # ... and in a new one; a second recover has nothing to do.
    with RegistryService.open(home) as svc:
        nothing = svc.recover()
        assert (nothing["recovered"], nothing["retired"], nothing["already_present"]) == ([], [], 7)
        assert nothing["backup"]["performed"] is False
        assert svc.register(smiles="CCCC", experiment_ref="E10")["id"] == "000005_01_001"
        assert svc.retire("000005_01_001", "test")["id"] == "000005_01_001"


def test_recover_works_from_the_mirror_alone(stale):
    home, before = stale
    (home / "issued-ids.jsonl").unlink()  # the home journal is lost too
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as exc:
            svc.register(smiles="c1ccccc1", experiment_ref="E9")
        assert exc.value.code == "database_older_than_journal"
        assert svc.recover()["recovered"] == LOST
        assert all_batches(svc) == before
        svc.register(smiles="c1ccccc1", experiment_ref="E9")
    # the home journal was rebuilt by catch-up, in order
    assert [(e["event"], e["id"]) for e in journal_lines(home / "issued-ids.jsonl")] == [
        ("register", "000001_01_001"), ("register", "000001_02_001"),
        ("register", "000002_01_001"), ("register", "000001_01_002"),
        ("retire", "000001_01_001"), ("register", "000003_01_001"),
        ("register", "000004_01_001")]


def rewrite_journals(svc_home: Path, backup_dir: Path, change) -> None:
    for path in (svc_home / "issued-ids.jsonl", backup_dir / "issued-ids.jsonl"):
        entries = [change(e) for e in journal_lines(path)]
        path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def test_recover_refuses_when_an_id_cannot_be_reproduced(stale):
    home, _ = stale
    with RegistryService.open(home) as svc:
        backup_dir, db = svc.config.backup_dir, svc.config.db_path

    def renumber(entry):
        if entry["id"] == "000002_01_001":
            entry = dict(entry, id="000002_01_002")  # the journal says something impossible
        return entry

    rewrite_journals(home, backup_dir, renumber)
    dump = db_dump(db)
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as exc:
            svc.recover()
        assert exc.value.code == "recover_failed"
        assert "000002_01_002" in exc.value.message and "Nothing was changed" in exc.value.message
        assert db_dump(db) == dump  # 000001_02_001 (replayed first) was rolled back too
        with pytest.raises(RegistryError) as exc:
            svc.register(smiles="c1ccccc1", experiment_ref="E9")
        assert exc.value.code == "database_older_than_journal"
    assert db_dump(db) == dump


def test_recover_refuses_an_incomplete_entry(stale):
    home, _ = stale
    with RegistryService.open(home) as svc:
        backup_dir, db = svc.config.backup_dir, svc.config.db_path
    rewrite_journals(home, backup_dir,
                     lambda e: {k: v for k, v in e.items() if k != "core_key"}
                     if e["id"] == "000003_01_001" else e)
    dump = db_dump(db)
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as exc:
            svc.recover()
        assert exc.value.code == "recover_failed" and "000003_01_001" in exc.value.message
    assert db_dump(db) == dump


def test_reissued_id_is_a_conflict(stale):
    """After the restore, someone bypassed the guard and issued 000001_02_001 again."""
    home, _ = stale
    with RegistryService.open(home) as svc:
        fake = Normalized(core_key="InChI=1S/Xx", stereo_key="[]", stereo_mode="none",
                          inchikey="OTHER", smiles="X", molblock="M  END\n")
        svc.repo.register(fake, ExperimentRef("OTHER"), 1)      # takes 000002_01_001 again
        db = svc.config.db_path
    dump = db_dump(db)
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as exc:
            svc.register(smiles="c1ccccc1", experiment_ref="E9")
        assert exc.value.code == "journal_conflict" and "000002_01_001" in exc.value.message
        with pytest.raises(RegistryError) as exc:
            svc.recover()
        assert exc.value.code == "recover_failed"
        assert svc.doctor()["journal"]["conflicts"] == ["000002_01_001"]
    assert db_dump(db) == dump


def test_journal_catch_up(svc, monkeypatch):
    main, mirror = journal_files(svc)
    svc.register(smiles=ETHANOL, experiment_ref="E1")                  # journalled
    fake = Normalized(core_key="InChI=1S/Yy", stereo_key="[]", stereo_mode="none",
                      inchikey="YY", smiles="Y", molblock="M  END\n")
    svc.repo.register(fake, ExperimentRef("E2"), 1)                     # NOT journalled
    svc.repo.retire("000001_01_001", "behind the journal's back")       # NOT journalled
    home = svc.config.home
    svc.close()
    with RegistryService.open(home) as again:
        # harmless: no warning, nothing missing in the database
        assert again.doctor()["journal"]["missing_in_database"] == []
        assert not any("journal" in w for w in again.analyze(smiles=ETHANOL)["warnings"])
        assert len(journal_lines(main)) == 1  # reads do not write
        again.register(smiles=ACETIC_ACID, experiment_ref="E3")
    expected = [("register", "000001_01_001"), ("register", "000002_01_001"),
                ("retire", "000001_01_001"), ("register", "000003_01_001")]
    assert [(e["event"], e["id"]) for e in journal_lines(main)] == expected
    assert [(e["event"], e["id"]) for e in journal_lines(mirror)] == expected
    caught = journal_lines(main)[1]
    assert caught["core_key"] == "InChI=1S/Yy" and caught["at"]

    # A journal introduced later: everything is written at the next write.
    main.unlink()
    mirror.unlink()
    with RegistryService.open(home) as again:
        again.register(smiles="CCCC", experiment_ref="E4")
    assert [(e["event"], e["id"]) for e in journal_lines(main)] == \
        expected + [("register", "000004_01_001")]

    # The append of the main journal fails: the next write of the same
    # instance catches up.
    from compound_registry import journal as journal_mod

    real_append = journal_mod.append_entries
    calls = {"n": 0}

    def flaky(path, entries):
        if Path(path) == main and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("disk full")
        return real_append(path, entries)

    with RegistryService.open(home) as again:
        monkeypatch.setattr(journal_mod, "append_entries", flaky)
        r = again.register(smiles="CCCCC", experiment_ref="E5")
        assert r["created"] and any("issued-ID journal" in w for w in r["warnings"])
        assert journal_lines(main)[-1]["id"] == "000004_01_001"
        again.register(smiles="CCCCCC", experiment_ref="E6")
    assert [e["id"] for e in journal_lines(main)][-2:] == ["000005_01_001", "000006_01_001"]
    assert [e["id"] for e in journal_lines(mirror)][-2:] == ["000005_01_001", "000006_01_001"]


# --------------------------------------------------------------------------- chemistry fingerprint

OLD_FINGERPRINT = "rdkit=2020.03.1;inchi=1.05;keyalg=1"


def test_fingerprint_stored_on_first_registration(svc):
    from compound_registry import chem
    from compound_registry.service import FINGERPRINT_KEY, chemistry_fingerprint

    assert svc.repo.get_meta(FINGERPRINT_KEY) is None
    svc.analyze(smiles=ETHANOL)
    assert svc.repo.get_meta(FINGERPRINT_KEY) is None  # reads do not write
    svc.register(smiles=ETHANOL, experiment_ref="E1")
    fp = svc.repo.get_meta(FINGERPRINT_KEY)
    assert fp == chemistry_fingerprint()
    assert fp == f"rdkit={rdkit.__version__};inchi={Chem.GetInchiVersion()};keyalg={chem.KEY_ALGORITHM}"
    d = svc.doctor()["chemistry_fingerprint"]
    assert d == {"stored": fp, "current": fp, "changed": False}


def test_changed_fingerprint_blocks_register_until_accepted(svc):
    from compound_registry.service import FINGERPRINT_KEY, chemistry_fingerprint

    populate(svc)
    svc.repo.set_meta(FINGERPRINT_KEY, OLD_FINGERPRINT)
    home = svc.config.home
    svc.close()
    with RegistryService.open(home) as svc:
        a = svc.analyze(smiles=AMINE_R)
        assert any("rekey-check --accept" in w for w in a["warnings"])
        d = svc.doctor()
        assert d["chemistry_fingerprint"] == {"stored": OLD_FINGERPRINT,
                                              "current": chemistry_fingerprint(), "changed": True}
        assert any("rekey-check --accept" in w for w in d["warnings"])
        with pytest.raises(RegistryError) as exc:
            svc.register(smiles=AMINE_R, experiment_ref="E1")  # even an idempotent repeat
        assert exc.value.code == "chemistry_version_changed"
        assert OLD_FINGERPRINT in exc.value.message
        assert "compound-registry rekey-check --accept" in exc.value.message
        assert svc.get("000001_01_002")["full_id"] == "000001_01_002"
        assert svc.retire("000001_01_002", "retiring needs no chemistry")["id"]

        check = svc.rekey_check()
        assert check["accepted"] is False and check["chemistry_fingerprint"]["changed"] is True
        assert svc.repo.get_meta(FINGERPRINT_KEY) == OLD_FINGERPRINT
        accepted = svc.rekey_check(accept=True)
        assert accepted["accepted"] is True and accepted["mismatches"] == []
        assert accepted["chemistry_fingerprint"] == {"stored": chemistry_fingerprint(),
                                                     "current": chemistry_fingerprint(),
                                                     "changed": False}
        assert svc.register(smiles="c1ccccc1", experiment_ref="E20")["created"]
    with RegistryService.open(home) as svc:
        assert svc.register(smiles="CCCC", experiment_ref="E21")["created"]


def test_accept_refused_with_mismatches(svc):
    from compound_registry.service import FINGERPRINT_KEY

    svc.register(smiles=AMINE_R, experiment_ref="E1")
    fake = Normalized(core_key="InChI=1S/C2H6O/fake", stereo_key="[]", stereo_mode="none",
                      inchikey="FAKE", smiles="CCO", molblock=molfile(ETHANOL))
    svc.repo.register(fake, ExperimentRef("E2"), 1)
    svc.repo.set_meta(FINGERPRINT_KEY, OLD_FINGERPRINT)
    home = svc.config.home
    svc.close()
    with RegistryService.open(home) as svc:
        result = svc.rekey_check(accept=True)
        assert result["accepted"] is False and len(result["mismatches"]) == 1
        assert svc.repo.get_meta(FINGERPRINT_KEY) == OLD_FINGERPRINT
        with pytest.raises(RegistryError) as exc:
            svc.register(smiles=ETHANOL, experiment_ref="E3")
        assert exc.value.code == "chemistry_version_changed"


def test_registry_without_fingerprint_checks_batch_versions(svc):
    from compound_registry.service import FINGERPRINT_KEY

    old = Normalized(core_key="InChI=1S/Zz", stereo_key="[]", stereo_mode="none",
                     inchikey="ZZ", smiles="Z", molblock="M  END\n", rdkit_version="2020.03.1",
                     inchi_version="1.05")
    svc.repo.register(old, ExperimentRef("E1"), 1)  # as if registered by an old version
    home = svc.config.home
    svc.close()
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as exc:
            svc.register(smiles=ETHANOL, experiment_ref="E2")
        assert exc.value.code == "chemistry_version_changed" and "2020.03.1" in exc.value.message
        assert svc.repo.get_meta(FINGERPRINT_KEY) is None


# --------------------------------------------------------------------------- ignored stereo

IGNORED = ("wedge on atom 3 is not a stereocentre", "allene axis C4=C5=C6")


@pytest.fixture
def ignoring(svc, monkeypatch):
    """The service sees ``IGNORED`` in every normalised structure."""
    original = svc._normalize
    monkeypatch.setattr(svc, "_normalize",
                        lambda *a, **k: replace(original(*a, **k), ignored_stereo=IGNORED))
    return svc


def test_analyze_reports_ignored_stereo(ignoring):
    a = ignoring.analyze(smiles=AMINE_R)
    assert a["structure"]["ignored_stereo"] == list(IGNORED)


def test_stereo_not_in_key_refused_without_acceptance(ignoring):
    svc = ignoring
    with pytest.raises(RegistryError) as exc:
        svc.register(smiles=AMINE_R, experiment_ref="E1")
    e = exc.value
    assert e.code == "stereo_not_in_key"
    assert all(item in e.message for item in IGNORED)
    assert "same ID" in e.message
    assert svc.repo.stats()["batches"] == 0
    assert journal_lines(journal_files(svc)[0]) == []

    r = svc.register(smiles=AMINE_R, experiment_ref="E1", accept_ignored_stereo=True)
    assert r["created"] and r["accepted_ignored_stereo"] == list(IGNORED)
    assert r["structure"]["ignored_stereo"] == list(IGNORED)
    (entry,) = journal_lines(journal_files(svc)[0])
    assert entry["accepted_ignored_stereo"] == list(IGNORED)

    # An idempotent repeat just returns the existing ID, without acceptance.
    again = svc.register(smiles=AMINE_R, experiment_ref="E1")
    assert again["id"] == r["id"] and again["created"] is False
    assert again["accepted_ignored_stereo"] == []
    other_slot = svc.register(smiles=AMINE_R, experiment_ref="E1", product_index=2)
    assert other_slot["id"] == r["id"] and other_slot["created"] is False
    assert any("already registered in this experiment" in w for w in other_slot["warnings"])
    # A NEW batch (other experiment) needs the acceptance again.
    with pytest.raises(RegistryError) as exc:
        svc.register(smiles=AMINE_R, experiment_ref="E2")
    assert exc.value.code == "stereo_not_in_key"
    assert svc.repo.stats()["batches"] == 1
    assert len(journal_lines(journal_files(svc)[0])) == 1
