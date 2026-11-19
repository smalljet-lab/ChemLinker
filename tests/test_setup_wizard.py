"""The guided installation ``compound-registry setup`` (setup_wizard.py).

Nothing here touches the machine: the questions and the output are scripted,
the browser registration goes to a target in ``tmp_path`` with a fake Windows
registry, npm and MestReNova are fakes, and the project folder is a stand-in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from compound_registry import __version__, cli, install_host, setup_wizard
from compound_registry.cli import main
from compound_registry.errors import RegistryError
from compound_registry.install_host import (CHROME_REGISTRY_KEY, REGISTRY_KEY, HostTarget,
                                            default_target)
from compound_registry.service import RegistryService
from compound_registry.setup_wizard import (DEFAULT_ELN_ORIGIN, Environment, Options,
                                            id_format_for_prefix, next_steps, node_version_ok,
                                            normalise_folder, parse_eln_origin,
                                            read_saved_eln_origin, registry_summary, run_setup)


class FakeRegistry:
    def __init__(self):
        self.values: dict[str, str] = {}

    def read_default(self, key):
        return self.values.get(key)

    def write_default(self, key, value):
        self.values[key] = value

    def delete_key(self, key):
        return self.values.pop(key, None) is not None


class Script:
    """Scripted answers; records every prompt and every printed line."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.lines: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def say(self, line: str = "") -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def asked(self, part: str) -> bool:
        return any(part in prompt for prompt in self.prompts)


class Npm:
    """Stands in for node and npm: records the commands, builds nothing."""

    def __init__(self, version: str = "v22.14.0", fail: str | None = None):
        self.version = version
        self.fail = fail
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), Path(cwd)))
        if list(argv[1:]) == ["--version"]:
            return 0, self.version + "\n"
        if self.fail is not None and self.fail in argv:
            return 1, "first line\nnpm ERR! it broke\n"
        return 0, "wrote the package\n"

    @property
    def npm(self) -> list[list[str]]:
        return [argv[1:] for argv, _cwd in self.calls if argv[0] == "npm-exe"]


class Mnova:
    def __init__(self, *, supported=True, found=False, installed=False, fail=False):
        self.state = {"supported": supported, "installed": installed,
                      "mestrenova": {"found": found, "version": "15.0.1" if found else None}}
        self.fail = fail
        self.installs = 0

    def status(self):
        return dict(self.state)

    def install(self):
        if self.fail:
            raise RegistryError("the script folder is read-only", code="mnova_install_failed")
        self.installs += 1
        return {"replaced": self.state["installed"], "scripts_dir": "the-scripts-folder"}


def create_registry(path: Path, batches: int = 0) -> Path:
    with RegistryService.open(path) as svc:
        for i in range(batches):
            svc.register(smiles="C" * (i + 1) + "O", experiment_ref=f"E{i}")
    return path


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    ext = root / "extension"
    ext.mkdir(parents=True)
    (ext / "package.json").write_text(json.dumps({"version": "9.8.7"}), encoding="utf-8")
    (ext / "package-lock.json").write_text("{}", encoding="utf-8")
    return root


def make_target(tmp_path: Path, registry: FakeRegistry, platform: str = "windows",
                venv: str = "venv") -> HostTarget:
    target = default_target(platform=platform, prefix=tmp_path / venv,
                            user_home=tmp_path / "user", registry=registry)
    target.launcher.parent.mkdir(parents=True, exist_ok=True)
    target.launcher.write_bytes(b"launcher")
    return target


def snapshot(tmp_path: Path, registry: FakeRegistry) -> dict:
    files = {str(p.relative_to(tmp_path)): (p.read_bytes(), p.stat().st_mtime_ns)
             for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    folders = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir())
    return {"files": files, "folders": folders, "registry": dict(registry.values)}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("the real Windows registry must not be used in tests")

    for name in ("read_default", "write_default", "delete_key"):
        monkeypatch.setattr(install_host.WindowsRegistry, name, forbidden)
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata-env"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def target(tmp_path, registry) -> HostTarget:
    return make_target(tmp_path, registry)


@pytest.fixture
def root(tmp_path) -> Path:
    return make_root(tmp_path)


class Run:
    """One wizard run with every outside part replaced."""

    def __init__(self, target, root, *answers, node=True, npm=None, mnova=None, can_ask=True,
                 **env):
        self.script = Script(*answers)
        self.npm = npm or Npm()
        self.mnova = mnova or Mnova()
        tools = {"node": "node-exe", "npm": "npm-exe"} if node else {}
        self.env = Environment(
            ask=self.script.ask, say=self.script.say, can_ask=can_ask, target=target,
            project_root=root, which=tools.get, run=self.npm,
            mnova_status=self.mnova.status, mnova_install=self.mnova.install, **env)

    def __call__(self, **options) -> int:
        return run_setup(Options(**options), self.env)

    @property
    def text(self) -> str:
        return self.script.text


def pinned(target: HostTarget) -> str:
    return json.loads(target.sidecar_path.read_text(encoding="utf-8"))["home"]


# --------------------------------------------------------------------------- pure parts

@pytest.mark.parametrize("value, origin", [
    ("https://eln.example.org", "https://eln.example.org"),
    ("  https://ELN.Example.org/  ", "https://eln.example.org"),
    ("https://eln.example.org:8443", "https://eln.example.org:8443"),
    ("https://eln.example.org:443", "https://eln.example.org"),
    ("https://10.0.0.5:8443/", "https://10.0.0.5:8443"),
    ("https://my-eln.sub-1.example.org:1", "https://my-eln.sub-1.example.org:1"),
    (DEFAULT_ELN_ORIGIN, DEFAULT_ELN_ORIGIN),
])
def test_eln_origin_accepted(value, origin):
    assert parse_eln_origin(value) == origin


@pytest.mark.parametrize("value, why", [
    ("", "it is empty"),
    ("http://eln.example.org", "it must start with https://"),
    ("eln.example.org", "it must start with https://"),
    ("https://eln.example.org/sciformation", "must not contain a path"),
    ("https://eln.example.org/?a=1", "query or a fragment"),
    ("https://eln.example.org#x", "query or a fragment"),
    ("https://user:pw@eln.example.org", "user name or password"),
    ("https://eln example.org", "white space"),
    ("https://eln_example.org", "host name is not valid"),
    ("https://", "host name is not valid"),
    ("https://eln..example.org", "host name is not valid"),
    ("https://eln.-example.org", "host name is not valid"),
    ("https://eln.example.org.", "host name is not valid"),
    ("https://eln.example.org:0", "the port is not valid"),
    ("https://eln.example.org:99999", "not a URL"),
    ("https://\u00e9ln.example.org", "ASCII"),
])
def test_eln_origin_refused(value, why):
    with pytest.raises(RegistryError) as info:
        parse_eln_origin(value)
    assert info.value.code == "invalid_argument" and why in info.value.message
    assert "https://eln.example.org:8443" in info.value.message


