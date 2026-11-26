"""``compound-registry install-recognition`` (compound_registry.recognition_install).

uv and every download are replaced: nothing is installed, nothing is fetched,
and only temporary folders are written. The "model files" are a few bytes
each, with MODEL_FILES replaced to match.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from compound_registry import recognition, recognition_install
from compound_registry.cli import main
from compound_registry.errors import RegistryError
from compound_registry.recognition_install import ModelFile, Tools

#: The real list, read before any test replaces it.
REAL_MODELS = recognition_install.MODEL_FILES

CONTENT = {
    recognition.RXN_CHECKPOINT: b"layout model " * 50,
    recognition.MOL_CHECKPOINT: b"molecule model " * 80,
    "easyocr/craft_mlt_25k.pth": b"text detector " * 30,
    "easyocr/english_g2.pth": b"text reader " * 20,
}


def zipped(member: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, data)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def small_models(monkeypatch):
    models = tuple(
        ModelFile(m.name, m.url, hashlib.sha256(CONTENT[m.name]).hexdigest(),
                  len(CONTENT[m.name]), m.member)
        for m in recognition_install.MODEL_FILES)
    monkeypatch.setattr(recognition_install, "MODEL_FILES", models)
    return models


class Machine:
    """Fake uv and fake internet."""

    def __init__(self, models, *, uv: str | None = "uv"):
        self.uv = uv
        self.commands: list[list[str]] = []
        self.urls: list[str] = []
        self.lines: list[str] = []
        self.exit_codes: dict[str, int] = {}
        self.announce_size = False
        self.served = {m.url: (zipped(m.member, CONTENT[m.name]) if m.member else CONTENT[m.name])
                       for m in models}
        self.tools = Tools(find_uv=lambda: self.uv, run=self.run, open_url=self.open_url,
                           say=self.lines.append)

    def run(self, command, timeout=None):
        self.commands.append([str(part) for part in command])
        kind = ("venv" if "venv" in command else "pip" if "pip" in command
                else "probe" if command[-1] == "pass" else "check")
        code = self.exit_codes.get(kind, 0)
        if kind == "venv" and code == 0:
            python = recognition.env_python(Path(command[2]).parent)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"python")
            (Path(command[2]) / "pyvenv.cfg").write_text("home = somewhere\n")
        return code

    def open_url(self, url):
        self.urls.append(url)
        data = self.served[url]
        if isinstance(data, Exception):
            raise data
        response = io.BytesIO(data)
        if self.announce_size:
            response.headers = {"Content-Length": str(len(data))}
        return response

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def machine(small_models) -> Machine:
    return Machine(small_models)


def test_the_real_model_list_is_complete_and_pinned():
    real = REAL_MODELS
    assert [m.name for m in real] == [recognition.RXN_CHECKPOINT, recognition.MOL_CHECKPOINT,
                                      *recognition.OCR_FILES]
    for m in real:
        assert m.url.startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", m.sha256)
        assert m.size > 10_000_000
    assert real[0].sha256 == "b0020634f13fb3e1f588bddca97f68fd6483f0cdecd83e2ca31c2434ea4340fe"
    assert real[1].sha256 == "32821fa4ba8d17f9a92e374601438c5e757947dbf1141e9ad0ef51cccfa1c501"
    assert "huggingface.co/yujieq/RxnScribe" in real[0].url
    assert "huggingface.co/yujieq/MolScribe" in real[1].url
    assert all(m.member for m in real[2:]) and not any(m.member for m in real[:2])


def test_the_requirements_are_pinned_exactly():
    lines = [line.strip() for line in
             recognition_install.requirements_path().read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    assert len(lines) == 122
    for line in lines:
        assert "==" in line or " @ https://github.com/" in line, line
    by_name = {re.split(r"==| @ ", line)[0]: line for line in lines}
    assert by_name["torch"] == "torch==2.15.0+cpu"
    assert by_name["rdkit"] == "rdkit==2026.9.2"
    assert by_name["rxnscribe"].endswith(
        "RxnScribe/archive/ad6b1c75d40e563e68deca0491918885948d69c7.zip")
    assert by_name["molscribe"].endswith(
        "MolScribe/archive/7296a30413eb55436702011efdff78131f66d162.zip")
    overrides = [line for line in
                 recognition_install.overrides_path().read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
    assert overrides == [by_name["molscribe"]]
    assert "git+" not in recognition_install.requirements_path().read_text(encoding="utf-8")


def test_the_main_project_gets_no_new_dependency():
    root = Path(__file__).parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert [line.strip().strip('",') for line in block.splitlines() if line.strip()] == [
        "rdkit==2026.9.2"]


def test_install_creates_everything_and_marks_it_last(tmp_path, machine, small_models):
    home = tmp_path / "recognition"
    result = recognition_install.install(home, tools=machine.tools)
    python = str(recognition.env_python(home))
    assert machine.commands[0] == ["uv", "venv", str(home / "env"), "--python", "3.12"]
    assert machine.commands[1] == [
        "uv", "pip", "install", "--python", python,
        "-r", str(recognition_install.requirements_path()),
        "--overrides", str(recognition_install.overrides_path()),
        "--extra-index-url", "https://download.pytorch.org/whl/cpu",
        "--index-strategy", "unsafe-best-match"]
    assert machine.commands[2][:3] == [python, "-I", "-c"] and "import torch" in machine.commands[2][3]
    assert len(machine.commands) == 3
    assert machine.urls == [m.url for m in small_models]
    for model in small_models:
        assert (home / "models" / model.name).read_bytes() == CONTENT[model.name]
    assert not list(home.rglob("*.part")) and not list(home.rglob("*.zip*"))
    assert (home / "models" / "easyocr" / "user_network").is_dir()
    marker = json.loads((home / recognition.MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["models"] == {m.name: m.sha256 for m in small_models}
    assert result["installed"] is True and result["home"] == str(home)
    assert set(result["models"].values()) == {"downloaded"}
    assert recognition.install_state(home)["installed"] is True
    assert "[1/4]" in machine.text and "[4/4]" in machine.text and "is installed" in machine.text


def test_install_again_downloads_nothing(tmp_path, machine):
    home = tmp_path / "recognition"
    recognition_install.install(home, tools=machine.tools)
    machine.commands.clear()
    machine.urls.clear()
    result = recognition_install.install(home, tools=machine.tools)
    assert machine.urls == [] and set(result["models"].values()) == {"present"}
    assert [c[1] for c in machine.commands if c[0] == "uv"] == ["pip"]      # no second venv
    python = str(recognition.env_python(home))
    assert machine.commands[0] == [python, "-I", "-c", "pass"]              # does it still start?
    assert recognition.install_state(home)["installed"] is True


def test_a_wrong_download_is_refused_and_leaves_nothing(tmp_path, machine, small_models):
    home = tmp_path / "recognition"
    machine.served[small_models[1].url] = b"something else"
    with pytest.raises(RegistryError) as info:
        recognition_install.install(home, tools=machine.tools)
    assert info.value.code == "recognition_install_failed" and "SHA-256" in info.value.message
    assert (home / "models" / small_models[0].name).is_file()               # kept
    assert not (home / "models" / small_models[1].name).exists()
    assert not list(home.rglob("*.part"))
    assert recognition.install_state(home)["installed"] is False
    # Repeating it finishes the job without fetching the first file again.
    machine.served[small_models[1].url] = CONTENT[small_models[1].name]
    machine.urls.clear()
    recognition_install.install(home, tools=machine.tools)
    assert machine.urls == [m.url for m in small_models[1:]]
    assert recognition.install_state(home)["installed"] is True


def test_a_failed_download_says_to_try_again(tmp_path, machine, small_models):
    machine.served[small_models[0].url] = OSError("connection reset")
    with pytest.raises(RegistryError) as info:
        recognition_install.install(tmp_path / "r", tools=machine.tools)
    assert "could not be downloaded" in info.value.message
    assert "run the command again" in info.value.message
    assert not list((tmp_path / "r").rglob("*.part"))


def test_a_download_that_is_no_zip_is_refused(tmp_path, machine, small_models):
    machine.served[small_models[2].url] = b"<html>not found</html>"
    with pytest.raises(RegistryError) as info:
        recognition_install.install(tmp_path / "r", tools=machine.tools)
    assert "not the expected zip file" in info.value.message
    assert not list((tmp_path / "r").rglob("*.part"))


def test_models_from_copies_and_checks_instead_of_downloading(tmp_path, machine, small_models):
    source = tmp_path / "downloaded"
    for name, data in CONTENT.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_bytes(data)
    home = tmp_path / "recognition"
    result = recognition_install.install(home, models_from=source, tools=machine.tools)
    assert machine.urls == [] and set(result["models"].values()) == {"copied"}
    assert (home / "models" / "easyocr" / "english_g2.pth").read_bytes() == \
        CONTENT["easyocr/english_g2.pth"]

    (source / recognition.MOL_CHECKPOINT).write_bytes(b"damaged")
    with pytest.raises(RegistryError) as info:
        recognition_install.install(tmp_path / "second", models_from=source, tools=machine.tools)
    assert "SHA-256" in info.value.message
    (source / recognition.MOL_CHECKPOINT).unlink()
    with pytest.raises(RegistryError) as info:
        recognition_install.install(tmp_path / "third", models_from=source, tools=machine.tools)
    assert "does not exist" in info.value.message
    with pytest.raises(RegistryError):
        recognition_install.install(tmp_path / "fourth", models_from=tmp_path / "no-folder",
                                    tools=machine.tools)


@pytest.mark.parametrize("kind, words", [("venv", "environment could not be created"),
                                         ("pip", "could not be installed"),
                                         ("check", "do not start")])
def test_a_failing_step_stops_the_installation_unmarked(tmp_path, machine, kind, words):
    home = tmp_path / "recognition"
    machine.exit_codes[kind] = 2
    with pytest.raises(RegistryError) as info:
        recognition_install.install(home, tools=machine.tools)
    assert words in info.value.message
    assert not (home / recognition.MARKER_NAME).exists()
    assert recognition.install_state(home)["installed"] is False


def test_a_failed_run_on_a_complete_installation_leaves_it_installed(tmp_path, machine,
                                                                    small_models):
    """The host's own messages say "run install-recognition once more". When
    that run fails for an outside reason (no internet for uv, interrupted),
    a recognition that worked must not be switched off."""
    home = tmp_path / "recognition"
    recognition_install.install(home, tools=machine.tools)
    before = (home / recognition.MARKER_NAME).read_text(encoding="utf-8")
    machine.exit_codes["pip"] = 1
    with pytest.raises(RegistryError):
        recognition_install.install(home, tools=machine.tools)
    assert recognition.install_state(home)["installed"] is True
    assert (home / recognition.MARKER_NAME).read_text(encoding="utf-8") == before
    # Too slow a start says nothing about the installation either.
    machine.exit_codes = {"check": 124}
    with pytest.raises(RegistryError) as info:
        recognition_install.install(home, tools=machine.tools)
    assert "took longer" in info.value.message
    assert recognition.install_state(home)["installed"] is True


def test_a_run_that_has_to_change_something_unmarks_the_installation_first(tmp_path, machine,
                                                                          small_models):
    home = tmp_path / "recognition"
    recognition_install.install(home, tools=machine.tools)
    marker = home / recognition.MARKER_NAME

    # A model file is damaged: from the moment it is replaced, "not installed".
    (home / "models" / small_models[0].name).write_bytes(b"damaged")
    machine.served[small_models[0].url] = OSError("no internet")
    with pytest.raises(RegistryError):
        recognition_install.install(home, tools=machine.tools)
    assert not marker.exists()
    machine.served[small_models[0].url] = CONTENT[small_models[0].name]
    recognition_install.install(home, tools=machine.tools)
    assert recognition.install_state(home)["installed"] is True

    # The programs do not start: demonstrably broken.
    machine.exit_codes = {"check": 1}
    with pytest.raises(RegistryError) as info:
        recognition_install.install(home, tools=machine.tools)
    assert "do not start" in info.value.message and not marker.exists()
    machine.exit_codes = {}
    recognition_install.install(home, tools=machine.tools)

    # Installed for other pins (an older ChemLinker): unmarked before the programs change.
    record = json.loads(marker.read_text(encoding="utf-8"))
    marker.write_text(json.dumps({**record, "requirements_sha256": "0" * 64}), encoding="utf-8")
    machine.exit_codes = {"pip": 1}
    with pytest.raises(RegistryError):
        recognition_install.install(home, tools=machine.tools)
    assert not marker.exists()
    machine.exit_codes = {}
    recognition_install.install(home, tools=machine.tools)
    assert recognition.install_state(home)["installed"] is True


def test_a_leftover_environment_folder_is_made_again(tmp_path, machine):
    home = tmp_path / "recognition"
    (home / "env" / "half").mkdir(parents=True)
    (home / "env" / "pyvenv.cfg").write_text("home = x\n")       # a Python environment, unfinished
    recognition_install.install(home, tools=machine.tools)
    assert machine.commands[0][:2] == ["uv", "venv"] and not (home / "env" / "half").exists()
    (tmp_path / "second" / "env").mkdir(parents=True)             # an empty folder is fine too
    recognition_install.install(tmp_path / "second", tools=machine.tools)
    assert recognition.install_state(tmp_path / "second")["installed"] is True


def test_an_environment_whose_python_no_longer_starts_is_made_again(tmp_path, machine):
    """uv builds the environment on a Python inside the ChemLinker folder.
    When that folder is moved, env/Scripts/python.exe is still a file but
    does not start; "already there" would be a dead end."""
    home = tmp_path / "recognition"
    recognition_install.install(home, tools=machine.tools)
    (home / "env" / "old-package").mkdir()
    machine.commands.clear()
    machine.urls.clear()
    machine.exit_codes["probe"] = 103
    result = recognition_install.install(home, tools=machine.tools)
    assert [c[:2] for c in machine.commands[:3]] == [
        [str(recognition.env_python(home)), "-I"], ["uv", "venv"], ["uv", "pip"]]
    assert not (home / "env" / "old-package").exists()
    assert "does not start any more" in machine.text
    assert machine.urls == [] and set(result["models"].values()) == {"present"}   # models kept
    assert recognition.install_state(home)["installed"] is True


def test_install_does_not_delete_an_env_folder_it_did_not_make(tmp_path, machine):
    """--home can name any folder, and "env" is a common name."""
    home = tmp_path / "my project"
    (home / "env").mkdir(parents=True)
    (home / "env" / "production.env").write_text("SECRET=1")
    with pytest.raises(RegistryError) as info:
        recognition_install.install(home, tools=machine.tools)
    assert "not a Python environment made by this command" in info.value.message
    assert (home / "env" / "production.env").read_text() == "SECRET=1"
    assert not any("venv" in command for command in machine.commands)
    assert not (home / "models").exists()


def test_uninstall_does_not_delete_folders_it_did_not_make(tmp_path, machine, small_models):
    """A folder that merely has an env/ and a models/ of its own (the names
    are common): nothing in it is removed."""
    home = tmp_path / "my project"
    (home / "models" / "easyocr").mkdir(parents=True)
    (home / "models" / "my_own_trained_model.bin").write_bytes(b"weeks of work")
    # Even files with the names of the recognition models stay: nothing shows
    # that this command put them here.
    (home / "models" / small_models[0].name).write_bytes(CONTENT[small_models[0].name])
    (home / "env").mkdir()
    (home / "env" / "notes.txt").write_text("mine")
    (home / "keep.txt").write_text("mine")
    result = recognition_install.uninstall(home, tools=machine.tools)
    assert result["removed"] == []
    assert sorted(Path(p).name for p in result["kept"]) == ["env", "models"]
    assert sorted(str(p.relative_to(home)).replace("\\", "/") for p in home.rglob("*")
                  if p.is_file()) == [
        "env/notes.txt", "keep.txt", "models/my_own_trained_model.bin",
        "models/" + small_models[0].name]
    assert "Left alone" in machine.text and "Nothing to remove" in machine.text


def test_uninstall_in_a_models_folder_that_was_there_before(tmp_path, machine, small_models):
    """Installed into a folder that already had a models/ folder: only the
    model files and the worker's caches go, the user's own file stays."""
    home = tmp_path / "shared"
    (home / "models").mkdir(parents=True)
    (home / "models" / "my_own_trained_model.bin").write_bytes(b"mine")
    recognition_install.install(home, tools=machine.tools)
    assert not (home / "models" / recognition_install.OWNER_TAG).exists()
    (home / "models" / "mpl").mkdir()                               # what the worker writes
    (home / "models" / "mpl" / "fontlist.json").write_text("{}")
    result = recognition_install.uninstall(home, tools=machine.tools)
    assert [Path(p).name for p in result["removed"]] == ["env"]
    assert [Path(p).name for p in result["kept"]] == ["models"]
    assert [str(p.relative_to(home)).replace("\\", "/") for p in home.rglob("*")] == [
        "models", "models/my_own_trained_model.bin"]

    # Without a user file in it, the folder goes as well.
    (home / "models" / "my_own_trained_model.bin").unlink()
    recognition_install.install(home, tools=machine.tools)
    result = recognition_install.uninstall(home, tools=machine.tools)
    assert sorted(Path(p).name for p in result["removed"]) == ["env", "models", "shared"]
    assert not home.exists()


def test_an_interrupted_installation_can_be_removed(tmp_path, machine, small_models):
    home = tmp_path / "recognition"
    machine.served[small_models[1].url] = OSError("connection reset")
    with pytest.raises(RegistryError):
        recognition_install.install(home, tools=machine.tools)
    assert (home / "models" / recognition_install.OWNER_TAG).is_file()
    result = recognition_install.uninstall(home, tools=machine.tools)
    assert not home.exists() and result["kept"] == []


def test_the_progress_of_a_zip_download_reaches_100_percent(tmp_path, machine, small_models):
    """A zip is smaller than the model file in it: counted against the model
    size the progress would end at "90 %"."""
    machine.announce_size = True
    recognition_install.install(tmp_path / "r", tools=machine.tools)
    for model in small_models:
        start = next(i for i, line in enumerate(machine.lines)
                     if line.strip().startswith(model.name + ": downloading"))
        shown = []
        for line in machine.lines[start + 1:]:
            if not line.startswith("      "):
                break
            shown.append(line)
        assert shown and shown[-1].rstrip().endswith("(100 %)"), (model.name, shown)
    machine.lines.clear()
    machine.announce_size = False                                   # no size announced: still 100 %
    recognition_install.install(tmp_path / "second", tools=machine.tools)
    assert sum(line.rstrip().endswith("(100 %)") for line in machine.lines) == len(small_models)


def test_without_uv_nothing_is_touched(tmp_path, small_models):
    machine = Machine(small_models, uv=None)
    with pytest.raises(RegistryError) as info:
        recognition_install.install(tmp_path / "r", tools=machine.tools)
    assert "uv" in info.value.message and not (tmp_path / "r").exists()
    assert machine.commands == [] and machine.urls == []


def test_uninstall_removes_only_what_was_installed(tmp_path, machine):
    home = tmp_path / "recognition"
    recognition_install.install(home, tools=machine.tools)
    (home / "my-notes.txt").write_text("keep me")
    result = recognition_install.uninstall(home, tools=machine.tools)
    assert sorted(Path(p).name for p in result["removed"]) == ["env", "models"]
    assert [p.name for p in home.iterdir()] == ["my-notes.txt"]
    assert recognition.install_state(home)["installed"] is False
    (home / "my-notes.txt").unlink()
    recognition_install.install(home, tools=machine.tools)
    assert str(home) in recognition_install.uninstall(home, tools=machine.tools)["removed"]
    assert not home.exists()
    assert recognition_install.uninstall(home, tools=machine.tools)["removed"] == []


# --------------------------------------------------------------------------- the command

def test_the_command_installs_into_the_given_folder(tmp_path, machine, monkeypatch, capsys):
    monkeypatch.setattr(recognition_install, "Tools",
                        lambda say: Tools(find_uv=machine.tools.find_uv, run=machine.run,
                                          open_url=machine.open_url, say=say))
    home = tmp_path / "recognition"
    assert main(["install-recognition", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "[3/4]" in out and "is installed" in out
    assert recognition.install_state(home)["installed"] is True

    assert main(["install-recognition", "--home", str(home), "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["installed"] is True and "[1/4]" in captured.err

    assert main(["install-recognition", "--home", str(home), "--uninstall"]) == 0
    assert "was removed" in capsys.readouterr().out and not home.exists()
    assert main(["install-recognition", "--uninstall", "--models-from", "x"]) == 2


def test_the_command_reports_a_failure(tmp_path, small_models, monkeypatch, capsys):
    machine = Machine(small_models, uv=None)
    monkeypatch.setattr(recognition_install, "Tools", lambda say: machine.tools)
    assert main(["install-recognition", "--home", str(tmp_path / "r")]) == 1
    assert "error [recognition_install_failed]" in capsys.readouterr().err


def test_the_default_folder_follows_the_environment_variable(tmp_path, machine, monkeypatch,
                                                             capsys):
    monkeypatch.setenv(recognition.HOME_ENV_VAR, str(tmp_path / "from-env"))
    monkeypatch.setattr(recognition_install, "Tools", lambda say: machine.tools)
    assert main(["install-recognition"]) == 0
    assert recognition.install_state()["home"] == str(tmp_path / "from-env")
    assert recognition.install_state()["installed"] is True


def test_doctor_says_whether_recognition_is_installed(tmp_path, monkeypatch, capsys):
    from compound_registry.service import RegistryService

    home = tmp_path / "registry"
    RegistryService.open(home).close()
    monkeypatch.setenv(recognition.HOME_ENV_VAR, str(tmp_path / "recognition"))
    assert main(["--home", str(home), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "ChemResolver  not installed" in out and "install-recognition" in out
    assert main(["--home", str(home), "--json", "doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["recognition"] == {
        "installed": False, "home": str(tmp_path / "recognition"),
        "problem": "its Python environment is missing"}

    python = recognition.env_python(tmp_path / "recognition")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    assert main(["--home", str(home), "doctor"]) == 0
    assert "ChemResolver  NOT complete" in capsys.readouterr().out
    for path in recognition.model_files(tmp_path / "recognition"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"m")
    (tmp_path / "recognition" / recognition.MARKER_NAME).write_text("{}")
    assert main(["--home", str(home), "doctor"]) == 0
    assert "NOT complete" in capsys.readouterr().out               # a record that says nothing
    recognition_install._write_marker(tmp_path / "recognition")
    assert main(["--home", str(home), "doctor"]) == 0
    assert f"ChemResolver  installed in {tmp_path / 'recognition'}" in capsys.readouterr().out


def test_the_setup_closing_text_only_mentions_the_optional_part():
    from compound_registry import setup_wizard

    assert "compound-registry install-recognition" in setup_wizard._RECOGNITION_HINT
    assert "Optional" in setup_wizard._RECOGNITION_HINT
    # The wizard asks before it installs the part (tests/test_setup_wizard.py);
    # the closing text names it and its size for those who said no.
    assert setup_wizard.RECOGNISER_NAME in setup_wizard._RECOGNITION_HINT
    assert setup_wizard.RECOGNISER_SIZE in setup_wizard._RECOGNITION_HINT
