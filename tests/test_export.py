"""Tests for compound_registry.export (CSV and SDF)."""

from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

import pytest
from rdkit import Chem
from rdkit.Chem import rdDepictor

from compound_registry.export import (
    COLUMNS,
    EXPORT_COLUMNS,
    export_csv,
    export_sdf,
    sdf_molblock,
    sdf_value,
)
from compound_registry.models import BatchRecord
from compound_registry.service import RegistryService

EXPECTED_COLUMNS = (
    "full_id", "core_no", "stereo_no", "batch_no", "core_stereo_id", "stereo_mode",
    "inchikey", "smiles", "experiment_ref", "experiment_code", "experiment_url",
    "product_index", "source", "created_at", "retired_at", "retired_reason",
    "rdkit_version", "inchi_version",
)
# The CSV header / SDF fields: the batch fields, the stored name, then the yields.
EXPECTED_HEADER = EXPECTED_COLUMNS + ("name", "yield_isolated", "yield_analytical")


def molfile(smiles: str, v3000: bool = False) -> str:
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    return Chem.MolToV3KMolBlock(mol) if v3000 else Chem.MolToMolBlock(mol)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))


@pytest.fixture
def svc(tmp_path):
    s = RegistryService.open(tmp_path / "home")
    yield s
    s.close()


@pytest.fixture
def populated(svc):
    """Five batches (the HCl salt 000002_01_001 retired), molfile and SMILES routes,
    a CRLF molfile, a V3000 racemate."""
    crlf = molfile("C[C@H](N)c1ccccc1").replace("\n", "\r\n")
    svc.register(molfile=crlf, experiment_ref="E1", experiment_code="ABC-001",
                 experiment_url="https://eln.example/exp/1")
    svc.register(smiles="C[C@H](N)c1ccccc1", experiment_ref="E2")
    svc.register(smiles="C[C@H](N)c1ccccc1.Cl", experiment_ref="E2", product_index=2)
    svc.register(molfile=molfile("C[C@H](N)c1ccccc1 |&1:1|", v3000=True), experiment_ref="E3")
    svc.register(smiles="CCO", experiment_ref="E4", experiment_code="ABC\n004")
    svc.retire("000002_01_001", "wrong product\n$$$$\n> <evil>")
    return svc


def make_batch(**overrides) -> BatchRecord:
    base = dict(
        full_id="000001_01_001", core_no=1, stereo_no=1, batch_no=1, core_stereo_id="000001_01",
        experiment_ref="E1", experiment_code=None, experiment_url=None, product_index=1,
        molfile=molfile("CCO"), smiles="CCO", inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        stereo_mode="none", source="cli", created_at="2026-11-17T08:00:00Z",
        rdkit_version="2026.09.2", inchi_version="1.07.3",
    )
    base.update(overrides)
    return BatchRecord(**base)