def test_the_default_eln_origin_is_the_one_of_the_extension_build():
    source = (Path(setup_wizard.__file__).resolve().parents[2] / "extension" / "scripts"
              / "eln-origin.mjs").read_text(encoding="utf-8")
    assert f'DEFAULT_ELN_ORIGIN = "{DEFAULT_ELN_ORIGIN}"' in source
    assert f'LOCAL_CONFIG_FILE = "{setup_wizard.LOCAL_CONFIG_FILE}"' in source


def test_normalise_folder(tmp_path, monkeypatch):
    base = str(tmp_path / "Lab Data" / "Registry")
    assert normalise_folder(base) == base
    assert normalise_folder(f'  "{base}{os.sep}"  ') == base        # pasted with quotes
    assert normalise_folder(f"'{base}'") == base
    assert normalise_folder(base + os.sep) == base
    monkeypatch.setenv("WIZARD_TEST_DATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "me"))
    variable = "%WIZARD_TEST_DATA%" if os.name == "nt" else "$WIZARD_TEST_DATA"
    assert normalise_folder(os.path.join(variable, "reg")) == str(tmp_path / "reg")
    assert normalise_folder("~/reg") == str(tmp_path / "me" / "reg")
    for bad in ("", "   ", '""', "reg", os.path.join("some", "folder")):
        with pytest.raises(RegistryError) as info:
            normalise_folder(bad)
        assert info.value.code == "invalid_argument"


def test_id_format_for_prefix():
    assert id_format_for_prefix("") == "{core:06d}_{stereo:02d}_{batch:03d}"
    assert id_format_for_prefix(" ABC ") == "ABC{core:06d}_{stereo:02d}_{batch:03d}"
    with pytest.raises(RegistryError):
        id_format_for_prefix("A{B}")


@pytest.mark.parametrize("text, ok", [
    ("v20.19.0", True), ("v20.18.3", False), ("v21.7.0", False), ("v22.11.0", False),
    ("v22.12.0\n", True), ("v24.1.0", True), ("v18.20.4", False), ("", False),
    ("not found", False),
])
def test_node_version_ok(text, ok):
    assert node_version_ok(text) is ok


def test_read_saved_eln_origin(root):
    assert read_saved_eln_origin(None) == (None, None)
    assert read_saved_eln_origin(root) == (None, None)
    path = root / "extension" / "local.config.json"
    path.write_bytes(b'\xef\xbb\xbf{"elnOrigin": "https://eln.example.org/"}')
    assert read_saved_eln_origin(root) == ("https://eln.example.org", None)
    for bad in ('{"elnOrigin": "http://x"}', "[1]", "{broken"):
        path.write_text(bad, encoding="utf-8")
        origin, problem = read_saved_eln_origin(root)
        assert origin is None and str(path) in problem


def test_registry_summary_reads_without_changing_the_file(tmp_path):
    home = create_registry(tmp_path / "reg", batches=3)
    with RegistryService.open(home) as svc:
        svc.retire("000002_01_001", reason="wrong structure")
    location = install_host.locate_registry(home)
    db = Path(location.config.db_path)
    before = (db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in home.iterdir()))
    summary = registry_summary(location.config)
    assert summary == {"id_format": "{core:06d}_{stereo:02d}_{batch:03d}",
                       "example": "000001_01_001", "batches": 3, "retired": 1, "cores": 3,
                       "problem": None}
    assert (db.read_bytes(), db.stat().st_mtime_ns,
            sorted(p.name for p in home.iterdir())) == before
    db.write_bytes(b"not a database")
    broken = registry_summary(location.config)
    assert broken["batches"] is None and "could not be read" in broken["problem"]


def test_next_steps_for_each_browser(root):
    xpi = str(root / "extension" / "artifacts" / "compound-registry-9.8.7-firefox.xpi")
    firefox = "\n".join(next_steps(("firefox",), root=root))
    assert xpi in firefox and str(root / "extension" / "dist" / "manifest.json") in firefox
    assert "Firefox ESR, Developer Edition and Nightly install it for good" in firefox
    assert "xpinstall.signatures.required to false in about:config" in firefox
    assert "Ordinary Firefox (release, beta) refuses a file that Mozilla has not signed" in firefox
    assert "about:debugging#/runtime/this-firefox" in firefox
    assert "unless your group has the file signed" in firefox
    assert "chrome://" not in firefox and "MestReNova" not in firefox and "npm" not in firefox
    assert "Reload the ELN tab(s)" in firefox

    chrome = "\n".join(next_steps(("chrome",), root=root, mnova=True))
    assert "chrome://extensions" in chrome and "Developer mode" in chrome
    assert "Load unpacked" in chrome and str(root / "chrome-extension") in chrome
    assert "xpi" not in chrome and "about:debugging" not in chrome
    assert "Restart MestReNova" in chrome

    both = next_steps(("firefox", "chrome"), root=root, mnova=True)
    assert [line[:5] for line in both if line[2:3].isdigit()] == [
        "  1. ", "  2. ", "  3. ", "  4. "]


def test_next_steps_variants(root):
    upgrade = "\n".join(next_steps(("firefox", "chrome"), root=root, upgrade=True))
    assert "install the new extension file" in upgrade
    assert "click the reload arrow" in upgrade
    note = next_steps(("chrome",), root=root, build_note="Node.js was not found.\n- do this")
    assert note[0] == "  1. Build the extension - it was NOT built:"
    assert note[1:3] == ["     Node.js was not found.", "     - do this"]
    assert note[3].startswith("  2. Chrome (after step 1): open chrome://extensions")
    assert "downloaded" not in "\n".join(note)


