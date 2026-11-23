"""MestReNova bridge: experiment numbers in NMR titles, proposals, installer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from compound_registry import mnova
from compound_registry.service import RegistryService

AMINE = "C[C@H](N)c1ccccc1"
AMIDE = "CC(=O)N[C@@H](C)c1ccccc1"
ETHANOL = "CCO"

#: What the installed add-on sends with every call.
ADDON = ["--addon-version", mnova.__version__]

#: The three title formats of the user's spectrometers (dataset name, then the comment).
TITLES = {
    "ABC-006-FC2-concentrated": "user.011.1.fid\nNummer ABC-006-FC2-concentrated",
    "ABC-006-tBuLi-overwefreezer": "group_.6893.1.fid\nABC-006-tBuLi-overwefreezer.",
    "ABC-006-FC2-concentrated ": ("gr_group-.7798.1.fid\nGruppe Beispiel\nUser group_nmr500\n"
                                  "Experiment ABC-006-FC2-concentrated\n"
                                  "H1 CDCl3 /opt/nmrdata group_nmr500 50"),
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))   # no host pin


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    with RegistryService.open(path) as svc:
        def reg(smiles, ref, code, index=1):
            return svc.register(smiles=smiles, experiment_ref=ref, experiment_code=code,
                                experiment_url=None, product_index=index, source="cli")["id"]

        reg(AMINE, "1006", "ABC-006", 1)
        reg(AMIDE, "1006", "ABC-006", 2)
        reg(ETHANOL, "1061", "ABC-0061", 1)
        retired = reg(ETHANOL, "1006", "ABC-006", 3)
        svc.retire(retired, "wrong vial")
        svc.set_name("000001_01", "(S)-1-phenylethan-1-amine")
    return path


# --------------------------------------------------------------------------- titles

@pytest.mark.parametrize("sample, title", TITLES.items())
def test_the_three_title_formats(sample, title):
    hits = mnova.find_experiments(title, ["ABC-006", "ABC-008", "ABC-0061"])
    assert [(h["experiment_ref"], h["sample"]) for h in hits] == [("ABC-006", sample.strip())]


@pytest.mark.parametrize("text", ["abc-006 crude", "ABC_006", "ABC 006", "ABC006-F2", "(ABC-006)"])
def test_spelling_variants_match(text):
    assert [h["experiment_ref"] for h in mnova.find_experiments(text, ["ABC-006"])] == ["ABC-006"]


@pytest.mark.parametrize("text", ["ABC-0061", "XABC-006", "ABC-0", "", "ABC-06", "PREABC-006"])
def test_near_misses_do_not_match(text):
    assert mnova.find_experiments(text, ["ABC-006"]) == []


def test_longer_reference_wins_and_each_is_reported_once():
    hits = mnova.find_experiments("ABC-0061-a and ABC-006-b, again ABC-006-c",
                                  ["ABC-006", "ABC-0061"])
    assert [(h["experiment_ref"], h["sample"]) for h in hits] == [
        ("ABC-0061", "ABC-0061-a"), ("ABC-006", "ABC-006-b")]


def test_file_label_is_a_safe_file_name():
    assert mnova.file_label("ABC-006-FC2-concentrated") == "ABC-006-FC2-concentrated"
    assert mnova.file_label('a/b\\c:d*e?"f<g>h|i') == "a_b_c_d_e_f_g_h_i"
    assert mnova.file_label("...") == ""
    assert mnova.file_label("con") == "_con"
    assert len(mnova.file_label("x" * 200)) == 60


# --------------------------------------------------------------------------- propose

def test_propose_lists_the_products_of_the_experiment(home, tmp_path):
    images = tmp_path / "img"
    result = mnova.propose(TITLES["ABC-006-FC2-concentrated"], home=str(home), image_dir=images)
    assert result["known_experiments"] == 2
    (match,) = result["matches"]
    assert match["experiment_ref"] == "ABC-006"
    assert match["file_label"] == "ABC-006-FC2-concentrated"
    assert [(c["full_id"], c["product_index"]) for c in match["candidates"]] == [
        ("000001_01_001", 1), ("000002_01_001", 2)]            # the retired batch is not offered
    first = match["candidates"][0]
    assert first["name"] == "(S)-1-phenylethan-1-amine" and first["formula"] == "C8H11N"
    assert (images / "000001_01_001.png").read_bytes().startswith(b"\x89PNG")
    assert first["image"].endswith("/000001_01_001.png")


def test_propose_without_a_known_number_proposes_nothing(home):
    result = mnova.propose("user.012.1.fid\nNummer XYZ-999", home=str(home))
    assert result["matches"] == []


def test_propose_changes_nothing_and_never_creates_a_registry(home, tmp_path):
    def state():
        with RegistryService.open(home) as svc:
            return svc.doctor()["stats"], [b["full_id"] for b in svc.experiment("1006")]

    journal = (home / "issued-ids.jsonl").read_bytes()
    before = state()
    mnova.propose(TITLES["ABC-006-FC2-concentrated"], home=str(home))
    assert state() == before
    assert (home / "issued-ids.jsonl").read_bytes() == journal
    missing = tmp_path / "nowhere"
    text = tmp_path / "t.txt"
    text.write_text("ABC-006", encoding="utf-8")
    out = tmp_path / "answer.json"
    assert mnova.main(["propose", "--text-file", str(text), "--home", str(missing),
                       "--out", str(out), *ADDON]) == 1
    assert json.loads(out.read_text())["error"]["code"] == "registry_missing"
    assert not missing.exists()


def test_cli_writes_one_ascii_json_document(home, tmp_path, capsys):
    text = tmp_path / "t.txt"
    text.write_text("Nummer ABC-006 Rückstand", encoding="utf-8")
    assert mnova.main(["propose", "--text-file", str(text), "--home", str(home), *ADDON]) == 0
    printed = capsys.readouterr().out
    assert printed.isascii()
    assert json.loads(printed)["result"]["matches"][0]["sample"] == "ABC-006"


# --------------------------------------------------------------------------- installer

def _python(tmp_path) -> Path:
    exe = tmp_path / "py" / "python.exe"
    exe.parent.mkdir(exist_ok=True)
    exe.write_bytes(b"")
    return exe


NO_MNOVA = {"found": False, "version": None, "folder": None, "evidence": None}


def test_install_and_uninstall(tmp_path):
    scripts = tmp_path / "scripts"
    done = mnova.install(scripts_dir=scripts, python=tmp_path / "py" / "python.exe")
    assert sorted(p.name for p in scripts.iterdir()) == sorted(mnova.ADDON_FILES)
    script = (scripts / "compoundRegistry.qs").read_text(encoding="utf-8")
    assert "@@COMPOUND_REGISTRY" not in script
    assert (tmp_path / "py" / "python.exe").as_posix() in script
    assert 'home: ""' in script                     # no registry folder: follows the extension
    assert f'version: "{mnova.__version__}"' in script
    assert done["home"] is None and done["version"] == mnova.__version__
    assert done["replaced"] is False and done["scripts_dir"] == str(scripts)
    pinned = mnova.install(scripts_dir=scripts, home=str(tmp_path / "reg"))
    assert (tmp_path / "reg").as_posix() in (scripts / "compoundRegistry.qs").read_text("utf-8")
    assert pinned["home"] and pinned["replaced"] is True
    gone = mnova.uninstall(scripts_dir=scripts)
    assert len(gone["removed"]) == 2 and gone["scripts_dir"] == str(scripts)
    assert list(scripts.iterdir()) == []
    assert mnova.uninstall(scripts_dir=scripts)["removed"] == []


def test_the_shipped_addon_has_the_three_placeholders_once():
    script = (Path(mnova.__file__).parent / "mnova_addon" / mnova.ADDON_SCRIPT).read_text("utf-8")
    assert script.isascii()
    for placeholder, field in ((mnova.PYTHON_PLACEHOLDER, "python"),
                               (mnova.HOME_PLACEHOLDER, "home"),
                               (mnova.VERSION_PLACEHOLDER, "version")):
        assert script.count(placeholder) == 1
        assert f'\t{field}: "{placeholder}",' in script
    # The add-on sends its version, and stops the bridge it gave up waiting for.
    assert 'args.push("--addon-version", compoundRegistry.version)' in script
    assert "proc.kill()" in script


def _addon_function(name: str) -> str:
    """The text of one function of the shipped add-on script."""
    script = (Path(mnova.__file__).parent / "mnova_addon" / mnova.ADDON_SCRIPT).read_text("utf-8")
    start = script.index(f"compoundRegistry.{name} = function")
    return script[start:script.index("\n};\n", start)]


def test_the_addon_tells_a_program_that_did_not_start_from_one_that_did_not_answer():
    # MestReNova ends the wait at once, with false, when the program cannot be started.
    body = _addon_function("runBridge")
    assert "proc.errorCode === Process.FailedToStart" in body
    not_started, timed_out = body.index("if (notStarted) {"), body.index("if (timedOut) {")
    assert not_started < timed_out
    answer = body[not_started:timed_out]
    assert "could not be started:" in answer and "repair" in answer
    assert "did not answer within 60 s" in body[timed_out:]


def test_the_addon_writes_the_pdf_under_a_name_of_its_own_and_trusts_only_the_file():
    body = _addon_function("exportPdf")
    # no fixed second name in the compound folder: "<stem>.new.pdf" can be another document's PDF
    assert ".new.pdf" not in body and "File.remove(fresh)" in body
    assert 'Application.createUuid().replace(/[\\{\\}]/g, "") + ".tmp.pdf"' in body
    # exportToPDF answers true also when nothing was written: its result is never used
    assert "exportToPDF(fresh) &&" not in body and "= aDoc.exportToPDF" not in body
    assert body.count("exportToPDF(") == 2
    assert body.index("compoundRegistry.hasContent(fresh)") < body.index("File.remove(aPath)")
    assert "ok = compoundRegistry.hasContent(aPath);" in body
    assert "new File(aPath).size > 0" in _addon_function("hasContent")


def test_install_refuses_a_relative_home(tmp_path):
    from compound_registry.errors import RegistryError

    with pytest.raises(RegistryError):
        mnova.install(scripts_dir=tmp_path / "s", home="relative/path")


def test_updating_keeps_the_registry_folder_of_the_installed_addon(tmp_path):
    scripts, python = tmp_path / "scripts", _python(tmp_path)
    reg = tmp_path / "my reg"
    mnova.install(scripts_dir=scripts, python=python, home=str(reg))
    again = mnova.install(scripts_dir=scripts, python=python)
    assert again["home"] == reg.as_posix() and again["replaced"] is True
    assert mnova.status(scripts_dir=scripts, mestrenova=NO_MNOVA)["home"] == reg.as_posix()
    other = mnova.install(scripts_dir=scripts, python=python, home=str(tmp_path / "other"))
    assert other["home"] == str(tmp_path / "other")
    dropped = mnova.install(scripts_dir=scripts, python=python, keep_home=False)
    assert dropped["home"] is None
    assert mnova.status(scripts_dir=scripts, mestrenova=NO_MNOVA)["home"] is None


def test_odd_characters_in_paths_stay_readable_and_ascii(tmp_path):
    scripts = tmp_path / "scripts"
    folder = tmp_path / 'J\u00fcrg "x"'
    try:
        folder.mkdir()
        python = folder / "python.exe"
        python.write_bytes(b"")
    except OSError:                       # a quotation mark is not allowed in Windows names
        folder = tmp_path / "J\u00fcrg x"
        folder.mkdir()
        python = folder / "python.exe"
        python.write_bytes(b"")
    mnova.install(scripts_dir=scripts, python=python, home=str(folder))
    assert (scripts / mnova.ADDON_SCRIPT).read_bytes().isascii()
    state = mnova.status(scripts_dir=scripts, mestrenova=NO_MNOVA)
    assert state["python"] == python.as_posix() and state["home"] == folder.as_posix()
    assert state["python_exists"] is True and state["current"] is True


# --------------------------------------------------------------------------- status

def test_status_without_an_addon(tmp_path):
    state = mnova.status(scripts_dir=tmp_path / "scripts", mestrenova=NO_MNOVA)
    assert state == {
        "supported": sys.platform == "win32", "mestrenova": NO_MNOVA,
        "scripts_dir": str(tmp_path / "scripts"), "installed": False, "addon_version": None,
        "program_version": mnova.__version__, "python": None, "python_exists": False,
        "home": None, "current": False, "problems": []}
    assert not (tmp_path / "scripts").exists()                       # read-only
    json.dumps(state)


def test_status_of_a_current_addon(tmp_path):
    scripts, python = tmp_path / "scripts", _python(tmp_path)
    mnova.install(scripts_dir=scripts, python=python)
    state = mnova.status(scripts_dir=scripts, mestrenova=NO_MNOVA)
    assert state["installed"] is True and state["current"] is True and state["problems"] == []
    assert state["addon_version"] == state["program_version"] == mnova.__version__
    assert state["python"] == python.as_posix() and state["python_exists"] is True


def test_status_after_a_program_update(tmp_path, monkeypatch):
    scripts, python = tmp_path / "scripts", _python(tmp_path)
    monkeypatch.setattr(mnova, "__version__", "0.0.1")
    mnova.install(scripts_dir=scripts, python=python)
    monkeypatch.undo()
    state = mnova.status(scripts_dir=scripts, mestrenova=NO_MNOVA)
    assert state["installed"] is True and state["current"] is False
    assert state["addon_version"] == "0.0.1" and state["program_version"] == mnova.__version__
    assert any("version 0.0.1" in p and mnova.__version__ in p for p in state["problems"])
    assert mnova.install(scripts_dir=scripts, python=python)["replaced"] is True   # the update
    assert mnova.status(scripts_dir=scripts, mestrenova=NO_MNOVA)["current"] is True


def test_status_of_an_addon_from_before_versions(tmp_path):
    scripts, python = tmp_path / "scripts", _python(tmp_path)
    mnova.install(scripts_dir=scripts, python=python)
    script = scripts / mnova.ADDON_SCRIPT
    script.write_text(script.read_text("utf-8").replace(
        f'\tversion: "{mnova.__version__}",\n', ""), encoding="utf-8", newline="\n")
    state = mnova.status(scripts_dir=scripts, mestrenova=NO_MNOVA)
    assert state["installed"] is True and state["addon_version"] is None
    assert state["current"] is False and "before versions" in state["problems"][0]
    assert state["python_exists"] is True


def test_status_after_the_program_folder_moved(tmp_path):
    scripts, python = tmp_path / "scripts", _python(tmp_path)
    mnova.install(scripts_dir=scripts, python=python)
    python.unlink()
    state = mnova.status(scripts_dir=scripts, mestrenova=NO_MNOVA)
    assert state["python_exists"] is False and state["current"] is False
    assert python.as_posix() in state["problems"][0] and "moved" in state["problems"][0]


def test_status_notices_a_changed_or_incomplete_addon(tmp_path):
    scripts, python = tmp_path / "scripts", _python(tmp_path)
    mnova.install(scripts_dir=scripts, python=python)
    script = scripts / mnova.ADDON_SCRIPT
    script.write_text(script.read_text("utf-8") + "\n// edited\n", encoding="utf-8", newline="\n")
    state = mnova.status(scripts_dir=scripts, mestrenova=NO_MNOVA)
    assert state["current"] is False and "differs" in state["problems"][0]
    mnova.install(scripts_dir=scripts, python=python)
    (scripts / "compoundRegistry.mui").unlink()
    state = mnova.status(scripts_dir=scripts, mestrenova=NO_MNOVA)
    assert state["current"] is False and "compoundRegistry.mui is missing" in state["problems"][0]


def test_find_mestrenova(tmp_path):
    def find(**kwargs):
        kwargs.setdefault("entries", [])
        kwargs.setdefault("program_dirs", [])
        kwargs.setdefault("appdata", tmp_path / "no-appdata")
        return mnova.find_mestrenova(**kwargs)

    assert find() == NO_MNOVA
    listed = find(entries=[("MestReNova 17.0", " 17.0.1 ", "C:\\Programs\\MestReNova\\")])
    assert listed == {"found": True, "version": "17.0.1", "folder": "C:\\Programs\\MestReNova",
                      "evidence": "installed-programs"}
    assert find(entries=[("MestReNova", "", "")])["version"] is None
    program = tmp_path / "MestReNova"
    program.mkdir()
    assert find(program_dirs=[program]) == NO_MNOVA               # no MestReNova.exe in it
    (program / "MestReNova.exe").write_bytes(b"")
    assert find(program_dirs=[tmp_path / "none", program]) == {
        "found": True, "version": None, "folder": str(program), "evidence": "program-folder"}
    appdata = tmp_path / "appdata"
    mnova.mnova_scripts_dir(appdata).parent.mkdir(parents=True)
    assert find(appdata=appdata)["evidence"] == "user-folder"
    json.dumps(listed)


def test_elsewhere_than_windows_the_script_folder_must_be_named(tmp_path, monkeypatch, capsys):
    from compound_registry.errors import RegistryError

    monkeypatch.setattr(mnova.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("APPDATA", raising=False)
    for call in (mnova.install, mnova.uninstall, mnova.mnova_scripts_dir):
        with pytest.raises(RegistryError) as caught:
            call()
        assert caught.value.code == "mnova_unsupported"
        assert "--scripts-dir" in caught.value.message and "Windows" in caught.value.message
    assert not (tmp_path / "home").exists()                 # nothing was written anywhere
    state = mnova.status()
    assert state["supported"] is False and state["installed"] is False
    assert state["scripts_dir"] is None and state["mestrenova"]["found"] is False
    assert mnova.main(["install", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "mnova_unsupported"
    assert mnova.main(["status"]) == 0
    assert "--scripts-dir" in capsys.readouterr().out
    # With the folder named it works as on Windows.
    scripts = tmp_path / "scripts"
    assert mnova.install(scripts_dir=scripts, python=_python(tmp_path))["version"]
    assert mnova.status(scripts_dir=scripts)["current"] is True


def test_setup_commands_report_as_json(tmp_path, monkeypatch, capsys):
    scripts, python = tmp_path / "scripts", _python(tmp_path)
    found = {"found": True, "version": "17.0.1", "folder": "C:\\MestReNova",
             "evidence": "installed-programs"}
    monkeypatch.setattr(mnova, "find_mestrenova", lambda: found)
    base = ["--scripts-dir", str(scripts), "--json"]

    assert mnova.main(["status", *base]) == 0
    before = json.loads(capsys.readouterr().out)
    assert before["ok"] is True and before["result"]["installed"] is False
    assert before["result"]["mestrenova"] == found

    assert mnova.main(["install", *base, "--python", str(python),
                       "--home", str(tmp_path / "reg")]) == 0
    done = json.loads(capsys.readouterr().out)["result"]
    assert done["version"] == mnova.__version__ and done["python"] == str(python)
    assert done["home"] == str(tmp_path / "reg")

    assert mnova.main(["status", *base]) == 0
    after = json.loads(capsys.readouterr().out)["result"]
    assert after["installed"] is True and after["current"] is True
    assert after["addon_version"] == mnova.__version__

    assert mnova.main(["install", *base, "--python", str(python), "--follow-extension"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["home"] is None

    assert mnova.main(["uninstall", *base]) == 0
    assert len(json.loads(capsys.readouterr().out)["result"]["removed"]) == 2
    assert mnova.main(["install", *base, "--home", "relative"]) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused["ok"] is False and refused["error"]["code"] == "invalid_argument"


def test_setup_commands_in_plain_words(tmp_path, monkeypatch, capsys):
    scripts, python = tmp_path / "scripts", _python(tmp_path)
    monkeypatch.setattr(mnova, "find_mestrenova", lambda: NO_MNOVA)
    base = ["--scripts-dir", str(scripts)]
    assert mnova.main(["status", *base]) == 0
    printed = capsys.readouterr().out
    assert "MestReNova   not found" in printed and "not installed" in printed
    assert mnova.main(["install", *base, "--python", str(python)]) == 0
    printed = capsys.readouterr().out
    assert f"MestReNova add-on {mnova.__version__} installed in {scripts}" in printed
    assert "Restart MestReNova" in printed and "was not found on this PC" in printed
    assert mnova.main(["install", *base, "--python", str(python)]) == 0
    assert "updated in" in capsys.readouterr().out
    assert mnova.main(["status", *base]) == 0
    assert "up to date" in capsys.readouterr().out
    python.unlink()
    assert mnova.main(["status", *base]) == 0
    printed = capsys.readouterr().out
    assert "not there any more" in printed and "compound-registry setup" in printed
    # the advice names the Python of this program in full, not a bare "python"
    assert f'"{Path(sys.executable)}" -m compound_registry.mnova install' in printed
    assert "'python -m" not in printed
    assert mnova.main(["uninstall", *base]) == 0
    assert capsys.readouterr().out.count("Removed ") == 2
    assert mnova.main(["uninstall", *base]) == 0
    assert "No MestReNova add-on was installed" in capsys.readouterr().out
    assert mnova.main(["install", *base, "--home", "relative"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("error: ")


# --------------------------------------------------------------------------- add-on version

def test_the_bridge_refuses_an_addon_of_another_version(home, tmp_path):
    text = tmp_path / "title.txt"
    text.write_text("Nummer ABC-006", encoding="utf-8")
    out = tmp_path / "answer.json"
    base = ["propose", "--text-file", str(text), "--home", str(home), "--out", str(out)]
    for sent, words in ((["--addon-version", "0.0.1"], "version 0.0.1"),
                        ([], "from before versions")):
        out.unlink(missing_ok=True)
        assert mnova.main([*base, *sent]) == 1
        answer = json.loads(out.read_text(encoding="ascii"))
        assert answer["ok"] is False and answer["error"]["code"] == "addon_outdated"
        message = answer["error"]["message"]
        assert words in message and f"version {mnova.__version__}" in message
        assert "compound_registry.mnova install" in message and "restart MestReNova" in message
    assert mnova.main([*base, *ADDON]) == 0
    assert json.loads(out.read_text(encoding="ascii"))["ok"] is True


def test_check_addon_version():
    from compound_registry.errors import RegistryError

    mnova.check_addon_version(mnova.__version__)
    mnova.check_addon_version(f" {mnova.__version__} ")
    for other in (None, "", "0.0.1", mnova.__version__ + ".1", 3):
        with pytest.raises(RegistryError) as caught:
            mnova.check_addon_version(other)
        assert caught.value.code == "addon_outdated"


# --------------------------------------------------------------------------- answers in any case

def test_an_unexpected_error_still_gives_the_addon_an_answer(home, tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise KeyError("odd data")

    monkeypatch.setattr(mnova, "record", broken)
    data = tmp_path / "s.json"
    data.write_text("{}", encoding="ascii")
    out = tmp_path / "answer.json"
    assert mnova.main(["record", "--data-file", str(data), "--home", str(home),
                       "--out", str(out), *ADDON]) == 1
    answer = json.loads(out.read_text(encoding="ascii"))
    assert answer == {"ok": False, "error": {"code": "internal_error",
                                             "message": "KeyError: 'odd data'"}}


def test_an_answer_file_that_cannot_be_written_is_printed_instead(home, tmp_path, capsys):
    text = tmp_path / "title.txt"
    text.write_text("Nummer ABC-006", encoding="utf-8")
    capsys.readouterr()
    assert mnova.main(["propose", "--text-file", str(text), "--home", str(home),
                       "--out", str(tmp_path / "no such folder" / "a.json"), *ADDON]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_no_stdout_is_not_an_error(home, tmp_path, monkeypatch):
    # pythonw.exe, which the add-on starts, has no stdout and no stderr.
    monkeypatch.setattr(mnova.sys, "stdout", None)
    monkeypatch.setattr(mnova.sys, "stderr", None)
    text = tmp_path / "title.txt"
    text.write_text("Nummer ABC-006", encoding="utf-8")
    assert mnova.main(["propose", "--text-file", str(text), "--home", str(home), *ADDON]) == 0
    assert mnova.main(["install", "--scripts-dir", str(tmp_path / "s"), "--home", "relative"]) == 1


# --------------------------------------------------------------------------- recorded spectra

REPORT_HTML = ('<html><head><meta charset="utf-8"><title>Multiplet Report</title></head><body>'
               '<font style="font-size: 10pt"><sup>1</sup>H NMR (500 MHz, CDCl<sub>3</sub>) &delta;  '
               '7.40 (t, <i>J</i> = 1.9 Hz, 1H),  7.36 &ndash; 7.31 (m, 1H),  0.86 (s, 9H).</font>'
               '</body></html>')
REPORT = "1H NMR (500 MHz, CDCl3) δ 7.40 (t, J = 1.9 Hz, 1H), 7.36 – 7.31 (m, 1H), 0.86 (s, 9H)."


def _spectrum(**changes):
    base = {"title": "user.011.1.fid", "comment": "Nummer ABC-006-FC2-concentrated",
            "nucleus": "1H", "dimensions": 1, "frequency_mhz": 500.13300078, "solvent": "CDCl3",
            "report_html": REPORT_HTML, "parameters": {"Solvent": "CDCl3", "Probe": "", "NS": 16},
            "mnova_file": "C:/nmr/a.mnova", "pdf_file": "C:/nmr/a.pdf", "not_a_field": "dropped"}
    base.update(changes)
    return base


def test_report_html_becomes_one_plain_line():
    from compound_registry.spectra import clean_spectrum, report_text

    assert report_text(REPORT_HTML) == REPORT
    key, data = clean_spectrum(_spectrum())
    assert key == "user.011.1.fid | 1H | 1D"
    assert data["report"] == REPORT and data["report_html"] == REPORT_HTML
    assert data["parameters"] == {"Solvent": "CDCl3", "NS": "16"}
    assert "not_a_field" not in data and data["frequency_mhz"] == 500.133001


def test_a_crude_spectrum_keeps_only_where_its_document_is():
    from compound_registry.spectra import clean_spectrum

    key, data = clean_spectrum(_spectrum(crude=True, report_kind="multiplets"))
    assert key == "user.011.1.fid | 1H | 1D"
    assert data["crude"] is True and data["mnova_file"] == "C:/nmr/a.mnova"
    assert not {"report", "report_html", "report_kind", "pdf_file", "parameters"} & set(data)
    assert "crude" not in clean_spectrum(_spectrum(crude=False))[1]
    assert "crude" not in clean_spectrum(_spectrum())[1]


@pytest.mark.parametrize("bad", [
    "text", {"title": ["a"]}, {"frequency_mhz": -1}, {"dimensions": 9}, {"parameters": []},
    {"report_html": "x" * 60001}, {"crude": "yes"}, {"crude": 1}])
def test_malformed_spectrum_is_refused(bad):
    from compound_registry.errors import RegistryError
    from compound_registry.spectra import clean_spectrum

    with pytest.raises(RegistryError) as info:
        clean_spectrum(bad)
    assert info.value.code == "invalid_argument"


def test_record_stores_replaces_and_survives_reopen(home):
    with RegistryService.open(home) as svc:
        first = mnova.record({"full_id": "000001_01_001", "spectra": [_spectrum()]}, service=svc)
        assert first["changed"] == 1 and first["spectra"][0]["data"]["report"] == REPORT
        again = mnova.record({"full_id": "000001_01_001", "spectra": [_spectrum()]}, service=svc)
        assert again["changed"] == 0                                  # same data: nothing written
        carbon = _spectrum(title="user.012.1.fid", nucleus="13C", report_html="")
        newer = _spectrum(report_html=REPORT_HTML.replace("0.86", "0.87"))
        both = mnova.record({"full_id": "000001_01_001", "spectra": [newer, carbon]}, service=svc)
        assert both["changed"] == 2
    with RegistryService.open(home) as svc:
        stored = svc.spectra("000001_01_001")["spectra"]
        assert [s["spectrum_key"] for s in stored] == [
            "user.011.1.fid | 1H | 1D", "user.012.1.fid | 13C | 1D"]
        assert "0.87" in stored[0]["data"]["report"] and "report" not in stored[1]["data"]
        assert svc.spectra("000001_01")["spectra"] == stored            # by CORE_STEREO
        assert svc.spectra("000002_01_001")["spectra"] == []
        assert len(svc.spectra()["spectra"]) == 2
        events = svc.repo._require().execute(
            "SELECT count(*) FROM events WHERE event = 'spectrum_recorded'").fetchone()[0]
        assert events == 3


def test_record_refuses_unknown_ids_and_bad_data_without_storing(home):
    from compound_registry.errors import RegistryError

    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as info:
            mnova.record({"full_id": "000099_01_001", "spectra": [_spectrum()]}, service=svc)
        assert info.value.code == "not_found"
        for payload in ("x", {"full_id": "000001_01_001"}, {"full_id": "000001_01_001", "spectra": []},
                        {"full_id": "000001_01_001", "spectra": [_spectrum(), {"dimensions": 9}]}):
            with pytest.raises(RegistryError):
                mnova.record(payload, service=svc)
        assert svc.spectra()["spectra"] == []


def test_a_registry_from_before_spectra_gets_the_table(home):
    import sqlite3

    conn = sqlite3.connect(home / "registry.sqlite")
    conn.execute("DROP TABLE spectra")
    conn.commit()
    conn.close()
    with RegistryService.open(home) as svc:
        assert svc.spectra()["spectra"] == []
        assert svc.record_spectra("000001_01_001", [_spectrum()])["changed"] == 1


def test_record_command_and_cli_listing(home, tmp_path, capsys):
    from compound_registry import cli

    data = tmp_path / "spectra.json"
    data.write_text(json.dumps({"full_id": "000002_01_001", "spectra": [_spectrum()]}),
                    encoding="ascii")
    out = tmp_path / "answer.json"
    assert mnova.main(["record", "--data-file", str(data), "--home", str(home),
                       "--out", str(out), *ADDON]) == 0
    assert json.loads(out.read_text())["result"]["changed"] == 1
    data.write_text("{not json", encoding="ascii")
    assert mnova.main(["record", "--data-file", str(data), "--home", str(home),
                       "--out", str(out), *ADDON]) == 1
    assert json.loads(out.read_text())["error"]["code"] == "invalid_argument"
    capsys.readouterr()
    assert cli.main(["--home", str(home), "--json", "spectra", "000002_01"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["spectra"][0]["data"]["pdf_file"] == "C:/nmr/a.pdf"


# --------------------------------------------------------------------------- compound folder

def test_folder_is_the_registry_folder_and_is_made_again_if_missing(home):
    import shutil

    expected = home / "compounds" / "000001_01"
    first = mnova.folder({"core_stereo_id": "000001_01"}, home=str(home))
    assert first["path"] == str(expected) and first["created"] is False
    assert first["files_folder"] == str(home / "compounds")
    shutil.rmtree(expected)
    again = mnova.folder({"core_stereo_id": "000001_01"}, home=str(home))
    assert again["created"] is True and (expected / "000001_01.mol").is_file()


def test_propose_tells_where_the_compound_folders_are(home):
    assert mnova.propose("ABC-006", home=str(home))["files_folder"] == str(home / "compounds")


@pytest.mark.parametrize("payload, code", [
    ({"core_stereo_id": "999999_01"}, "not_found"),
    ({}, "invalid_argument"), ("000001_01", "invalid_argument"),
    ({"core_stereo_id": 1}, "invalid_argument"),
])
def test_folder_refuses_unknown_compounds_and_bad_requests(home, payload, code):
    from compound_registry.errors import RegistryError

    with pytest.raises(RegistryError) as caught:
        mnova.folder(payload, home=str(home))
    assert caught.value.code == code
    assert not (home / "compounds" / "999999_01").exists()


def test_folder_that_cannot_be_made_is_an_error_with_the_reason(home):
    import shutil

    from compound_registry.errors import RegistryError

    shutil.rmtree(home / "compounds" / "000002_01")
    (home / "compounds" / "000002_01").write_text("in the way")
    with pytest.raises(RegistryError) as caught:
        mnova.folder({"core_stereo_id": "000002_01"}, home=str(home))
    assert caught.value.code == "file_unavailable" and "in the way" in str(caught.value)


def test_folder_command(home, tmp_path):
    data = tmp_path / "compound.json"
    data.write_text(json.dumps({"core_stereo_id": "000002_01"}), encoding="ascii")
    out = tmp_path / "answer.json"
    assert mnova.main(["folder", "--data-file", str(data), "--home", str(home),
                       "--out", str(out), *ADDON]) == 0
    assert json.loads(out.read_text())["result"]["path"] == str(home / "compounds" / "000002_01")


def test_the_addon_has_no_folder_setting_of_its_own():
    from pathlib import Path

    addon = Path(mnova.__file__).parent / "mnova_addon"
    for name in mnova.ADDON_FILES:
        text = (addon / name).read_text(encoding="utf-8")
        assert "SaveFolder" not in text and "compoundRegistryFolder" not in text


# --------------------------------------------------------------------------- search

def _found(result):
    return [(m["experiment_ref"], [c["full_id"] for c in m["candidates"]])
            for m in result["matches"]]


@pytest.mark.parametrize("query", ["ABC-006", "abc006", " ABC 006 ", "Nummer ABC-006-FC2"])
def test_search_by_experiment_number(home, query):
    result = mnova.search(query, home=str(home))
    assert _found(result) == [("ABC-006", ["000001_01_001", "000002_01_001"])]
    assert result["truncated"] is False and result["query"] == query.strip()


def test_search_by_part_of_the_number_lists_every_experiment_it_fits(home):
    assert [ref for ref, _ in _found(mnova.search("006", home=str(home)))] == ["ABC-006", "ABC-0061"]
    # an experiment named exactly is not mixed with the ones it is only a part of
    assert [ref for ref, _ in _found(mnova.search("ABC-006", home=str(home)))] == ["ABC-006"]
    assert [ref for ref, _ in _found(mnova.search("ABC-0061", home=str(home)))] == ["ABC-0061"]


@pytest.mark.parametrize("query, ids", [
    ("000002_01_001", ["000002_01_001"]), ("000002", ["000002_01_001"]),
    ("000002-01", ["000002_01_001"]),
])
def test_search_by_compound_id(home, query, ids):
    assert _found(mnova.search(query, home=str(home))) == [("ABC-006", ids)]


def test_search_never_offers_a_retired_batch_and_finds_nothing_for_nonsense(home):
    retired = mnova.search("000003_01_002", home=str(home))
    assert _found(retired) == [] and retired["known_experiments"] == 2
    for query in ("", "   ", "---", "XYZ-999"):
        assert _found(mnova.search(query, home=str(home))) == []


def test_search_labels_the_file_with_the_dataset_name_and_draws(home, tmp_path):
    result = mnova.search("ABC-0061", text="user.011.1.fid\nno number here", home=str(home),
                          image_dir=tmp_path / "img")
    match = result["matches"][0]
    assert match["sample"] == "" and match["file_label"] == "user.011.1.fid"
    assert match["candidates"][0]["image"].endswith(".png")
    assert result["files_folder"] == str(home / "compounds")


def test_search_is_cut_at_the_limit(home, monkeypatch):
    monkeypatch.setattr(mnova, "MAX_SEARCH_RESULTS", 1)
    result = mnova.search("ABC-006", home=str(home))
    assert _found(result) == [("ABC-006", ["000001_01_001"])] and result["truncated"] is True


def test_search_changes_nothing_and_refuses_bad_requests(home, tmp_path):
    from compound_registry.errors import RegistryError

    def state():
        with RegistryService.open(home) as svc:
            return svc.doctor()["stats"]

    journal = (home / "issued-ids.jsonl").read_bytes()
    before = state()
    mnova.search("ABC-006", home=str(home))
    assert state() == before and (home / "issued-ids.jsonl").read_bytes() == journal
    for bad in (None, 6, "x" * 201):
        with pytest.raises(RegistryError) as caught:
            mnova.search(bad, home=str(home))
        assert caught.value.code == "invalid_argument"
    data = tmp_path / "q.json"
    out = tmp_path / "a.json"
    data.write_text(json.dumps({"query": "000002", "text": "user.011.1.fid"}), encoding="ascii")
    assert mnova.main(["search", "--data-file", str(data), "--home", str(home),
                       "--out", str(out), *ADDON]) == 0
    assert _found(json.loads(out.read_text())["result"]) == [("ABC-006", ["000002_01_001"])]
    data.write_text("[]", encoding="ascii")
    assert mnova.main(["search", "--data-file", str(data), "--home", str(home),
                       "--out", str(out), *ADDON]) == 1


def test_folder_also_gives_the_registered_structure_for_pasting(home):
    molfile = mnova.folder({"core_stereo_id": "000001_01"}, home=str(home))["molfile"]
    assert "V2000" in molfile or "V3000" in molfile
    assert molfile.strip() == (home / "compounds" / "000001_01" / "000001_01.mol").read_text().strip()


# --------------------------------------------------------------------------- several spectra, peak reports

def test_a_document_with_several_nuclei_is_recorded_spectrum_by_spectrum(home):
    carbon = _spectrum(title="user.012.1.fid", nucleus="13C", frequency_mhz=125.76,
                       report_kind="peaks",
                       report_html="<html><head><title>Peak Report</title></head><body>"
                                   "<sup>13</sup>C NMR (126 MHz, CDCl<sub>3</sub>) &delta; 171.1, 60.4."
                                   "</body></html>")
    fluorine = _spectrum(title="user.013.1.fid", nucleus="19F", report_html="", report=None)
    hsqc = _spectrum(title="user.014.1.ser", nucleus="1H", dimensions=2, report_html="")
    twin = _spectrum(key="user.011.1.fid | 1H | 1D #2")
    with RegistryService.open(home) as svc:
        result = svc.record_spectra("000001_01_001",
                                    [_spectrum(report_kind="multiplets"), carbon, fluorine, hsqc, twin])
        assert result["changed"] == 5
        stored = {s["spectrum_key"]: s["data"] for s in svc.spectra("000001_01_001")["spectra"]}
    assert set(stored) == {"user.011.1.fid | 1H | 1D", "user.012.1.fid | 13C | 1D",
                           "user.013.1.fid | 19F | 1D", "user.014.1.ser | 1H | 2D",
                           "user.011.1.fid | 1H | 1D #2"}
    assert stored["user.012.1.fid | 13C | 1D"]["report"] == "13C NMR (126 MHz, CDCl3) δ 171.1, 60.4."
    assert stored["user.012.1.fid | 13C | 1D"]["report_kind"] == "peaks"
    assert stored["user.011.1.fid | 1H | 1D"]["report_kind"] == "multiplets"
    assert "report" not in stored["user.013.1.fid | 19F | 1D"]


def test_report_kind_needs_a_report_and_a_known_value():
    from compound_registry import spectra
    from compound_registry.errors import RegistryError

    assert "report_kind" not in spectra.clean_spectrum(
        _spectrum(report_html="", report=None, report_kind="peaks"))[1]
    with pytest.raises(RegistryError) as caught:
        spectra.clean_spectrum(_spectrum(report_kind="guessed"))
    assert caught.value.code == "invalid_argument"


# --------------------------------------------------------------------------- library

def _library_ids(result):
    return [c["core_stereo_id"] for c in result["compounds"]]


def test_library_lists_every_compound_with_its_saved_documents(home, tmp_path):
    import os
    import shutil

    folder = home / "compounds" / "000001_01"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(home / "compounds" / "000002_01", ignore_errors=True)
    (folder / "old.mnova").write_bytes(b"x")
    (folder / "new.MNOVA").write_bytes(b"x")
    (folder / "report.pdf").write_bytes(b"%PDF-")
    (folder / "sub.mnova").mkdir()
    os.utime(folder / "old.mnova", (1_600_000_000, 1_600_000_000))
    result = mnova.library(home=str(home), image_dir=tmp_path / "img")
    # the retired batch does not count, but ethanol has another batch (ABC-0061)
    assert _library_ids(result) == ["000001_01", "000002_01", "000003_01"]
    assert result["total"] == 3 and result["truncated"] is False
    first = result["compounds"][0]
    assert first["ids"] == ["000001_01_001"] and first["experiments"] == ["ABC-006"]
    assert first["name"] == "(S)-1-phenylethan-1-amine" and first["formula"]
    assert first["image"].endswith("library-000001_01.png")
    assert [d["name"] for d in first["documents"]] == ["new.MNOVA", "old.mnova"]
    assert first["documents"][1]["path"] == str(folder / "old.mnova")
    assert result["compounds"][1]["documents"] == []
    assert result["compounds"][2]["experiments"] == ["ABC-0061"]
    # reading the library makes no folder
    assert not (home / "compounds" / "000002_01").exists()


@pytest.mark.parametrize("query, ids", [
    ("", ["000001_01", "000002_01", "000003_01"]),
    ("000002", ["000002_01"]), ("000002_01_001", ["000002_01"]),
    ("abc-0061", ["000003_01"]), ("ABC 006", ["000001_01", "000002_01", "000003_01"]),
    ("phenylethan", ["000001_01"]), ("abc006 amine", ["000001_01"]),
    ("c2h6o", ["000003_01"]), ("nothing-like-this", []),
])
def test_library_search(home, query, ids):
    assert _library_ids(mnova.library(query, home=str(home))) == ids


def test_library_is_cut_at_the_limit_changes_nothing_and_refuses_bad_requests(home, tmp_path,
                                                                             monkeypatch):
    from compound_registry.errors import RegistryError

    def state():
        with RegistryService.open(home) as svc:
            return svc.doctor()["stats"]

    journal = (home / "issued-ids.jsonl").read_bytes()
    before = state()
    monkeypatch.setattr(mnova, "MAX_LIBRARY", 2)
    cut = mnova.library(home=str(home))
    assert _library_ids(cut) == ["000001_01", "000002_01"]
    assert cut["truncated"] is True and cut["total"] == 3
    assert state() == before and (home / "issued-ids.jsonl").read_bytes() == journal
    for bad in (None, 6, "x" * 201):
        with pytest.raises(RegistryError) as caught:
            mnova.library(bad, home=str(home))
        assert caught.value.code == "invalid_argument"
    data = tmp_path / "q.json"
    out = tmp_path / "a.json"
    data.write_text(json.dumps({"query": "000003"}), encoding="ascii")
    assert mnova.main(["library", "--data-file", str(data), "--home", str(home),
                       "--out", str(out), *ADDON]) == 0
    answer = json.loads(out.read_text(encoding="ascii"))
    assert answer["ok"] is True and _library_ids(answer["result"]) == ["000003_01"]


def test_addon_has_the_library_action():
    from pathlib import Path

    addon = Path(mnova.__file__).parent / "mnova_addon"
    qs = (addon / "compoundRegistry.qs").read_text(encoding="utf-8")
    mui = (addon / "compoundRegistry.mui").read_text(encoding="utf-8")
    assert "MnUi.compoundRegistry.compoundRegistryLibrary" in qs
    assert mui.count('<addaction name="compoundRegistryLibrary"/>') == 2
