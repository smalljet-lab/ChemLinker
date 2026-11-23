"""Compound folders: compound_registry.files, the [files] config key, register's
folder, compound_files / read_pdf (service, host) and the ``folders`` command.
Everything runs in tmp_path."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from compound_registry import files, host, protocol
from compound_registry.cli import main
from compound_registry.config import DEFAULT_CONFIG_TEXT, Config, load_config
from compound_registry.errors import ConfigError, RegistryError
from compound_registry.host import HomeChoice
from compound_registry.idformat import IdFormat
from compound_registry.service import RegistryService

AMINE_R = "C[C@H](N)c1ccccc1"
AMINE_S = "C[C@@H](N)c1ccccc1"
ETHANOL = "CCO"
PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("compound_registry.cli._is_interactive", lambda: False)


def code_of(info: pytest.ExceptionInfo) -> str:
    return info.value.code


# =========================================================================== files module
# --------------------------------------------------------------------------- names

@pytest.mark.parametrize("cs", ["EX0001_01", "000042_02", "UB-00001_01", "a.b", "x" * 100])
def test_usable_folder_names(cs):
    assert files.folder_name(cs) == cs


@pytest.mark.parametrize("cs", ["", "a/b", "a\\b", "a b", "a:b", ".x", "x.", "..", "CON", "con",
                                "NUL.01", "COM1", "lpt9", "AUX", "PRN", "x" * 101, "caf\xe9",
                                "EX#0001_01", "a\x00b", None, 42])
def test_unusable_folder_names(cs):
    assert files.folder_name(cs) is None
    assert files.compound_folder("C:/anything", cs) is None


def test_compound_folder_is_absolute(tmp_path):
    assert files.compound_folder(tmp_path / "c", "EX0001_01") == tmp_path / "c" / "EX0001_01"


# --------------------------------------------------------------------------- making a folder

def test_ensure_folder_creates_folder_and_molfile_once(tmp_path):
    base = tmp_path / "deep" / "compounds"
    r = files.ensure_folder(base, "EX0001_01", "the molfile\nM  END")
    assert r == files.FolderResult(base / "EX0001_01", True, None)
    mol = base / "EX0001_01" / "EX0001_01.mol"
    assert mol.read_text(encoding="utf-8") == "the molfile\nM  END\n"
    mol.write_text("changed by the user", encoding="utf-8")
    again = files.ensure_folder(base, "EX0001_01", "another molfile")
    assert again == files.FolderResult(base / "EX0001_01", False, None)
    assert mol.read_text(encoding="utf-8") == "changed by the user"       # never overwritten


def test_ensure_folder_adds_a_missing_molfile_to_an_existing_folder(tmp_path):
    folder = tmp_path / "EX0001_01"
    folder.mkdir()
    (folder / "spectrum.pdf").write_bytes(PDF)
    r = files.ensure_folder(tmp_path, "EX0001_01", "mol")
    assert r.created is False and r.warning is None
    assert (folder / "EX0001_01.mol").read_text(encoding="utf-8") == "mol\n"
    assert (folder / "spectrum.pdf").read_bytes() == PDF


def test_ensure_folder_without_molfile_writes_none(tmp_path):
    r = files.ensure_folder(tmp_path, "EX0001_01", None)
    assert r.created and list(r.path.iterdir()) == []
    assert files.ensure_folder(tmp_path, "EX0001_02", "   ").path.is_dir()


def test_unusable_id_gets_no_folder(tmp_path):
    r = files.ensure_folder(tmp_path, "EX#1_01", "mol")
    assert r.path is None and r.created is False and "folder name" in r.warning
    assert list(tmp_path.iterdir()) == []


def test_a_file_in_place_of_the_files_folder_is_a_warning(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    r = files.ensure_folder(blocker / "compounds", "EX0001_01", "mol")
    assert r.path is None and r.created is False and str(blocker / "compounds") in r.warning


def test_a_file_in_place_of_the_compound_folder_is_a_warning(tmp_path):
    (tmp_path / "EX0001_01").write_text("x", encoding="utf-8")
    r = files.ensure_folder(tmp_path, "EX0001_01", "mol")
    assert r.path is None and "in the way" in r.warning
    assert (tmp_path / "EX0001_01").read_text(encoding="utf-8") == "x"


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses these characters")
def test_an_impossible_files_folder_is_a_warning(tmp_path):
    r = files.ensure_folder(tmp_path / "bad<name>|", "EX0001_01", "mol")
    assert r.path is None and r.warning


def test_molfile_that_cannot_be_opened_is_a_warning(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(files, "open", refuse, raising=False)
    r = files.ensure_folder(tmp_path, "EX0001_01", "mol")
    assert r.path == tmp_path / "EX0001_01" and r.created is True
    assert "EX0001_01.mol" in r.warning and "Access is denied" in r.warning


def test_molfile_write_failure_leaves_no_half_file(tmp_path, monkeypatch):
    real_open = open

    class DiskFull:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, text):
            self.handle.write(text[:3])
            raise OSError(28, "There is not enough space on the disk")

    monkeypatch.setattr(files, "open", lambda *a, **k: DiskFull(real_open(*a, **k)),
                        raising=False)
    r = files.ensure_folder(tmp_path, "EX0001_01", "molfile")
    assert r.path.is_dir() and "not enough space" in r.warning
    assert not (r.path / "EX0001_01.mol").exists()


# --------------------------------------------------------------------------- listing

def _make_junction(link: Path, target: Path) -> bool:
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return True
        except (ImportError, OSError, AttributeError):
            return False
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def test_list_files_lists_regular_files_only_sorted_ignoring_case(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "b.mnova").write_bytes(b"12345")
    (folder / "A.PDF").write_bytes(PDF)
    (folder / "c.pdf").write_bytes(PDF)
    (folder / "sub").mkdir()
    (folder / "sub" / "inner.pdf").write_bytes(PDF)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "far.pdf").write_bytes(PDF)
    _make_junction(folder / "linked", outside)        # if possible: not entered either
    os.utime(folder / "b.mnova", (1_700_000_000, 1_700_000_000))
    listed, truncated, warning = files.list_files(folder)
    assert warning is None and truncated is False
    assert [f["name"] for f in listed] == ["A.PDF", "b.mnova", "c.pdf"]
    assert [f["is_pdf"] for f in listed] == [True, False, True]
    b = listed[1]
    assert b == {"name": "b.mnova", "size": 5, "modified": "2023-11-14T22:13:20Z",
                 "is_pdf": False}
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", f["modified"]) for f in listed)


def test_list_files_truncates(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    listed, truncated, _ = files.list_files(tmp_path, limit=3)
    assert [f["name"] for f in listed] == ["f0.txt", "f1.txt", "f2.txt"] and truncated is True
    listed, truncated, _ = files.list_files(tmp_path, limit=5)
    assert len(listed) == 5 and truncated is False


def test_list_files_of_a_missing_folder_or_a_file(tmp_path):
    assert files.list_files(tmp_path / "missing") == ([], False, None)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    listed, truncated, warning = files.list_files(tmp_path / "file")
    assert listed == [] and truncated is False and str(tmp_path / "file") in warning


# --------------------------------------------------------------------------- name resolution

def test_resolve_pdf_name(tmp_path):
    assert files.resolve_pdf_name(tmp_path, "x.pdf") == tmp_path / "x.pdf"
    assert files.resolve_pdf_name(tmp_path, "EX0001_01_001_H1 CDCl3.pdf").parent == tmp_path


@pytest.mark.parametrize("name", ["../x.pdf", "..", ".", "a/b.pdf", "a\\b.pdf", "C:x.pdf",
                                  "x.pdf:stream", "a\x00.pdf", "a\n.pdf", "a\x7f.pdf", "",
                                  "   ", "x" * 300 + ".pdf", None, 3])
def test_resolve_pdf_name_refuses_anything_but_a_plain_name(tmp_path, name):
    with pytest.raises(RegistryError) as info:
        files.resolve_pdf_name(tmp_path, name)
    assert code_of(info) == "invalid_argument"


def test_a_link_leading_outside_the_folder_is_refused(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(PDF)
    (folder / "inside.pdf").write_bytes(PDF)
    try:
        os.symlink(secret, folder / "link.pdf")
        os.symlink(folder / "inside.pdf", folder / "local.pdf")
    except (OSError, NotImplementedError):
        pytest.skip("this system does not allow creating symbolic links")
    with pytest.raises(RegistryError) as info:
        files.resolve_pdf_name(folder, "link.pdf")
    assert code_of(info) == "file_unavailable" and "outside" in info.value.message
    assert files.resolve_pdf_name(folder, "local.pdf") == folder / "local.pdf"


def test_a_name_whose_real_path_is_elsewhere_is_refused(tmp_path, monkeypatch):
    """The same check without needing the right to create links."""
    folder = tmp_path / "f"
    folder.mkdir()
    real_realpath = os.path.realpath

    def realpath(path, *args, **kwargs):
        if str(path).endswith("link.pdf"):
            return str(tmp_path / "secret.pdf")
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(files.os.path, "realpath", realpath)
    with pytest.raises(RegistryError) as info:
        files.resolve_pdf_name(folder, "link.pdf")
    assert code_of(info) == "file_unavailable" and "outside" in info.value.message


def test_a_folder_reached_through_a_junction_still_serves_its_files(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "x.pdf").write_bytes(PDF)
    if not _make_junction(tmp_path / "via", real):
        pytest.skip("this system does not allow creating a junction")
    path = files.resolve_pdf_name(tmp_path / "via", "x.pdf")
    assert base64.b64decode(files.read_pdf_chunk(path)["data"]) == PDF


# --------------------------------------------------------------------------- PDF checks

def test_read_a_small_pdf(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(PDF)
    piece = files.read_pdf_chunk(p)
    assert piece == {"name": "s.pdf", "size": len(PDF), "offset": 0,
                     "data": base64.b64encode(PDF).decode("ascii"), "eof": True}
    assert files.check_pdf(p) == len(PDF) and files.pdf_readable(str(p))
    end = files.read_pdf_chunk(p, len(PDF))
    assert end["data"] == "" and end["eof"] is True


def test_read_pdf_in_pieces(tmp_path):
    p = tmp_path / "big.PDF"
    content = PDF + bytes(range(256)) * 10
    p.write_bytes(content)
    got, offset = b"", 0
    while True:
        piece = files.read_pdf_chunk(p, offset, chunk_bytes=1000)
        assert piece["size"] == len(content) and piece["offset"] == offset
        data = base64.b64decode(piece["data"])
        assert len(data) <= 1000
        got += data
        offset += len(data)
        if piece["eof"]:
            break
    assert got == content


def test_a_file_that_changes_size_is_noticed(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(PDF * 10)
    first = files.read_pdf_chunk(p, 0, chunk_bytes=50)
    assert first["eof"] is False
    with open(p, "ab") as fh:
        fh.write(b"appended")
    second = files.read_pdf_chunk(p, 50, chunk_bytes=50)
    assert second["size"] == first["size"] + len(b"appended")


@pytest.mark.parametrize("offset", [-1, True, 1.5, "0"])
def test_bad_offsets(tmp_path, offset):
    p = tmp_path / "s.pdf"
    p.write_bytes(PDF)
    with pytest.raises(RegistryError) as info:
        files.read_pdf_chunk(p, offset)
    assert code_of(info) == "invalid_argument"


def test_offset_beyond_the_end(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(PDF)
    with pytest.raises(RegistryError) as info:
        files.read_pdf_chunk(p, len(PDF) + 1)
    assert code_of(info) == "invalid_argument"


def _unavailable(path, match: str) -> None:
    with pytest.raises(RegistryError) as info:
        files.read_pdf_chunk(path)
    assert code_of(info) == "file_unavailable"
    assert match in info.value.message
    assert files.pdf_readable(str(path)) is False


def test_pdf_refusals(tmp_path, monkeypatch):
    _unavailable(tmp_path / "gone.pdf", "is not there any more")
    _unavailable(Path("relative.pdf"), "full path")
    text = tmp_path / "x.txt"
    text.write_bytes(PDF)
    _unavailable(text, "x.txt")
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"MZ not a pdf at all")
    _unavailable(fake, "does not start like one")
    tiny = tmp_path / "tiny.pdf"
    tiny.write_bytes(b"%PD")
    _unavailable(tiny, "tiny.pdf")
    (tmp_path / "dir.pdf").mkdir()
    _unavailable(tmp_path / "dir.pdf", "not a file")
    big = tmp_path / "big.pdf"
    big.write_bytes(PDF)
    monkeypatch.setattr(files, "MAX_PDF_BYTES", len(PDF) - 1)
    _unavailable(big, "too large")
    assert files.pdf_readable(None) is False and files.pdf_readable("") is False


# =========================================================================== config

def test_files_folder_default_and_text():
    cfg = load_config()
    assert cfg.files_dir == cfg.home / "compounds"
    import tomllib

    assert tomllib.loads(DEFAULT_CONFIG_TEXT)["files"]["folder"] == "compounds"
    assert "MestReNova" in DEFAULT_CONFIG_TEXT


def test_config_without_files_section(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text("[backup]\nkeep = 5\n", encoding="utf-8")
    assert load_config(home).files_dir == home / "compounds"


def test_hand_built_config_gets_the_default_folder(tmp_path):
    cfg = Config(home=tmp_path, config_path=tmp_path / "config.toml",
                 db_path=tmp_path / "r.sqlite", backup_dir=tmp_path / "b")
    assert cfg.files_dir == tmp_path / "compounds"


@pytest.mark.parametrize("value, expected", [
    ('"spectra"', lambda home, tmp: home / "spectra"),
    ('"../shared/c"', lambda home, tmp: tmp / "shared" / "c"),
    ('"~/c"', lambda home, tmp: tmp / "user" / "c"),
])
def test_files_folder_values(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text(f"[files]\nfolder = {value}\n", encoding="utf-8")
    assert load_config(home).files_dir == expected(home, tmp_path)


def test_absolute_files_folder(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    target = tmp_path / "abs" / "c"
    (home / "config.toml").write_text(f"[files]\nfolder = '{target}'\n", encoding="utf-8")
    assert load_config(home).files_dir == target


@pytest.mark.parametrize("text, words", [
    ('[files]\nfolder = ""\n', "files.folder"),
    ("[files]\nfolder = 3\n", "files.folder"),
    ('[files]\nfolders = "x"\n', "Unknown key"),
    ('[files]\nfolder = "registry.sqlite"\n', "same path"),
    ('[files]\nfolder = "D:rel"\n', "files.folder"),
    ('files = "x"\n', "[files]"),
])
def test_bad_files_folder(tmp_path, text, words):
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(home)
    assert words in info.value.message


# =========================================================================== service

@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def svc(home):
    s = RegistryService.open(home)
    yield s
    s.close()


def reg(svc, smiles=AMINE_R, ref="E1", index=1, code="ABC-001"):
    return svc.register(smiles=smiles, experiment_ref=ref, experiment_code=code,
                        product_index=index)


def test_register_makes_the_folder_with_the_structure(svc, home):
    r = reg(svc)
    folder = home / "compounds" / "000001_01"
    assert r["id"] == "000001_01_001" and r["created"]
    assert r["folder"] == {"path": str(folder), "created": True}
    mol = (folder / "000001_01.mol").read_text(encoding="utf-8")
    assert mol.strip() == svc.repo.get_stereo("000001_01").molfile.strip()
    # The same registration again: the folder is already there.
    again = reg(svc)
    assert again["created"] is False and again["folder"] == {"path": str(folder), "created": False}
    # A second batch of the same stereo variant keeps the user's .mol file.
    (folder / "000001_01.mol").write_text("edited", encoding="utf-8")
    second = reg(svc, ref="E2")
    assert second["id"] == "000001_01_002" and second["folder"]["created"] is False
    assert (folder / "000001_01.mol").read_text(encoding="utf-8") == "edited"
    # A new stereo variant gets its own folder.
    other = reg(svc, smiles=AMINE_S, ref="E3")
    assert other["folder"] == {"path": str(home / "compounds" / "000001_02"), "created": True}


def test_register_remakes_a_removed_folder(svc, home):
    reg(svc)
    shutil.rmtree(home / "compounds")
    r = reg(svc)
    assert r["created"] is False and r["folder"]["created"] is True
    assert (home / "compounds" / "000001_01" / "000001_01.mol").is_file()


def test_folder_failure_is_a_warning_and_the_id_is_issued(home, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    RegistryService.open(home).close()
    config = replace(load_config(home), files_dir=blocker / "compounds")
    with RegistryService(config) as s:
        r = reg(s)
        assert r["created"] is True and r["folder"] is None
        assert any("could not be made" in w for w in r["warnings"])
        assert s.get("000001_01_001")["full_id"] == "000001_01_001"


def test_an_unexpected_folder_error_never_fails_the_registration(svc, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("surprise")

    monkeypatch.setattr(files, "ensure_folder", boom)
    r = reg(svc)
    assert r["created"] is True and r["folder"] is None
    assert any("surprise" in w for w in r["warnings"])
    assert svc.get(r["id"])["full_id"] == r["id"]


def test_molfile_warning_keeps_the_folder(svc, home, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(files, "open", refuse, raising=False)
    r = reg(svc)
    assert r["folder"] == {"path": str(home / "compounds" / "000001_01"), "created": True}
    assert any("000001_01.mol" in w for w in r["warnings"])


@pytest.fixture
def odd_home(tmp_path, monkeypatch) -> Path:
    """A registry whose IDs cannot be folder names. Such a format is refused
    for a new registry, but a registry that has one keeps working."""
    path = tmp_path / "odd"
    path.mkdir()
    (path / "config.toml").write_text('[id]\nformat = "EX#{core:04d}_{stereo:02d}_{batch:03d}"\n',
                                      encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(IdFormat, "require_file_safe", lambda self: None)
        RegistryService.open(path).close()
    return path


def test_ids_that_cannot_be_folder_names(odd_home):
    with RegistryService.open(odd_home) as s:
        r = reg(s)
        assert r["id"] == "EX#0001_01_001" and r["folder"] is None
        assert any("folder name" in w for w in r["warnings"])
        listing = s.compound_files("EX#0001_01")
        assert listing["folder"] is None and listing["files"] == []
        assert any("folder name" in w for w in listing["warnings"])
        with pytest.raises(RegistryError) as info:
            s.read_pdf("EX#0001_01", name="x.pdf")
        assert code_of(info) == "file_unavailable"
        made = s.make_folders()
        assert made["failed"] == 1 and made["folders"][0]["path"] is None


# --------------------------------------------------------------------------- compound_files

def spectrum(folder: Path, sample: str, **extra) -> dict:
    data = {
        "title": f"user.011.1.fid\nNummer {sample}", "comment": "Nummer " + sample,
        "sample": sample, "nucleus": "1H", "solvent": "CDCl3", "experiment": "1D",
        "pulse_sequence": "zg30", "frequency_mhz": 500.13, "dimensions": 1,
        "report_html": "<html><body><p>1H NMR (500 MHz, CDCl3) &delta; 7.40\n(t, 1H)</p>"
                       "</body></html>",
        "parameters": {"Temperature": "298.1", "acquisition time": "3.27", "Scans": 16},
        "mnova_file": str(folder / f"000001_01_001_{sample}.mnova"),
        "pdf_file": str(folder / f"000001_01_001_{sample}.pdf"),
    }
    data.update(extra)
    return data


def test_compound_files_with_spectra_and_files(svc, home):
    reg(svc)
    folder = home / "compounds" / "000001_01"
    (folder / "000001_01_001_S1.pdf").write_bytes(PDF)
    (folder / "000001_01_001_S1.mnova").write_bytes(b"mnova")
    (folder / "notes.txt").write_text("hello", encoding="utf-8")
    (folder / "sub").mkdir()
    svc.record_spectra("000001_01_001", [
        spectrum(folder, "S1"),
        spectrum(folder, "S2", title="user.012.1.fid", pdf_file="relative\\S2.pdf"),
    ])
    result = svc.compound_files("000001_01")
    assert set(result) == {"core_stereo_id", "folder", "spectra", "files", "files_truncated",
                           "warnings"}
    assert result["core_stereo_id"] == "000001_01"
    assert result["folder"] == {"path": str(folder), "exists": True}
    assert result["warnings"] == [] and result["files_truncated"] is False
    first, second = result["spectra"]
    assert set(first) == {"full_id", "spectrum_key", "updated_at", "experiment_code", "title",
                          "comment", "sample", "nucleus", "solvent", "experiment",
                          "pulse_sequence", "frequency_mhz", "dimensions", "report",
                          "parameters", "mnova_file", "pdf_file", "pdf_available", "crude"}
    assert first["crude"] is False and second["crude"] is False
    assert first["full_id"] == "000001_01_001" and first["experiment_code"] == "ABC-001"
    assert first["report"] == "1H NMR (500 MHz, CDCl3) \u03b4 7.40 (t, 1H)"
    assert "\n" not in first["report"] and "report_html" not in first
    assert first["parameters"] == [{"name": "acquisition time", "value": "3.27"},
                                   {"name": "Scans", "value": "16"},
                                   {"name": "Temperature", "value": "298.1"}]
    assert first["frequency_mhz"] == 500.13 and first["dimensions"] == 1
    assert first["pdf_available"] is True
    assert second["pdf_file"] == "relative\\S2.pdf" and second["pdf_available"] is False
    by_name = {f["name"]: f for f in result["files"]}
    assert list(by_name) == ["000001_01.mol", "000001_01_001_S1.mnova", "000001_01_001_S1.pdf",
                             "notes.txt"]
    assert by_name["000001_01_001_S1.pdf"]["recorded"] is True
    assert by_name["000001_01_001_S1.pdf"]["is_pdf"] is True
    assert by_name["000001_01_001_S1.mnova"]["recorded"] is True
    assert by_name["notes.txt"]["recorded"] is False and by_name["notes.txt"]["size"] == 5
    assert by_name["000001_01.mol"]["recorded"] is False
    json.dumps(result, allow_nan=False)


def test_compound_files_shows_only_the_path_of_a_crude_spectrum(svc, home):
    reg(svc)
    folder = home / "compounds" / "000001_01"
    crude_file = folder / "000001_01_001_S1_crude.mnova"
    crude_file.write_bytes(b"mnova")
    (folder / "000001_01_001_S1.pdf").write_bytes(PDF)
    # even when a careless caller sends the details, none of them is kept or shown
    stored = svc.record_spectra("000001_01_001", [
        spectrum(folder, "S1", crude=True, mnova_file=str(crude_file))])
    data = stored["spectra"][0]["data"]
    assert data["crude"] is True and data["mnova_file"] == str(crude_file)
    assert not {"report", "report_html", "report_kind", "pdf_file", "parameters"} & set(data)
    (item,) = svc.compound_files("000001_01")["spectra"]
    assert item["crude"] is True and item["mnova_file"] == str(crude_file)
    assert item["full_id"] == "000001_01_001" and item["experiment_code"] == "ABC-001"
    assert item["pdf_file"] is None and item["pdf_available"] is False
    assert item["report"] is None and item["parameters"] == []
    assert all(item[f] is None for f in ("title", "comment", "sample", "nucleus", "solvent",
                                         "experiment", "pulse_sequence", "frequency_mhz",
                                         "dimensions"))
    by_name = {f["name"]: f for f in svc.compound_files("000001_01")["files"]}
    assert by_name[crude_file.name]["recorded"] is True
    with pytest.raises(RegistryError) as info:                  # no PDF goes with a crude spectrum
        svc.read_pdf("000001_01", full_id="000001_01_001",
                     spectrum_key=stored["spectra"][0]["spectrum_key"])
    assert code_of(info) == "file_unavailable"
    # saved again as a full spectrum, the record is a full one again
    svc.record_spectra("000001_01_001", [spectrum(folder, "S1")])
    (item,) = svc.compound_files("000001_01")["spectra"]
    assert item["crude"] is False and item["report"] and item["pdf_available"] is True


def test_compound_files_without_folder_or_spectra_creates_nothing(svc, home):
    reg(svc)
    shutil.rmtree(home / "compounds")
    result = svc.compound_files(" 000001_01 ")
    assert result["folder"] == {"path": str(home / "compounds" / "000001_01"), "exists": False}
    assert result["spectra"] == [] and result["files"] == [] and result["warnings"] == []
    assert not (home / "compounds").exists()                     # read-only


def test_compound_files_pdf_gone(svc, home):
    reg(svc)
    folder = home / "compounds" / "000001_01"
    svc.record_spectra("000001_01_001", [spectrum(folder, "S1")])   # the files were never saved
    (item,) = svc.compound_files("000001_01")["spectra"]
    assert item["pdf_available"] is False


def test_compound_files_unknown_variant(svc):
    reg(svc)
    for bad in ("000001_09", "000009_01"):
        with pytest.raises(RegistryError) as info:
            svc.compound_files(bad)
        assert code_of(info) == "not_found"
    with pytest.raises(RegistryError) as info:
        svc.compound_files("  ")
    assert code_of(info) == "invalid_argument"


def test_compound_files_lists_the_spectra_of_this_variant_only(svc, home):
    reg(svc)
    reg(svc, ref="E2")
    reg(svc, smiles=AMINE_S, ref="E3")
    folder = home / "compounds" / "000001_01"
    svc.record_spectra("000001_01_002", [spectrum(folder, "B")])
    svc.record_spectra("000001_01_001", [spectrum(folder, "A")])
    svc.record_spectra("000001_02_001", [spectrum(folder, "C")])
    ids = [s["full_id"] for s in svc.compound_files("000001_01")["spectra"]]
    assert ids == ["000001_01_001", "000001_01_002"]


def test_compound_files_stays_within_the_response_limit(svc, home):
    reg(svc)
    folder = home / "compounds" / "000001_01"
    params = {f"p{i:03d}": "v" * 1900 for i in range(200)}            # ~ 400 kB per spectrum
    svc.record_spectra("000001_01_001", [
        spectrum(folder, f"S{i}", title=f"data.{i}.fid", parameters=params) for i in range(3)])
    result = svc.compound_files("000001_01")
    size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert size <= protocol.MAX_RESPONSE_BYTES
    spectra = result["spectra"]
    assert spectra[0]["parameters"] and spectra[-1]["parameters"] == []
    assert any("acquisition parameters" in w for w in result["warnings"])
    # Through the host it is sent, not refused as too large.
    response = host.handle_message(
        json.dumps({"id": 1, "v": 1, "op": "compound_files",
                    "params": {"core_stereo_id": "000001_01"}}).encode(),
        HomeChoice(home, "host-config")).response
    body = json.loads(host.encode_response(response, "compound_files"))
    assert body["ok"] is True


# --------------------------------------------------------------------------- read_pdf

def read_all(read, **kwargs) -> tuple[bytes, int]:
    """Read a whole PDF piece by piece; returns (content, pieces)."""
    got, offset, pieces, size = b"", 0, 0, None
    while True:
        piece = read(offset=offset, **kwargs)
        size = piece["size"] if size is None else size
        assert piece["size"] == size and piece["offset"] == offset
        data = base64.b64decode(piece["data"])
        assert len(data) <= protocol.PDF_CHUNK_BYTES
        got += data
        offset += len(data)
        pieces += 1
        if piece["eof"]:
            return got, pieces


def test_read_pdf_by_name_and_by_spectrum(svc, home):
    reg(svc)
    folder = home / "compounds" / "000001_01"
    content = PDF + os.urandom(protocol.PDF_CHUNK_BYTES * 2 + 1234)
    (folder / "000001_01_001_S1.pdf").write_bytes(content)
    svc.record_spectra("000001_01_001", [spectrum(folder, "S1")])
    key = svc.compound_files("000001_01")["spectra"][0]["spectrum_key"]
    got, pieces = read_all(lambda **kw: svc.read_pdf("000001_01", **kw),
                           name="000001_01_001_S1.pdf")
    assert got == content and pieces == 3
    got, _ = read_all(lambda **kw: svc.read_pdf("000001_01", **kw),
                      full_id="000001_01_001", spectrum_key=key)
    assert got == content


def test_read_pdf_recorded_outside_the_folder(svc, home, tmp_path):
    reg(svc)
    elsewhere = tmp_path / "nmr"
    elsewhere.mkdir()
    (elsewhere / "x.pdf").write_bytes(PDF)
    svc.record_spectra("000001_01_001", [spectrum(home, "S1", pdf_file=str(elsewhere / "x.pdf"))])
    key = svc.spectra("000001_01_001")["spectra"][0]["spectrum_key"]
    piece = svc.read_pdf("000001_01", full_id="000001_01_001", spectrum_key=key)
    assert base64.b64decode(piece["data"]) == PDF and piece["name"] == "x.pdf"


def _refused(svc, code: str, *args, **kwargs) -> str:
    with pytest.raises(RegistryError) as info:
        svc.read_pdf(*args, **kwargs)
    assert code_of(info) == code, info.value.message
    return info.value.message


def test_read_pdf_refusals(svc, home, monkeypatch):
    reg(svc)
    reg(svc, smiles=AMINE_S, ref="E2")
    folder = home / "compounds" / "000001_01"
    (folder / "ok.pdf").write_bytes(PDF)
    (folder / "fake.pdf").write_bytes(b"not a pdf")
    (folder / "notes.txt").write_bytes(PDF)
    svc.record_spectra("000001_01_001", [
        spectrum(folder, "S1", title="a.fid", pdf_file=None),
        spectrum(folder, "S2", title="b.fid", pdf_file="relative.pdf"),
        spectrum(folder, "S3", title="c.fid", pdf_file=str(folder / "gone.pdf")),
    ])
    keys = [s["spectrum_key"] for s in svc.compound_files("000001_01")["spectra"]]
    no_pdf, relative, gone = keys
    svc.record_spectra("000001_02_001", [spectrum(folder, "S9", pdf_file=str(folder / "ok.pdf"))])
    other_key = svc.spectra("000001_02_001")["spectra"][0]["spectrum_key"]

    _refused(svc, "not_found", "000001_07", name="ok.pdf")
    _refused(svc, "not_found", "000001_01", full_id="000001_02_001", spectrum_key=other_key)
    _refused(svc, "not_found", "000001_01", full_id="000001_01_009", spectrum_key=no_pdf)
    _refused(svc, "not_found", "000001_01", full_id="000001_01_001", spectrum_key="nope")
    assert "No PDF is recorded" in _refused(svc, "file_unavailable", "000001_01",
                                           full_id="000001_01_001", spectrum_key=no_pdf)
    # Only a file name is recorded: it is looked for in the compound's folder.
    message = _refused(svc, "file_unavailable", "000001_01",
                       full_id="000001_01_001", spectrum_key=relative)
    assert "not there any more" in message and str(folder / "relative.pdf") in message
    assert "not there any more" in _refused(svc, "file_unavailable", "000001_01",
                                            full_id="000001_01_001", spectrum_key=gone)
    assert "gone.pdf" in _refused(svc, "file_unavailable", "000001_01", name="gone.pdf")
    _refused(svc, "file_unavailable", "000001_01", name="fake.pdf")
    _refused(svc, "file_unavailable", "000001_01", name="notes.txt")
    _refused(svc, "file_unavailable", "000001_01", name="000001_01.mol")
    for bad in ("../ok.pdf", "..\\000001_01\\ok.pdf", str(folder / "ok.pdf"), "..", "x:y.pdf"):
        _refused(svc, "invalid_argument", "000001_01", name=bad)
    _refused(svc, "invalid_argument", "000001_01")
    _refused(svc, "invalid_argument", "000001_01", name="ok.pdf", full_id="000001_01_001",
             spectrum_key=no_pdf)
    _refused(svc, "invalid_argument", "000001_01", full_id="000001_01_001")
    _refused(svc, "invalid_argument", "000001_01", name="ok.pdf", offset=len(PDF) + 1)
    _refused(svc, "invalid_argument", "000001_01", name="ok.pdf", offset=-1)
    monkeypatch.setattr(files, "MAX_PDF_BYTES", 10)
    assert "too large" in _refused(svc, "file_unavailable", "000001_01", name="ok.pdf")
    monkeypatch.undo()
    assert base64.b64decode(svc.read_pdf("000001_01", name="ok.pdf")["data"]) == PDF


# --------------------------------------------------------------------------- moved files folder

#: A path as the add-on recorded it on another PC (or before the folder was moved).
OLD_FOLDER = "D:\\old place\\compounds\\000001_01"


def test_resolve_recorded(tmp_path):
    files_dir = tmp_path / "compounds"
    folder = files_dir / "000001_01"
    folder.mkdir(parents=True)
    (folder / "a.pdf").write_bytes(PDF)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.pdf").write_bytes(b"%PDF-other")
    (elsewhere / "b.pdf").write_bytes(PDF)

    def where(stored, cs="000001_01"):
        return files.resolve_recorded(files_dir, cs, stored)

    # The recorded place wins while the file is there, also outside the folder.
    assert where(str(elsewhere / "a.pdf")) == elsewhere / "a.pdf"
    assert where("  " + str(folder / "a.pdf") + " ") == folder / "a.pdf"
    # Gone from the recorded place: the file of that name in the compound's folder.
    assert where(OLD_FOLDER + "\\a.pdf") == folder / "a.pdf"
    assert where("/mnt/old/compounds/000001_01/a.pdf") == folder / "a.pdf"
    assert where(str(tmp_path / "nowhere" / "a.pdf")) == folder / "a.pdf"
    assert where("a.pdf") == folder / "a.pdf"
    assert where("sub\\a.pdf") == folder / "a.pdf"
    # In neither place: the recorded full path, or the place in the folder for a bare name.
    gone = tmp_path / "nowhere" / "c.pdf"
    assert where(str(gone)) == gone
    assert where("c.pdf") == folder / "c.pdf"
    # A file of that name in any other folder is never taken.
    assert where(str(tmp_path / "nowhere" / "b.pdf")) == tmp_path / "nowhere" / "b.pdf"
    # Nothing recorded, or no place to look.
    for nothing in (None, "", "   ", 5, ["a.pdf"]):
        assert where(nothing) is None
    assert where("a.pdf", cs="EX#0001_01") is None
    assert where("..") is None and where("sub\\..") is None


def test_resolve_recorded_does_not_follow_a_link_out_of_the_folder(tmp_path, monkeypatch):
    files_dir = tmp_path / "compounds"
    folder = files_dir / "000001_01"
    folder.mkdir(parents=True)
    (folder / "link.pdf").write_bytes(PDF)
    real_realpath = os.path.realpath

    def realpath(path, *args, **kwargs):
        if str(path).endswith("link.pdf"):
            return str(tmp_path / "secret.pdf")
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(files.os.path, "realpath", realpath)
    found = files.resolve_recorded(files_dir, "000001_01", OLD_FOLDER + "\\link.pdf")
    # Not the link in the folder: the recorded path (where it is a full path), which is gone.
    assert found is None or (found == Path(OLD_FOLDER + "\\link.pdf") and not found.exists())
    assert files.resolve_recorded(files_dir, "000001_01", "link.pdf") is None


def test_spectra_recorded_before_the_files_folder_moved_are_still_found(svc, home, capsys):
    reg(svc)
    folder = home / "compounds" / "000001_01"
    (folder / "000001_01_001_S1.pdf").write_bytes(PDF)
    (folder / "000001_01_001_S1.mnova").write_bytes(b"mnova")
    svc.record_spectra("000001_01_001", [
        spectrum(folder, "S1", mnova_file=OLD_FOLDER + "\\000001_01_001_S1.mnova",
                 pdf_file=OLD_FOLDER + "\\000001_01_001_S1.pdf"),
        spectrum(folder, "S2", title="user.012.1.fid", mnova_file=None,
                 pdf_file=OLD_FOLDER + "\\000001_01_001_S2.pdf"),
    ])
    result = svc.compound_files("000001_01")
    first, second = result["spectra"]
    assert first["pdf_file"] == OLD_FOLDER + "\\000001_01_001_S1.pdf"     # as recorded
    assert first["pdf_available"] is True and second["pdf_available"] is False
    by_name = {f["name"]: f for f in result["files"]}
    assert by_name["000001_01_001_S1.pdf"]["recorded"] is True
    assert by_name["000001_01_001_S1.mnova"]["recorded"] is True
    assert by_name["000001_01.mol"]["recorded"] is False

    piece = svc.read_pdf("000001_01", full_id="000001_01_001", spectrum_key=first["spectrum_key"])
    assert base64.b64decode(piece["data"]) == PDF and piece["name"] == "000001_01_001_S1.pdf"
    message = _refused(svc, "file_unavailable", "000001_01", full_id="000001_01_001",
                       spectrum_key=second["spectrum_key"])
    assert "not there any more" in message

    assert svc.recorded_file("000001_01_001", first["pdf_file"]) == str(
        folder / "000001_01_001_S1.pdf")
    assert svc.recorded_file("000001_01_001", second["pdf_file"]) is None
    assert svc.recorded_file("000001_01_001", None) is None
    assert svc.recorded_file("000009_01_001", first["pdf_file"]) is None

    capsys.readouterr()
    assert main(["--home", str(home), "spectra", "000001_01_001"]) == 0
    printed = capsys.readouterr().out
    assert f"mnova: {folder / '000001_01_001_S1.mnova'}" in printed
    assert f"pdf: {folder / '000001_01_001_S1.pdf'}" in printed
    assert "000001_01_001_S2.pdf (not found)" in printed


def test_a_recorded_file_that_is_not_a_pdf_is_not_served_from_the_folder(svc, home):
    reg(svc)
    folder = home / "compounds" / "000001_01"
    (folder / "notes.txt").write_bytes(PDF)
    (folder / "fake.pdf").write_bytes(b"not a pdf")
    svc.record_spectra("000001_01_001", [
        spectrum(folder, "S1", title="a.fid", pdf_file=OLD_FOLDER + "\\notes.txt"),
        spectrum(folder, "S2", title="b.fid", pdf_file=OLD_FOLDER + "\\fake.pdf"),
    ])
    for item in svc.compound_files("000001_01")["spectra"]:
        assert item["pdf_available"] is False
        _refused(svc, "file_unavailable", "000001_01", full_id="000001_01_001",
                 spectrum_key=item["spectrum_key"])


# --------------------------------------------------------------------------- make_folders

def test_make_folders_for_every_variant(svc, home):
    reg(svc)
    reg(svc, smiles=AMINE_S, ref="E2")
    reg(svc, smiles=ETHANOL, ref="E3")
    shutil.rmtree(home / "compounds" / "000001_02")
    result = svc.make_folders()
    assert result["files_folder"] == str(home / "compounds")
    assert [(e["core_stereo_id"], e["created"], e["warning"]) for e in result["folders"]] == [
        ("000001_01", False, None), ("000001_02", True, None), ("000002_01", False, None)]
    assert result["created"] == 1 and result["failed"] == 0
    assert (home / "compounds" / "000001_02" / "000001_02.mol").is_file()
    one = svc.make_folders("000002_01")
    assert one["folders"] == [{"core_stereo_id": "000002_01",
                               "path": str(home / "compounds" / "000002_01"),
                               "created": False, "warning": None}]
    with pytest.raises(RegistryError) as info:
        svc.make_folders("000009_01")
    assert code_of(info) == "not_found"


def test_doctor_reports_the_files_folder(svc, home):
    assert svc.doctor()["files_folder"] == str(home / "compounds")


# =========================================================================== host

def ask(home: Path, op: str, params: dict, request_id: int = 1) -> dict:
    outcome = host.handle_message(
        json.dumps({"id": request_id, "v": 1, "op": op, "params": params}).encode("utf-8"),
        HomeChoice(home, "host-config"))
    return json.loads(host.encode_response(outcome.response, op))


def ok(doc: dict) -> dict:
    assert doc["ok"] is True, doc
    return doc["result"]


def error(doc: dict, code: str) -> dict:
    assert doc["ok"] is False and doc["error"]["code"] == code, doc
    return doc["error"]


@pytest.fixture
def registered(home) -> Path:
    with RegistryService.open(home) as s:
        reg(s)
    return home


def test_host_register_reports_the_folder(registered):
    home = registered
    structure = ok(ask(home, "analyze", {"smiles": AMINE_S}))["structure"]
    confirm = {key: structure[key] for key in protocol.CONFIRM_KEYS}
    result = ok(ask(home, "register", {"smiles": AMINE_S, "experiment_ref": "E9",
                                       "product_index": 1, "confirm": confirm}))
    assert result["folder"] == {"path": str(home / "compounds" / "000001_02"), "created": True}


def test_host_ping_reports_the_files_folder(registered):
    assert ok(ask(registered, "ping", {}))["files_folder"] == str(registered / "compounds")


def test_host_compound_files_and_read_pdf(registered):
    home = registered
    folder = home / "compounds" / "000001_01"
    content = PDF + os.urandom(protocol.PDF_CHUNK_BYTES + 10)
    (folder / "000001_01_001_S1.pdf").write_bytes(content)
    with RegistryService.open(home) as s:
        s.record_spectra("000001_01_001", [spectrum(folder, "S1")])
    listing = ok(ask(home, "compound_files", {"core_stereo_id": "000001_01"}))
    with RegistryService.open(home) as s:
        assert listing == json.loads(json.dumps(s.compound_files("000001_01")))   # verbatim
    (item,) = listing["spectra"]
    assert item["pdf_available"] is True

    def by_host(**params):
        return ok(ask(home, "read_pdf", {"core_stereo_id": "000001_01", **params}))

    got, pieces = read_all(by_host, name="000001_01_001_S1.pdf")
    assert got == content and pieces == 2
    got, _ = read_all(by_host, full_id="000001_01_001", spectrum_key=item["spectrum_key"])
    assert got == content


def test_host_refusals(registered):
    home = registered
    error(ask(home, "compound_files", {"core_stereo_id": "000001_05"}), "not_found")
    error(ask(home, "read_pdf", {"core_stereo_id": "000001_05", "name": "x.pdf"}), "not_found")
    error(ask(home, "read_pdf", {"core_stereo_id": "000001_01", "name": "x.pdf"}),
          "file_unavailable")
    error(ask(home, "read_pdf", {"core_stereo_id": "000001_01", "name": "../x.pdf"}),
          "invalid_argument")
    error(ask(home, "read_pdf", {"core_stereo_id": "000001_01"}), "bad_request")
    error(ask(home, "read_pdf", {"core_stereo_id": "000001_01", "name": "x.pdf",
                                 "path": "C:/x.pdf"}), "bad_request")
    error(ask(home, "read_pdf", {"core_stereo_id": "000001_01", "full_id": "000001_01_001",
                                 "spectrum_key": "nope"}), "not_found")


# =========================================================================== CLI

def test_cli_folders(registered, capsys):
    home = registered
    with RegistryService.open(home) as s:
        reg(s, smiles=AMINE_S, ref="E2")
    shutil.rmtree(home / "compounds" / "000001_01")
    assert main(["--home", str(home), "folders"]) == 0
    out = capsys.readouterr().out
    assert f"Compound folders in {home / 'compounds'}" in out
    assert f"000001_01  created  {home / 'compounds' / '000001_01'}" in out
    assert f"000001_02  already there  {home / 'compounds' / '000001_02'}" in out
    assert (home / "compounds" / "000001_01" / "000001_01.mol").is_file()

    assert main(["--home", str(home), "folders", "000001_02"]) == 0
    assert capsys.readouterr().out.strip() == str(home / "compounds" / "000001_02")

    assert main(["--home", str(home), "folders", "000001_02", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["folders"][0]["path"] == str(home / "compounds" / "000001_02")

    assert main(["--home", str(home), "--json", "folders", "000009_01"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"


def test_cli_folders_reports_failures(odd_home, capsys):
    with RegistryService.open(odd_home) as s:
        reg(s)
    assert main(["--home", str(odd_home), "folders"]) == 1
    out = capsys.readouterr().out
    assert "EX#0001_01  NOT MADE  -" in out and "folder name" in out
    assert main(["--home", str(odd_home), "folders", "EX#0001_01"]) == 1
    assert "folder name" in capsys.readouterr().err


def test_cli_doctor_shows_the_compound_folders(registered, capsys):
    assert main(["--home", str(registered), "doctor"]) in (0, 1)
    assert f"Compound folders {registered / 'compounds'}" in capsys.readouterr().out