def test_next_steps_when_the_extension_is_not_there(root):
    """Nothing was built: the steps must not send the user to files that do
    not exist, and must say what to do with a downloaded package."""
    xpi = root / "extension" / "artifacts" / "compound-registry-9.8.7-firefox.xpi"
    text = "\n".join(next_steps(("firefox", "chrome"), root=root, build_note="why",
                                download=True))
    assert "  2. Firefox (after step 1): install the extension file" in text
    assert ("     compound-registry-9.8.7-firefox.xpi that you downloaded, or, after a build,\n"
            f"     {xpi}") in text
    assert "'Load Temporary Add-on...' and choose\n       the same file" in text
    assert str(root / "extension" / "dist") not in text
    assert "  3. Chrome (after step 1): open chrome://extensions" in text
    assert ("With the downloaded compound-registry-9.8.7-chrome.zip: unpack it first, into a "
            "folder that\n     stays, and choose that folder.") in text

    upgrade = "\n".join(next_steps(("chrome",), root=root, build_note="why", download=True,
                                   upgrade=True))
    assert "stays (the folder Chrome has loaded the extension from, if it is loaded)" in upgrade

    own_eln = "\n".join(next_steps(("firefox", "chrome"), root=root, build_note="why"))
    assert "downloaded" not in own_eln and "(after step 1)" in own_eln and str(xpi) in own_eln
    assert str(root / "extension" / "dist") not in own_eln

    without_root = "\n".join(next_steps(("firefox", "chrome"), root=None, build_note="why",
                                        download=True))
    assert f"compound-registry-{__version__}-firefox.xpi that you downloaded" in without_root
    assert f"compound-registry-{__version__}-chrome.zip: unpack it first" in without_root
    assert "extension/artifacts" not in without_root and "after a build" not in without_root
    commands = "\n".join(next_steps(("firefox",), root=root, build_commands=True))
    assert "npm ci" in commands and "npm run package" in commands
    assert "package:chrome" not in commands and str(root / "extension") in commands
    without_root = "\n".join(next_steps(("firefox", "chrome"), root=None, build_commands=True))
    assert "extension/artifacts/compound-registry-<version>-firefox.xpi" in without_root
    assert "extension/dist/manifest.json" in without_root


# --------------------------------------------------------------------------- first installation

def test_first_installation_with_questions(tmp_path, target, registry, root):
    home = tmp_path / "env-home"
    #          folder  create  prefix  format-ok  browser  ELN  continue
    run = Run(target, root, "", "y", "ABC", "", "", "", "")
    assert run() == 0
    # 1. the folder question comes first, with the default in brackets
    assert run.script.prompts[0] == f"Data folder [{home}]: "
    assert "registry database, its backups and the compound files" in run.text
    assert "A NEW, EMPTY registry will be created there: its numbering starts at 1." in run.text
    assert "The format can NEVER be changed" in run.text
    assert run.script.asked("IDs will look like ABC000001_01_001. Use this format?")
    assert run.script.prompts[4] == "Browser - firefox, chrome or both [both]: "
    assert run.script.prompts[5] == f"ELN address [{DEFAULT_ELN_ORIGIN}]: "
    assert run.script.prompts[-1] == "Continue? [Y/n]: " and not run.script.answers
    assert not run.script.asked("MestReNova")              # not found: not asked
    # the registry, with the chosen format
    with RegistryService.open(home) as svc:
        assert svc.register(smiles="CCO", experiment_ref="1")["id"] == "ABC000001_01_001"
    # both browsers registered and pinned to the folder
    assert pinned(target) == str(home)
    assert registry.values == {REGISTRY_KEY: str(target.manifest_path),
                               CHROME_REGISTRY_KEY: str(target.chrome_manifest_path)}
    # the ELN address is saved for the build, and the build ran with it
    saved = json.loads((root / "extension" / "local.config.json").read_text(encoding="utf-8"))
    assert saved == {"elnOrigin": DEFAULT_ELN_ORIGIN}
    assert run.npm.npm == [
        ["ci"],
        ["run", "package", "--", "--force", "--eln-origin", DEFAULT_ELN_ORIGIN],
        ["run", "package:chrome", "--", "--force", "--eln-origin", DEFAULT_ELN_ORIGIN]]
    assert all(cwd == root / "extension" for _argv, cwd in run.npm.calls)
    # one list of what is left, and the reminder
    assert run.text.count("What to do now:") == 1
    assert str(root / "extension" / "artifacts" / "compound-registry-9.8.7-firefox.xpi") in run.text
    assert str(root / "chrome-extension") in run.text
    assert "update.ps1" in run.text and "update.sh" in run.text
    assert "Registry check" not in run.text               # a new registry has nothing to check


def test_an_existing_registry_is_shown_and_used(tmp_path, target, registry, root):
    home = create_registry(tmp_path / "data", batches=2)
    run = Run(target, root, f'"{home}{os.sep}"', "firefox", "", "")
    assert run() == 0
    assert f"A registry exists in {home} and will be used:" in run.text
    assert "ID format  {core:06d}_{stereo:02d}_{batch:03d}  (IDs look like 000001_01_001)" in run.text
    assert "Contents   2 ID(s) of 2 compound(s), 0 retired" in run.text
    assert "NEW, EMPTY" not in run.text and not run.script.asked("prefix")
    assert pinned(target) == str(home)
    assert registry.values == {REGISTRY_KEY: str(target.manifest_path)}
    assert run.npm.npm[-1][:2] == ["run", "package"] and len(run.npm.npm) == 2
    assert "Registry check: The chemistry version did not change" in run.text


def test_declining_the_creation_asks_for_the_folder_again(tmp_path, target, root):
    existing = create_registry(tmp_path / "data")
    run = Run(target, root, str(tmp_path / "typo"), "n", str(existing), "", "", "")
    assert run() == 0
    assert not (tmp_path / "typo").exists()
    assert [p for p in run.script.prompts if p.startswith("Data folder")].__len__() == 2
    assert pinned(target) == str(existing)


def test_folder_and_prefix_are_asked_again_until_usable(tmp_path, target, root):
    home = tmp_path / "data"
    run = Run(target, root, "relative/folder", str(home), "y", "A/B", "LAB-", "n", "", "", "", "",
              "")
    assert run() == 0
    assert "must be an absolute path" in run.text
    assert "cannot be used for a new registry" in run.text           # A/B
    assert run.script.asked("IDs will look like LAB-000001_01_001")  # declined, then no prefix
    assert run.script.asked("IDs will look like 000001_01_001")
    location = install_host.locate_registry(home)
    assert location.exists and location.config.id_format == "{core:06d}_{stereo:02d}_{batch:03d}"


