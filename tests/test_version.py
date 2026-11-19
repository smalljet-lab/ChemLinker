"""One version number for the program and the extension (scripts/set-version.py)."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest

import compound_registry

ROOT = Path(__file__).resolve().parent.parent
FILES = ("src/compound_registry/__init__.py", "extension/package.json",
         "extension/package-lock.json", "extension/static/manifest.json")


def _load_script():
    spec = importlib.util.spec_from_file_location("set_version", ROOT / "scripts" / "set-version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


set_version = _load_script()


@pytest.fixture
def project(tmp_path) -> Path:
    """A copy of the four files that hold the version."""
    for name in FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, tmp_path / name)
    return tmp_path


def test_the_version_is_the_same_everywhere():
    same, found = set_version.check(ROOT)
    assert same, f"run 'python scripts/set-version.py X.Y.Z': {found}"
    assert set(found.values()) == {compound_registry.__version__}
    assert len(found) == 5
    assert re.fullmatch(r"\d+\.\d+\.\d+", compound_registry.__version__)


def test_pyproject_reads_the_version_from_the_package():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" not in data["project"] and data["project"]["dynamic"] == ["version"]
    assert data["tool"]["hatch"]["version"]["path"] == "src/compound_registry/__init__.py"
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (own,) = [p for p in lock["package"] if p["name"] == "compound-registry"]
    assert "version" not in own                       # nothing to forget at a release


def test_set_version_writes_every_file_and_nothing_else(project):
    before = {name: (project / name).read_bytes() for name in FILES}
    old = compound_registry.__version__.encode()
    changed = set_version.set_version(project, "12.34.56")
    assert sorted(changed) == sorted(FILES)
    assert set_version.check(project) == (True, dict.fromkeys(set_version.read_versions(project),
                                                              "12.34.56"))
    for name in FILES:
        after = (project / name).read_bytes()
        expected = 2 if name.endswith("package-lock.json") else 1
        assert after.count(b"12.34.56") == expected
        # Only the version changed: line ends, indentation, dependency versions.
        assert after.replace(b"12.34.56", old, expected) == before[name]
    lock = json.loads((project / "extension/package-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == lock["packages"][""]["version"] == "12.34.56"
    assert set_version.set_version(project, "12.34.56") == []


@pytest.mark.parametrize("bad", ["1.2", "1.2.3.4", "v1.2.3", "1.2.3-rc1", "01.2.3", ""])
def test_set_version_refuses_other_forms(project, bad):
    before = {name: (project / name).read_bytes() for name in FILES}
    with pytest.raises(set_version.VersionError):
        set_version.set_version(project, bad)
    assert {name: (project / name).read_bytes() for name in FILES} == before


def test_nothing_is_written_when_a_file_cannot_be_updated(project):
    (project / "extension/static/manifest.json").write_text('{"name": "x"}', encoding="utf-8")
    before = {name: (project / name).read_bytes() for name in FILES}
    with pytest.raises(set_version.VersionError):
        set_version.set_version(project, "2.0.0")
    assert {name: (project / name).read_bytes() for name in FILES} == before


def test_command_line(project, capsys):
    assert set_version.main(["--root", str(project), "--check"]) == 0
    assert compound_registry.__version__ in capsys.readouterr().out
    assert set_version.main(["--root", str(project), "3.0.1"]) == 0
    assert "extension/package.json" in capsys.readouterr().out
    assert set_version.main(["--root", str(project), "--check"]) == 0
    capsys.readouterr()

    manifest = project / "extension/static/manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("3.0.1", "3.0.0"),
                        encoding="utf-8")
    assert set_version.main(["--root", str(project), "--check"]) == 1
    captured = capsys.readouterr()
    assert "3.0.0" in captured.out and "not the same everywhere" in captured.err
    assert set_version.main(["--root", str(project), "nonsense"]) == 1
    assert "X.Y.Z" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        set_version.main(["--root", str(project)])
    with pytest.raises(SystemExit):
        set_version.main(["--root", str(project), "--check", "1.2.3"])
