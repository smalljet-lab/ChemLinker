"""Tests for compound_registry.journal (file format, tolerant reading, appending)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compound_registry.config import Config
from compound_registry.journal import (
    JOURNAL_FILENAME,
    REGISTER_NORM_FIELDS,
    Journal,
    append_entries,
    journal_paths,
    merge,
    normalized_from_entry,
    read_journal,
    register_entry,
    retire_entry,
)
from compound_registry.models import Normalized

NORM = Normalized(
    core_key="InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", stereo_key="[]", stereo_mode="none",
    inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N", smiles="CCO", inchi="InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3",
    core_inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N", formula="C2H6O", detected_mode="none",
    mode_overridden=False, molblock="\n  RDKit\n\nM  END\n", input_format="smiles",
    warnings=("a warning that is not journalled",), ignored_stereo=("x",),
    rdkit_version="2026.09.2", inchi_version="1.07",
)


def reg(full_id: str, at: str = "2026-11-17T10:00:00Z", ref: str = "E1") -> dict:
    return register_entry(full_id, at, experiment_ref=ref, experiment_code="ABC-1",
                          experiment_url=None, product_index=1, source="cli", norm=NORM,
                          accepted_ignored_stereo=["x"])


def test_journal_paths(tmp_path):
    cfg = Config(home=tmp_path / "h", config_path=tmp_path / "h" / "config.toml",
                 db_path=tmp_path / "h" / "r.sqlite", backup_dir=tmp_path / "bk")
    assert journal_paths(cfg) == (tmp_path / "h" / JOURNAL_FILENAME, tmp_path / "bk" / JOURNAL_FILENAME)
    j = Journal.for_config(cfg)
    assert j.files() == [tmp_path / "h" / JOURNAL_FILENAME, tmp_path / "bk" / JOURNAL_FILENAME]
    same = Journal.for_config(Config(home=tmp_path, config_path=tmp_path / "c.toml",
                                     db_path=tmp_path / "r.sqlite", backup_dir=tmp_path))
    assert same.mirror_path is None  # never write the same file twice


def test_register_entry_holds_everything_and_round_trips():
    e = reg("000001_01_001")
    assert e["event"] == "register" and e["id"] == "000001_01_001"
    assert e["at"] == "2026-11-17T10:00:00Z"
    for name in ("experiment_ref", "experiment_code", "experiment_url", "product_index", "source"):
        assert name in e
    for name in REGISTER_NORM_FIELDS:
        assert e[name] == getattr(NORM, name)
    assert e["accepted_ignored_stereo"] == ["x"]
    rebuilt = normalized_from_entry(json.loads(json.dumps(e)))
    for name in REGISTER_NORM_FIELDS:
        assert getattr(rebuilt, name) == getattr(NORM, name)
    with pytest.raises(KeyError):
        normalized_from_entry({k: v for k, v in e.items() if k != "core_key"})
    with pytest.raises(TypeError):
        normalized_from_entry(dict(e, mode_overridden="no"))


def test_append_and_read(tmp_path):
    path = tmp_path / "sub" / JOURNAL_FILENAME
    assert read_journal(path).entries == [] and not read_journal(path).exists
    append_entries(path, [reg("000001_01_001")])
    append_entries(path, [reg("000002_01_001"), retire_entry("000001_01_001", "2026-11-17T11:00:00Z", "bad")])
    append_entries(path, [])  # nothing to do
    text = path.read_bytes()
    assert text.endswith(b"\n") and text.count(b"\n") == 3
    assert all(json.loads(line) for line in text.splitlines())
    jf = read_journal(path)
    assert [(e["event"], e["id"]) for e in jf.entries] == [
        ("register", "000001_01_001"), ("register", "000002_01_001"), ("retire", "000001_01_001")]
    assert jf.unreadable_lines == 0 and jf.exists and jf.error is None


def test_truncated_last_line_is_tolerated_and_repaired_on_append(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    append_entries(path, [reg("000001_01_001")])
    cut = json.dumps(reg("000002_01_001"))[:40]  # a write cut short by a crash
    with open(path, "ab") as fh:
        fh.write(cut.encode())
    jf = read_journal(path)
    assert [e["id"] for e in jf.entries] == ["000001_01_001"] and jf.unreadable_lines == 1
    append_entries(path, [reg("000003_01_001")])  # starts on a fresh line
    jf = read_journal(path)
    assert [e["id"] for e in jf.entries] == ["000001_01_001", "000003_01_001"]
    assert jf.unreadable_lines == 1


def test_duplicates_and_junk_lines(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    append_entries(path, [reg("000001_01_001"), reg("000001_01_001"), reg("000002_01_001")])
    with open(path, "ab") as fh:
        fh.write(b"\n\n[1, 2]\n{\"event\": \"other\", \"id\": \"x\", \"at\": \"y\"}\n\xff\xfe\n")
    append_entries(path, [reg("000001_01_001")])
    jf = read_journal(path)
    assert [e["id"] for e in jf.entries] == ["000001_01_001", "000002_01_001"]
    assert jf.unreadable_lines == 3


def test_merge_keeps_main_order_then_mirror_only(tmp_path):
    main, mirror = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    append_entries(main, [reg("1"), reg("3")])
    append_entries(mirror, [reg("1"), reg("2"), reg("3"), reg("4")])
    merged = merge([read_journal(main), read_journal(mirror)])
    assert [e["id"] for e in merged] == ["1", "3", "2", "4"]


def test_journal_append_reports_failures(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    ok_path = tmp_path / "home" / JOURNAL_FILENAME
    j = Journal(ok_path, blocker / "bk" / JOURNAL_FILENAME)
    outcome = j.append([reg("1")])
    assert outcome.main_ok is True
    assert len(outcome.warnings) == 1 and "mirror" in outcome.warnings[0]
    assert [e["id"] for e in read_journal(ok_path).entries] == ["1"]

    j = Journal(blocker / "x" / JOURNAL_FILENAME, tmp_path / "mirror" / JOURNAL_FILENAME)
    outcome = j.append([reg("1")])
    assert outcome.main_ok is False
    assert "issued-ID journal" in outcome.warnings[0] and "1" in outcome.warnings[0]
    assert [e["id"] for e in read_journal(tmp_path / "mirror" / JOURNAL_FILENAME).entries] == ["1"]


def test_unreadable_journal_is_reported(tmp_path):
    folder = tmp_path / JOURNAL_FILENAME
    folder.mkdir()  # a folder where the file should be
    jf = read_journal(folder)
    assert jf.exists and jf.error and jf.entries == []


def test_lines_are_ascii_json(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    append_entries(path, [register_entry("1", "2026-11-17T10:00:00Z", experiment_ref="José",
                                         experiment_code=None, experiment_url=None,
                                         product_index=1, source="cli", norm=NORM)])
    raw = path.read_bytes()
    raw.decode("ascii")
    assert read_journal(path).entries[0]["experiment_ref"] == "José"
    assert Path(path).read_text(encoding="utf-8").count("\n") == 1