def test_sync_folder_warning_is_shown_and_must_be_confirmed(tmp_path, target, root, monkeypatch):
    cloud = tmp_path / "OneDrive - Example"
    monkeypatch.setenv("OneDrive", str(cloud))
    local = create_registry(tmp_path / "local")
    run = Run(target, root, str(cloud / "reg"), "n", str(local), "", "", "")
    assert run() == 0
    assert "is inside OneDrive" in run.text
    assert run.script.prompts[1] == "Use this folder anyway? [y/N]: "
    assert not (cloud / "reg").exists() and pinned(target) == str(local)

    #                  folder             anyway create prefix ok switch browser ELN continue
    again = Run(target, root, str(cloud / "reg"), "y", "y", "", "", "y", "", "", "")
    assert again() == 0
    assert pinned(target) == str(cloud / "reg") and (cloud / "reg" / "config.toml").is_file()
    assert again.text.count("is inside OneDrive") == 1              # not repeated after creation

    # the folder the browsers use already: warned, not asked again
    known = Run(target, root, "", "", "", "")
    assert known() == 0
    assert "is inside OneDrive" in known.text and not known.script.asked("anyway")


# --------------------------------------------------------------------------- another registry

def test_switching_to_another_registry_needs_a_confirmation(tmp_path, target, root):
    a = create_registry(tmp_path / "A")
    b = create_registry(tmp_path / "B")
    install_host.install(a, target=target, browsers="both")
    refused = Run(target, root, str(b), "n", "", "", "", "")
    assert refused() == 0
    assert refused.script.prompts[0] == f"Data folder [{a}]: "        # default: the one in use
    assert "The browsers are set up for ANOTHER registry now:" in refused.text
    assert f"  now   {a}" in refused.text and f"  new   {b}" in refused.text
    assert refused.script.prompts[1] == "Switch the browsers to the new registry? [y/N]: "
    assert pinned(target) == str(a)

    switched = Run(target, root, str(b), "y", "", "", "")
    assert switched() == 0
    assert pinned(target) == str(b)
    assert f"and switch from the registry {a}" in switched.text


def test_a_home_option_is_still_confirmed_when_it_switches(tmp_path, target, root):
    a = create_registry(tmp_path / "A")
    b = create_registry(tmp_path / "B")
    install_host.install(a, target=target)
    run = Run(target, root, "y", "", "", "")
    assert run(home=str(b)) == 0
    assert not run.script.asked("Data folder") and run.script.asked("Switch the browsers")
    assert pinned(target) == str(b)


def test_chrome_alone_cannot_switch_so_both_are_registered(tmp_path, target, registry, root):
    a = create_registry(tmp_path / "A")
    b = create_registry(tmp_path / "B")
    install_host.install(a, target=target, browsers=("chrome",))
    run = Run(target, root, str(b), "y", "", "", "")
    assert run() == 0
    assert run.script.prompts[2] == "Browser - firefox, chrome or both [chrome]: "
    assert "Firefox is registered as well" in run.text
    assert pinned(target) == str(b) and set(registry.values) == {REGISTRY_KEY, CHROME_REGISTRY_KEY}


# --------------------------------------------------------------------------- --yes

def test_yes_never_creates_a_registry_by_itself(tmp_path, target, registry, root):
    before = snapshot(tmp_path, registry)
    run = Run(target, root)
    with pytest.raises(RegistryError) as info:
        run(yes=True)
    assert info.value.code == "setup_needs_answer"
    assert "--create-registry" in info.value.message and "Nothing was changed" in info.value.message
    assert snapshot(tmp_path, registry) == before and run.script.prompts == []
    assert run.npm.calls == []


def test_yes_with_the_options_of_a_first_installation(tmp_path, target, registry, root):
    home = tmp_path / "data"
    run = Run(target, root)
    assert run(yes=True, home=str(home), create_registry=True, id_prefix="XY",
               browser="chrome", eln_origin="https://eln.example.org:8443/") == 0
    assert run.script.prompts == []
    assert install_host.locate_registry(home).config.id_format == \
        "XY{core:06d}_{stereo:02d}_{batch:03d}"
    assert registry.values == {CHROME_REGISTRY_KEY: str(target.chrome_manifest_path)}
    assert run.npm.npm == [["ci"], ["run", "package:chrome", "--", "--force", "--eln-origin",
                                    "https://eln.example.org:8443"]]
    saved = json.loads((root / "extension" / "local.config.json").read_text(encoding="utf-8"))
    assert saved == {"elnOrigin": "https://eln.example.org:8443"}


def test_yes_takes_the_defaults(tmp_path, target, registry, root):
    create_registry(tmp_path / "env-home")
    run = Run(target, root, mnova=Mnova(found=True))
    assert run(yes=True) == 0
    assert run.script.prompts == [] and pinned(target) == str(tmp_path / "env-home")
    assert set(registry.values) == {REGISTRY_KEY, CHROME_REGISTRY_KEY}       # default: both
    assert run.mnova.installs == 0                 # found but not installed: default no


def test_yes_with_an_id_format_option(tmp_path, target, root):
    run = Run(target, root)
    assert run(yes=True, home=str(tmp_path / "f"), create_registry=True,
               id_format="LAB{core:04d}-{stereo:02d}-{batch:02d}", skip_extension=True) == 0
    assert install_host.locate_registry(tmp_path / "f").config.id_format == \
        "LAB{core:04d}-{stereo:02d}-{batch:02d}"


@pytest.mark.parametrize("options, part", [
    ({"id_prefix": "AB", "id_format": "{core:06d}_{stereo:02d}_{batch:03d}"}, "not both"),
    ({"id_prefix": "A/B"}, "cannot be used for a new registry"),
    ({"id_format": "{core}"}, ""),
    ({"browser": "edge"}, "Unknown browser"),
    ({"eln_origin": "http://eln.example.org"}, "https://"),
    ({"mnova": "maybe"}, "--mnova"),
    ({"home": "relative"}, "absolute path"),
    ({"home": "D:\\Lab Data\\Registry --id-prefix ABC"}, "an option seems to have become a part"),
])
def test_unusable_options_change_nothing(tmp_path, target, registry, root, options, part):
    before = snapshot(tmp_path, registry)
    run = Run(target, root)
    with pytest.raises(RegistryError) as info:
        run(**{"yes": True, "home": str(tmp_path / "data"), "create_registry": True, **options})
    assert part in info.value.message
    assert snapshot(tmp_path, registry) == before and run.npm.calls == []


def test_a_file_cannot_be_the_data_folder(tmp_path, target, registry, root):
    (tmp_path / "afile").write_text("x", encoding="utf-8")
    before = snapshot(tmp_path, registry)
    for home in (tmp_path / "afile", tmp_path / "afile" / "sub"):
        with pytest.raises(RegistryError) as info:
            Run(target, root)(yes=True, create_registry=True, home=str(home))
        assert info.value.code == "invalid_argument"
        assert f"{tmp_path / 'afile'} is a file, not a folder" in info.value.message
    assert snapshot(tmp_path, registry) == before
    # typed at the question: said, and asked again
    data = tmp_path / "data"
    run = Run(target, root, str(tmp_path / "afile"), str(data), "y", "", "", "", "", "")
    assert run() == 0
    assert "is a file, not a folder" in run.text and pinned(target) == str(data)