def sdf_records(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    assert text.endswith("$$$$\n")
    return text.split("$$$$\n")[:-1]


# --------------------------------------------------------------------------- CSV

def test_columns_are_the_documented_ones():
    assert COLUMNS == EXPECTED_COLUMNS
    assert EXPORT_COLUMNS == EXPECTED_HEADER


def test_csv_round_trip(populated, tmp_path):
    batches = list(populated.repo.iter_batches())
    dest = tmp_path / "out.csv"
    assert export_csv(batches, dest) == 5

    raw = dest.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM: Excel opens it as UTF-8
    with open(dest, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert tuple(rows[0]) == EXPECTED_HEADER
    assert all(dict(zip(rows[0], r))["name"] == "" for r in rows[1:])  # no names given
    assert len(rows) == 1 + len(batches)
    for batch, row in zip(batches, rows[1:]):
        record = dict(zip(rows[0], row))
        for column in EXPECTED_COLUMNS:
            value = getattr(batch, column)
            assert record[column] == ("" if value is None else str(value)), column

    by_id = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
    assert by_id["000001_01_001"]["experiment_code"] == "ABC-001"
    assert by_id["000002_01_001"]["retired_reason"] == "wrong product\n$$$$\n> <evil>"
    assert by_id["000002_01_001"]["retired_at"] != ""
    assert by_id["000001_01_002"]["retired_at"] == ""
    assert by_id["000001_02_001"]["stereo_mode"] == "rac"


def test_csv_empty_registry_has_header_only(tmp_path):
    dest = tmp_path / "empty.csv"
    assert export_csv([], dest) == 0
    with open(dest, encoding="utf-8-sig", newline="") as fh:
        assert list(csv.reader(fh)) == [list(EXPECTED_HEADER)]


# --------------------------------------------------------------------------- names

NAME_R = "(1R)-1-phenylethan-1-amine"
NAME_SALT = "(1R)-1-phenylethan-1-amine hydrochloride; µ-test ''quoted'', comma"


def test_names_in_csv_and_sdf(populated, tmp_path):
    populated.set_name("000001_01", NAME_R)
    populated.set_name("000002_01", NAME_SALT)
    populated.export("csv", tmp_path / "n.csv")
    populated.export("sdf", tmp_path / "n.sdf")

    with open(tmp_path / "n.csv", encoding="utf-8-sig", newline="") as fh:
        rows = {r["full_id"]: r for r in csv.DictReader(fh)}
    assert rows["000001_01_001"]["name"] == NAME_R
    assert rows["000001_01_002"]["name"] == NAME_R          # every batch of the variant
    assert rows["000002_01_001"]["name"] == NAME_SALT        # retired batch too, verbatim
    assert rows["000001_02_001"]["name"] == ""              # the racemate has no name
    assert list(rows["000001_01_001"])[-3:] == ["name", "yield_isolated", "yield_analytical"]

    mols = {m.GetProp("full_id"): m for m in Chem.SDMolSupplier(str(tmp_path / "n.sdf"))}
    assert mols["000001_01_001"].GetProp("name") == NAME_R
    assert mols["000002_01_001"].GetProp("name") == NAME_SALT
    assert not mols["000001_02_001"].HasProp("name")


def test_names_leave_the_sdf_molfile_unchanged(populated, tmp_path):
    batches = list(populated.repo.iter_batches())
    dest = tmp_path / "n.sdf"
    export_sdf(batches, dest, {"000001_01": "x $$$$ name", "000003_01": "$$$$"})
    for batch, record in zip(batches, sdf_records(dest)):
        stored = batch.molfile.replace("\r\n", "\n")
        assert record.startswith(stored if stored.endswith("\n") else stored + "\n")
    mols = {m.GetProp("full_id"): m for m in Chem.SDMolSupplier(str(dest))}
    assert len(mols) == 5
    assert mols["000003_01_001"].GetProp("name") == " $$$$"


# --------------------------------------------------------------------------- SDF

def test_sdf_round_trip(populated, tmp_path):
    batches = list(populated.repo.iter_batches())
    dest = tmp_path / "out.sdf"
    assert export_sdf(batches, dest) == 5

    mols = list(Chem.SDMolSupplier(str(dest)))
    assert len(mols) == len(batches)
    for batch, mol in zip(batches, mols):
        assert mol is not None, batch.full_id
        props = mol.GetPropsAsDict()
        assert props["full_id"] == batch.full_id
        assert str(props["core_stereo_id"]) == batch.core_stereo_id
        assert props["inchikey"] == batch.inchikey
        assert props["smiles"] == batch.smiles
        assert str(props["experiment_ref"]) == batch.experiment_ref
        assert int(props["product_index"]) == batch.product_index
        # The structure in the file is the stored one.
        assert Chem.MolToInchiKey(mol) == batch.inchikey
        # Empty values are skipped.
        if batch.retired_at is None:
            assert "retired_at" not in props and "retired_reason" not in props
        if batch.experiment_url is None:
            assert "experiment_url" not in props

    # The stored molfile text is preserved (only line endings normalised).
    for batch, record in zip(batches, sdf_records(dest)):
        stored = batch.molfile.replace("\r\n", "\n")
        assert record.startswith(stored if stored.endswith("\n") else stored + "\n")
        assert "\r" not in record


def test_sdf_sanitises_awkward_values(populated, tmp_path):
    dest = tmp_path / "out.sdf"
    export_sdf(populated.repo.iter_batches(), dest)
    lines = dest.read_text(encoding="utf-8").split("\n")
    assert sum(1 for line in lines if line.startswith("$$$$")) == 5
    assert all(line == "$$$$" for line in lines if line.startswith("$$$$"))
    assert all(line.startswith("> <") for line in lines if line.startswith(">"))
    mols = {m.GetProp("full_id"): m for m in Chem.SDMolSupplier(str(dest))}
    assert mols["000002_01_001"].GetProp("retired_reason") == "wrong product $$$$ > <evil>"
    assert mols["000003_01_001"].GetProp("experiment_code") == "ABC 004"


@pytest.mark.parametrize("value, expected", [
    ("plain", "plain"),
    ("a\nb\r\nc\rd", "a b c d"),
    ("$$$$", " $$$$"),
    ("> <x>", " > <x>"),
    ("x\n$$$$", "x $$$$"),
    (None, ""),
    (12, "12"),
])
def test_sdf_value(value, expected):
    assert sdf_value(value) == expected


def test_sdf_value_fields_never_break_records(tmp_path):
    weird = make_batch(
        experiment_code="$$$$", experiment_url="> <full_id>\nfake", source="cli",
        retired_at="2026-11-17T09:00:00Z", retired_reason="\n\n$$$$\n",
    )
    dest = tmp_path / "w.sdf"
    assert export_sdf([weird, make_batch(full_id="000001_01_002", batch_no=2)], dest) == 2
    mols = list(Chem.SDMolSupplier(str(dest)))
    assert [m.GetProp("full_id") for m in mols] == ["000001_01_001", "000001_01_002"]
    assert mols[0].GetProp("experiment_code") == " $$$$"
    assert mols[0].GetProp("experiment_url") == " > <full_id> fake"
    assert mols[0].GetProp("retired_reason").strip() == "$$$$"


def test_sdf_molblock_adds_or_cuts_at_m_end():
    mb = molfile("CCO")
    body = mb[: mb.index("M  END")]
    assert sdf_molblock(body).endswith("M  END\n")
    assert sdf_molblock(body + "\n\n").endswith("\nM  END\n")
    assert sdf_molblock(mb + "> <x>\n1\n\n$$$$\n") == mb
    assert sdf_molblock(mb.replace("\n", "\r\n")) == mb
    assert sdf_molblock("$$$$\n" + mb.split("\n", 1)[1]).startswith("\n")


def test_sdf_without_m_end_still_reads(tmp_path):
    mb = molfile("CCO")
    dest = tmp_path / "x.sdf"
    export_sdf([make_batch(molfile=mb[: mb.index("M  END")])], dest)
    mols = list(Chem.SDMolSupplier(str(dest)))
    assert len(mols) == 1 and mols[0] is not None


# --------------------------------------------------------------------------- active only

def test_active_only_export(populated, tmp_path):
    all_csv = populated.export("csv", tmp_path / "all.csv")
    active = populated.export("csv", tmp_path / "active.csv", include_retired=False)
    sdf = populated.export("sdf", tmp_path / "active.sdf", include_retired=False)
    assert all_csv["count"] == 5 and active["count"] == 4 and sdf["count"] == 4
    with open(tmp_path / "active.csv", encoding="utf-8-sig", newline="") as fh:
        ids = [r["full_id"] for r in csv.DictReader(fh)]
    assert "000002_01_001" not in ids and len(ids) == 4
    assert len(list(Chem.SDMolSupplier(str(tmp_path / "active.sdf")))) == 4


# --------------------------------------------------------------------------- atomic write

def _failing(batches):
    for b in batches:
        yield b
    raise RuntimeError("disk on fire")


@pytest.mark.parametrize("writer", [export_csv, export_sdf])
def test_failed_export_leaves_existing_file_untouched(writer, tmp_path):
    dest = tmp_path / "out.file"
    dest.write_text("OLD CONTENT", encoding="utf-8")
    with pytest.raises(RuntimeError):
        writer(_failing([make_batch()]), dest)
    assert dest.read_text(encoding="utf-8") == "OLD CONTENT"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.file"]


@pytest.mark.parametrize("writer", [export_csv, export_sdf])
def test_failed_export_creates_no_file(writer, tmp_path):
    dest = tmp_path / "new.file"
    with pytest.raises(RuntimeError):
        writer(_failing([make_batch()]), dest)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("writer", [export_csv, export_sdf])
def test_successful_export_replaces_and_leaves_no_temp(writer, tmp_path):
    dest = tmp_path / "out.file"
    dest.write_text("OLD", encoding="utf-8")
    assert writer([make_batch()], dest) == 1
    assert "OLD" not in dest.read_text(encoding="utf-8-sig")
    assert [p.name for p in tmp_path.iterdir()] == ["out.file"]


def test_export_into_missing_folder_is_a_registry_error(populated, tmp_path):
    from compound_registry.errors import RegistryError

    with pytest.raises(RegistryError) as exc:
        populated.export("csv", tmp_path / "no" / "such" / "dir" / "x.csv")
    assert exc.value.code == "export_failed"


def test_batch_record_fields_cover_columns():
    names = {f.name for f in dataclasses.fields(BatchRecord)}
    assert set(EXPECTED_COLUMNS) <= names