def test_a_registry_whose_database_is_missing(tmp_path, target, registry, root):
    """The database was moved or its drive is away: not 'a missing part',
    and no promise of a creation in front of a refusal."""
    home = create_registry(tmp_path / "data", batches=2)
    install_host.install(home, target=target)
    database = install_host.locate_registry(home).config.db_path
    Path(database).rename(tmp_path / "moved.sqlite")
    before = snapshot(tmp_path, registry)

    for options in ({"upgrade": True}, {"yes": True}):
        refused = Run(target, root)
        with pytest.raises(RegistryError) as info:
            refused(**options)
        assert info.value.code == "setup_needs_answer" and str(database) in info.value.message
        assert "will be created" not in refused.text and "is kept" not in refused.text
    assert snapshot(tmp_path, registry) == before

    #                    folder  create
    declined = Run(target, root, "", "n")
    with pytest.raises(RegistryError):
        declined()
    assert f"The DATABASE of the registry in {home} is missing" in declined.text
    assert "choose the folder it is in, or restore it from a backup" in declined.text
    assert "Creating it gives a NEW, EMPTY database" in declined.text
    assert declined.script.prompts[1] == "Create a new, empty database? [y/N]: "
    assert "will be created" not in declined.text and "missing part" not in declined.text
    assert snapshot(tmp_path, registry) == before

    #                   folder  create  browser  ELN  continue
    created = Run(target, root, "", "y", "", "", "")
    assert created() == 0
    assert "A new, empty database will be created; config.toml is kept." in created.text
    assert f"create a NEW, EMPTY database for the registry in {home}" in created.text
    assert f"A NEW, EMPTY database was created in {home}" in created.text
    assert not created.script.asked("prefix") and Path(database).is_file()


def test_a_database_without_its_settings_file(tmp_path, target, root):
    home = create_registry(tmp_path / "data", batches=2)
    (home / "config.toml").unlink()
    #               folder    write  browser  ELN  continue
    run = Run(target, root, str(home), "y", "", "", "")
    assert run() == 0
    assert "holds a registry database without its settings file" in run.text
    assert run.script.prompts[1] == (
        "Write a settings file with the defaults and use this database? [y/N]: ")
    assert "The settings file will be created; the database is kept." in run.text
    assert "NEW, EMPTY" not in run.text and "the database is used as it is" in run.text
    assert registry_summary(install_host.locate_registry(home).config)["batches"] == 2


def test_an_id_format_is_refused_for_an_existing_registry(tmp_path, target, registry, root):
    home = create_registry(tmp_path / "data")
    before = dict(registry.values)
    with pytest.raises(RegistryError) as info:
        Run(target, root)(yes=True, home=str(home), id_prefix="AB")
    assert info.value.code == "invalid_argument" and "cannot be changed" in info.value.message
    assert registry.values == before and not target.sidecar_path.exists()


def test_yes_switches_only_with_an_explicit_home(tmp_path, target, root, monkeypatch):
    a = create_registry(tmp_path / "A")
    b = create_registry(tmp_path / "B")
    install_host.install(a, target=target)
    kept = Run(target, root)
    assert kept(yes=True) == 0 and pinned(target) == str(a)
    switched = Run(target, root)
    assert switched(yes=True, home=str(b)) == 0 and pinned(target) == str(b)
    assert "ANOTHER registry" in switched.text


# --------------------------------------------------------------------------- --upgrade

def test_upgrade_needs_an_installation(tmp_path, target, registry, root):
    create_registry(tmp_path / "env-home")
    before = snapshot(tmp_path, registry)
    with pytest.raises(RegistryError) as info:
        Run(target, root)(upgrade=True)
    assert info.value.code == "setup_needs_answer" and "install.ps1" in info.value.message
    assert snapshot(tmp_path, registry) == before


def test_upgrade_keeps_every_previous_choice(tmp_path, target, registry, root):
    home = create_registry(tmp_path / "data", batches=1)
    install_host.install(home, target=target, browsers=("chrome",))
    (root / "extension" / "local.config.json").write_text(
        json.dumps({"elnOrigin": "https://eln.example.org"}), encoding="utf-8")
    config_before = (root / "extension" / "local.config.json").stat().st_mtime_ns
    run = Run(target, root, mnova=Mnova(found=True, installed=True))
    assert run(upgrade=True) == 0
    assert run.script.prompts == []
    assert pinned(target) == str(home)
    assert registry.values == {CHROME_REGISTRY_KEY: str(target.chrome_manifest_path)}
    assert run.npm.npm == [["ci"], ["run", "package:chrome", "--", "--force", "--eln-origin",
                                    "https://eln.example.org"]]
    assert (root / "extension" / "local.config.json").stat().st_mtime_ns == config_before
    assert run.mnova.installs == 1
    assert "The MestReNova add-on was brought up to date in the-scripts-folder." in run.text
    assert "Registry check: The chemistry version did not change" in run.text
    assert "click the reload arrow" in run.text and "Restart MestReNova" in run.text
    assert "xpi" not in run.text


def test_upgrade_leaves_an_add_on_that_is_not_installed(tmp_path, target, root):
    install_host.install(create_registry(tmp_path / "data"), target=target)
    run = Run(target, root, mnova=Mnova(found=True, installed=False))
    assert run(upgrade=True) == 0
    assert run.mnova.installs == 0 and "Restart MestReNova" not in run.text
    assert "install the new extension file" in run.text


def test_upgrade_with_a_missing_registry_changes_nothing(tmp_path, target, registry, root):
    home = create_registry(tmp_path / "data")
    install_host.install(home, target=target)
    home.rename(tmp_path / "moved")
    before = snapshot(tmp_path, registry)
    run = Run(target, root)
    with pytest.raises(RegistryError) as info:
        run(upgrade=True)
    assert info.value.code == "setup_needs_answer" and str(home) in info.value.message
    assert snapshot(tmp_path, registry) == before and not home.exists()


def test_a_blocked_chemistry_check_is_reported_and_does_not_fail(tmp_path, target, root):
    home = create_registry(tmp_path / "data")
    install_host.install(home, target=target)
    asked = []

    def blocked(folder):
        asked.append(folder)
        return {"status": "blocked", "message": "2 structure(s) would get other keys."}

    run = Run(target, root, upgrade_registry=blocked)
    assert run(upgrade=True) == 0
    assert asked == [str(home)]
    assert "Registry check: 2 structure(s) would get other keys." in run.text

    def broken(folder):
        raise RegistryError("the database is locked", code="storage_error")

    failing = Run(target, root, upgrade_registry=broken)
    assert failing(upgrade=True) == 0
    assert "warning: the registry could not be checked: the database is locked" in failing.text


# --------------------------------------------------------------------------- --dry-run

def test_dry_run_changes_nothing(tmp_path, target, registry, root):
    before = snapshot(tmp_path, registry)
    run = Run(target, root, mnova=Mnova(found=True))
    assert run(dry_run=True, yes=True, home=str(tmp_path / "data"), create_registry=True,
               id_prefix="AB", mnova="yes", eln_origin="https://eln.example.org") == 0
    assert snapshot(tmp_path, registry) == before
    assert run.npm.calls == [] and run.mnova.installs == 0
    text = run.text
    assert "(dry run: nothing is changed)" in text and "Dry run: nothing was changed." in text
    assert f"Would create {tmp_path / 'data' / 'config.toml'}" in text
    assert f"Would write {target.sidecar_path}" in text
    assert f"Would write {target.manifest_path}" in text
    assert f"Would write {target.chrome_manifest_path}" in text
    assert "Would write registry key HKCU\\Software\\Mozilla\\NativeMessagingHosts" in text
    assert "Would write registry key HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts" in text
    assert f"Would write {root / 'extension' / 'local.config.json'}" in text
    assert f"Would run in {root / 'extension'}: npm ci" in text
    assert "npm run package -- --force --eln-origin https://eln.example.org" in text
    assert "npm run package:chrome -- --force --eln-origin https://eln.example.org" in text
    assert f"Would write {root / 'chrome-extension'}" in text
    assert "Would install the MestReNova add-on" in text
    assert "What to do now" not in text


def test_dry_run_of_an_upgrade_does_not_open_the_registry(tmp_path, target, registry, root):
    home = create_registry(tmp_path / "data")
    install_host.install(home, target=target)
    before = snapshot(tmp_path, registry)
    run = Run(target, root, node=False,
              upgrade_registry=lambda folder: pytest.fail("must not be checked in a dry run"))
    assert run(dry_run=True, upgrade=True) == 0
    assert snapshot(tmp_path, registry) == before
    assert "Would NOT build the extension: Node.js (node and npm) was not found." in run.text
    assert f"Would check the stored structures of the registry in {home}" in run.text


def test_dry_run_with_questions_asks_no_final_confirmation(tmp_path, target, registry, root):
    create_registry(tmp_path / "env-home")
    before = snapshot(tmp_path, registry)
    run = Run(target, root, "", "", "")
    assert run(dry_run=True) == 0
    assert not run.script.asked("Continue?") and snapshot(tmp_path, registry) == before


# --------------------------------------------------------------------------- the extension build

def test_without_nodejs_the_installation_still_succeeds(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    run = Run(target, root, node=False)
    assert run(yes=True) == 0
    assert target.manifest_path.is_file() and run.npm.calls == []
    assert "Build the extension - it was NOT built:" in run.text
    assert "Node.js was not found." in run.text
    assert "Install Node.js (the LTS version, https://nodejs.org), then run the installer" in run.text
    assert f"download the ready-made extension packages of version {__version__}" in run.text
    assert "https://github.com/smalljet-lab/compound-registry/releases" in run.text
    assert "compound-registry-9.8.7-firefox.xpi that you downloaded" in run.text
    assert "With the downloaded compound-registry-9.8.7-chrome.zip: unpack it first" in run.text
    assert str(root / "extension" / "dist") not in run.text


def test_ready_made_packages_are_only_offered_for_the_default_address(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    run = Run(target, root, node=False)
    assert run(yes=True, eln_origin="https://eln.example.org") == 0
    assert "Install Node.js" in run.text
    assert "download the ready-made" not in run.text
    assert "cannot be used: they are built" in run.text
    assert "downloaded" not in run.text and "Firefox (after step 1)" in run.text


def test_an_old_nodejs_is_explained(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    run = Run(target, root, npm=Npm(version="v18.20.4"))
    assert run(yes=True) == 0
    assert run.npm.npm == []
    assert "Node.js v18.20.4 is too old" in run.text and "22.12 or newer" in run.text
    assert "firefox.xpi that you downloaded" in run.text


def test_a_failing_build_is_reported_with_exit_code_1(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    run = Run(target, root, npm=Npm(fail="package"), mnova=Mnova(found=True, installed=True))
    assert run(yes=True) == 1
    assert target.manifest_path.is_file()                # the host is registered all the same
    assert "npm ERR! it broke" in run.text
    assert "'npm run package -- --force --eln-origin" in run.text and "failed" in run.text
    assert run.npm.npm[-1][1] == "package"               # the Chrome build was not started
    assert run.mnova.installs == 1 and "What to do now:" in run.text
    assert "downloaded" not in run.text and "Firefox (after step 1)" in run.text


def test_npm_ci_only_when_the_lock_file_is_newer(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    marker = root / "extension" / "node_modules" / ".package-lock.json"
    marker.parent.mkdir()
    marker.write_text("{}", encoding="utf-8")
    lock = root / "extension" / "package-lock.json"
    os.utime(lock, (1_000_000, 1_000_000))
    fresh = Run(target, root)
    assert fresh(yes=True, browser="firefox") == 0
    assert [c[0] for c in fresh.npm.npm] == ["run"]
    os.utime(marker, (500_000, 500_000))
    stale = Run(target, root)
    assert stale(yes=True, browser="firefox") == 0
    assert [c[0] for c in stale.npm.npm] == ["ci", "run"]


def test_skip_extension(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    run = Run(target, root, "", "", "")
    assert run(skip_extension=True) == 0
    assert run.npm.calls == [] and not run.script.asked("ELN address")
    assert "The extension was not built (--skip-extension)." in run.text
    assert not (root / "extension" / "local.config.json").exists()
    assert "Build the extension - it was NOT built:" in run.text
    assert "Run the installer again without --skip-extension." in run.text
    assert "Firefox (after step 1)" in run.text and "downloaded" not in run.text
    saved = Run(target, root)
    assert saved(yes=True, skip_extension=True, eln_origin="https://eln.example.org") == 0
    assert read_saved_eln_origin(root) == ("https://eln.example.org", None)
    assert saved.npm.calls == []
    # built earlier (by hand, or by a run before): the steps name what is there
    paths = setup_wizard.extension_paths(root)
    for built in (paths["firefox_package"], paths["chrome_folder"] / "manifest.json"):
        built.parent.mkdir(parents=True, exist_ok=True)
        built.write_text("x", encoding="utf-8")
    there = Run(target, root)
    assert there(yes=True, skip_extension=True) == 0
    assert "NOT built" not in there.text and "(after step 1)" not in there.text
    assert "  1. Firefox: install the extension file" in there.text


def test_without_a_project_folder_nothing_is_built(tmp_path, target):
    create_registry(tmp_path / "env-home")
    run = Run(target, None, "", "", "")
    assert run() == 0
    assert run.npm.calls == [] and not run.script.asked("ELN address")
    assert "does not run from its project folder" in run.text
    assert f"compound-registry-{__version__}-firefox.xpi that you downloaded" in run.text
    assert "extension/artifacts" not in run.text


def test_the_eln_question(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    (root / "extension" / "local.config.json").write_text(
        json.dumps({"elnOrigin": "https://old.example.org"}), encoding="utf-8")
    run = Run(target, root, "", "", "eln.example.org", "https://eln.example.org/x",
              "https://new.example.org", "")
    assert run() == 0
    assert run.script.prompts[2] == "ELN address [https://old.example.org]: "
    assert "it must start with https://" in run.text and "must not contain a path" in run.text
    assert read_saved_eln_origin(root) == ("https://new.example.org", None)
    assert run.npm.npm[-1][-1] == "https://new.example.org"


@pytest.mark.parametrize("content", ["{broken", '{"elnOrigin": "https://eln.example.org/path"}'])
def test_an_unusable_saved_address_is_never_replaced_silently(tmp_path, target, registry, root,
                                                              content):
    """A run without questions must not build the extension of a group with
    its own ELN for the default address."""
    install_host.install(create_registry(tmp_path / "env-home"), target=target)
    saved = root / "extension" / "local.config.json"
    saved.write_text(content, encoding="utf-8")
    before = snapshot(tmp_path, registry)
    for options in ({"upgrade": True}, {"yes": True}, {"yes": True, "dry_run": True}):
        run = Run(target, root)
        with pytest.raises(RegistryError) as info:
            run(**options)
        assert info.value.code == "setup_needs_answer"
        assert "The saved ELN address is not usable" in info.value.message
        assert str(saved) in info.value.message and "--eln-origin" in info.value.message
        assert run.npm.calls == []
    assert snapshot(tmp_path, registry) == before

    # nothing is built: the file is left alone, with a warning
    skipped = Run(target, root)
    assert skipped(upgrade=True, skip_extension=True) == 0
    assert "warning: the saved ELN address is not usable" in skipped.text
    assert saved.read_text(encoding="utf-8") == content

    # with questions: a warning, and the address is asked
    asked = Run(target, root, "", "", "https://eln.example.org", "")
    assert asked() == 0
    assert "warning: the saved ELN address is not usable" in asked.text
    assert asked.script.asked(f"ELN address [{DEFAULT_ELN_ORIGIN}]")
    assert read_saved_eln_origin(root) == ("https://eln.example.org", None)

    # the option answers it
    saved.write_text(content, encoding="utf-8")
    given = Run(target, root)
    assert given(upgrade=True, eln_origin="https://other.example.org") == 0
    assert read_saved_eln_origin(root) == ("https://other.example.org", None)
    assert given.npm.npm[-1][-1] == "https://other.example.org"


# --------------------------------------------------------------------------- MestReNova

def test_mnova_is_asked_only_where_it_was_found(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    absent = Run(target, root, "", "", "", "", mnova=Mnova(found=False))
    assert absent() == 0 and not absent.script.asked("MestReNova")

    found = Run(target, root, "", "", "", "y", "", mnova=Mnova(found=True))
    assert found() == 0
    assert found.script.prompts[3] == "Install the MestReNova add-on? [y/N]: "
    assert "MestReNova 15.0.1 was found on this PC" in found.text
    assert found.mnova.installs == 1
    assert "The MestReNova add-on was installed in the-scripts-folder." in found.text
    assert "Restart MestReNova" in found.text

    declined = Run(target, root, "", "", "", "", "", mnova=Mnova(found=True))
    assert declined() == 0 and declined.mnova.installs == 0
    assert "Restart MestReNova" not in declined.text

    installed = Run(target, root, "", "", "", "", "", mnova=Mnova(found=True, installed=True))
    assert installed() == 0
    assert installed.script.prompts[3] == "Bring the MestReNova add-on up to date? [Y/n]: "
    assert installed.mnova.installs == 1


def test_mnova_options(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    no = Run(target, root, "", "", "", "", mnova=Mnova(found=True, installed=True))
    assert no(mnova="no") == 0
    assert not no.script.asked("MestReNova") and no.mnova.installs == 0

    yes = Run(target, root, mnova=Mnova(found=False))
    assert yes(yes=True, mnova="yes") == 0 and yes.mnova.installs == 1

    unsupported = Run(target, root, mnova=Mnova(supported=False))
    assert unsupported(yes=True, mnova="yes") == 0 and unsupported.mnova.installs == 0
    assert "on Windows only" in unsupported.text

    elsewhere = Run(target, root, "", "", "", "", mnova=Mnova(supported=False, found=True))
    assert elsewhere() == 0 and not elsewhere.script.asked("MestReNova")


def test_a_failing_add_on_installation_does_not_stop_the_rest(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    run = Run(target, root, mnova=Mnova(found=True, installed=True, fail=True))
    assert run(yes=True) == 0
    assert "warning: the MestReNova add-on was not installed: the script folder is read-only" \
        in run.text
    assert "What to do now:" in run.text and "Restart MestReNova" not in run.text

    def boom():
        raise OSError("no access")

    unknown = Run(target, root)
    unknown.env.mnova_status = boom
    assert unknown(yes=True) == 0
    assert "MestReNova could not be looked for" in unknown.text


# --------------------------------------------------------------------------- other installations

def test_a_registration_of_another_installation(tmp_path, target, registry, root):
    home = create_registry(tmp_path / "env-home")
    other = make_target(tmp_path, registry, venv="other-venv")
    install_host.install(home, target=other)
    before = snapshot(tmp_path, registry)

    with pytest.raises(RegistryError) as info:
        Run(target, root)(yes=True, browser="firefox")
    assert info.value.code == "host_install_conflict" and "--force" in info.value.message
    assert snapshot(tmp_path, registry) == before

    declined = Run(target, root, "", "firefox", "n")
    with pytest.raises(RegistryError) as info:
        declined()
    assert info.value.code == "host_install_conflict"
    assert declined.script.prompts[-1] == "Let this installation take over? [y/N]: "
    assert str(other.launcher) in declined.text and snapshot(tmp_path, registry) == before

    accepted = Run(target, root, "", "firefox", "y", "", "")
    assert accepted() == 0
    manifest = json.loads(target.manifest_path.read_text(encoding="utf-8"))
    assert manifest["path"] == str(target.launcher)
    assert "replacing the registration of another installation" in accepted.text

    install_host.install(home, target=other, force=True)
    forced = Run(target, root)
    assert forced(yes=True, browser="firefox", force=True) == 0
    assert json.loads(target.manifest_path.read_text(encoding="utf-8"))["path"] == \
        str(target.launcher)


def test_the_host_program_must_exist(tmp_path, registry, root):
    target = default_target(platform="windows", prefix=tmp_path / "venv",
                            user_home=tmp_path / "user", registry=registry)
    with pytest.raises(RegistryError) as info:
        Run(target, root)(yes=True)
    assert info.value.code == "host_launcher_missing"


# --------------------------------------------------------------------------- nobody to ask

def test_without_a_terminal_it_fails_instead_of_waiting(tmp_path, target, registry, root):
    create_registry(tmp_path / "env-home")
    before = snapshot(tmp_path, registry)
    run = Run(target, root, can_ask=False)
    with pytest.raises(RegistryError) as info:
        run()
    assert info.value.code == "setup_needs_answer"
    assert "--yes" in info.value.message and "--upgrade" in info.value.message
    assert run.script.prompts == [] and snapshot(tmp_path, registry) == before
    assert Run(target, root, can_ask=False)(yes=True) == 0


def test_input_that_ends_or_a_no_at_the_end_changes_nothing(tmp_path, target, registry, root):
    before = snapshot(tmp_path, registry)
    ended = Run(target, root, "", "y")                     # the input ends at the prefix
    with pytest.raises(RegistryError) as info:
        ended()
    assert info.value.code == "cancelled" and snapshot(tmp_path, registry) == before

    stopped = Run(target, root, "", "y", "", "", "", "", "n")
    with pytest.raises(RegistryError) as info:
        stopped()
    assert info.value.code == "cancelled" and "before anything was changed" in info.value.message
    assert snapshot(tmp_path, registry) == before and stopped.npm.calls == []


def test_yes_or_no_questions_repeat_until_answered(tmp_path, target, root):
    create_registry(tmp_path / "env-home")
    run = Run(target, root, "", "x", "ch", "", "perhaps", "yes")
    assert run() == 0
    assert "Please answer firefox, chrome or both." in run.text
    assert "Please answer y or n." in run.text
    assert not target.manifest_path.exists() and target.chrome_manifest_path.is_file()


def test_a_system_without_a_chrome_folder_sets_up_firefox(tmp_path, registry, root):
    create_registry(tmp_path / "env-home")
    full = make_target(tmp_path, registry, platform="linux")
    target = HostTarget(full.platform, full.manifest_dir, full.launcher)
    run = Run(target, root, "", "", "")
    assert run() == 0
    assert not run.script.asked("Browser") and target.manifest_path.is_file()
    with pytest.raises(RegistryError) as info:
        Run(target, root)(yes=True, browser="both")
    assert "Chrome cannot be set up" in info.value.message


def test_on_linux_the_browser_question_names_google_chrome(tmp_path, registry, root):
    create_registry(tmp_path / "env-home")
    target = make_target(tmp_path, registry, platform="linux")
    run = Run(target, root, "", "", "", "")
    assert run() == 0
    assert "Chrome means Google Chrome" in run.text
    assert target.manifest_path.is_file() and target.chrome_manifest_path.is_file()
    assert registry.values == {}


# --------------------------------------------------------------------------- the command

@pytest.fixture
def command(monkeypatch, capsys, target, root):
    monkeypatch.setattr(install_host, "default_target", lambda **kw: target)
    monkeypatch.setattr(setup_wizard, "find_project_root", lambda: root)
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup_wizard, "_mnova_status",
                        lambda: {"supported": False, "installed": False,
                                 "mestrenova": {"found": False}})
    for name in ("_run_command", "_mnova_install"):
        monkeypatch.setattr(setup_wizard, name,
                            lambda *a, **kw: pytest.fail("must not be started in tests"))

    def _run(*argv: str):
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def test_command_dry_run(command, tmp_path, target, registry, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: False)
    before = snapshot(tmp_path, registry)
    code, out, err = command("setup", "--dry-run", "--yes", "--home", str(tmp_path / "data"),
                             "--create-registry", "--id-prefix", "AB", "--browser", "both")
    assert code == 0, err
    out.encode("ascii")
    assert "Would create" in out and "Dry run: nothing was changed." in out
    assert snapshot(tmp_path, registry) == before


def test_command_installs(command, tmp_path, target, registry, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: False)
    home = tmp_path / "data"
    code, out, err = command("setup", "--yes", "--home", str(home), "--create-registry",
                             "--browser", "firefox", "--eln-origin", "https://eln.example.org")
    assert code == 0, err
    assert "A NEW, EMPTY registry was created" in out and "What to do now:" in out
    assert pinned(target) == str(home) and REGISTRY_KEY in registry.values
    assert "Node.js was not found." in out
    # the low-level commands still work on the same installation
    code, out, _ = command("host-status", "--browser", "firefox")
    assert code == 0 and f"Extension home  {home} (host configuration)" in out
    code, out, _ = command("install-host")
    assert code == 0 and f"Keeping the registry {home}" in out


def test_command_refuses_to_wait_without_a_terminal(command, tmp_path, registry, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: False)
    before = snapshot(tmp_path, registry)
    code, out, err = command("setup")
    assert code == 1 and "error [setup_needs_answer]" in err and "--yes" in err
    assert snapshot(tmp_path, registry) == before
    code, out, _err = command("--json", "setup", "--yes")
    assert code == 1 and json.loads(out)["error"]["code"] == "invalid_argument"
    assert snapshot(tmp_path, registry) == before


def test_command_help_lists_every_option(command):
    code, out, _ = command("setup", "--help")
    assert code == 0
    for option in ("--home", "--browser", "--eln-origin", "--id-prefix", "--id-format",
                   "--create-registry", "--mnova", "--skip-extension", "--force", "--yes",
                   "--upgrade", "--dry-run"):
        assert option in out
    code, out, _ = command("--help")
    assert "setup" in out and "host-status" in out
