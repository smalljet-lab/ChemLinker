"""CLI commands install-host / uninstall-host / host-status and the native_host
part of doctor.

``install_host.default_target`` is replaced by a target in ``tmp_path`` with a
fake registry, so nothing touches the real HKCU, %LOCALAPPDATA% or the
Mozilla folders.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compound_registry import __version__, cli, install_host
from compound_registry.cli import main
from compound_registry.install_host import REGISTRY_KEY, default_target, manifest_document
from compound_registry.service import RegistryService


class FakeRegistry:
    def __init__(self):
        self.values: dict[str, str] = {}

    def read_default(self, key):
        return self.values.get(key)

    def write_default(self, key, value):
        self.values[key] = value

    def delete_key(self, key):
        return self.values.pop(key, None) is not None


def create_registry(path: Path, batches: int = 0) -> Path:
    with RegistryService.open(path) as svc:
        for i in range(batches):
            svc.register(smiles="C" * (i + 1) + "O", experiment_ref=f"E{i}")
    return path


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture(params=["windows", "linux"])
def target(request, tmp_path, registry):
    t = default_target(platform=request.param, prefix=tmp_path / "venv",
                       user_home=tmp_path / "user", registry=registry)
    t.launcher.parent.mkdir(parents=True)
    t.launcher.write_bytes(b"launcher")
    return t


@pytest.fixture
def env_home(tmp_path) -> Path:
    return create_registry(tmp_path / "env-home")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path, request):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata-env"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)

    def forbidden(*args, **kwargs):
        pytest.fail("the real Windows registry must not be used in tests")

    for name in ("read_default", "write_default", "delete_key"):
        monkeypatch.setattr(install_host.WindowsRegistry, name, forbidden)
    if "target" in request.fixturenames:
        t = request.getfixturevalue("target")
        monkeypatch.setattr(install_host, "default_target", lambda **kw: t)
    else:
        monkeypatch.setattr(install_host, "default_target",
                            lambda **kw: pytest.fail("default_target not patched"))


@pytest.fixture
def run(capsys):
    def _run(*argv: str):
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def assert_ascii(text: str) -> None:
    text.encode("ascii")


PROJECT_ROOT = Path(cli.__file__).resolve().parents[2]


# --------------------------------------------------------------------------- install-host

def test_install_host_human_output(run, target, registry, tmp_path):
    create_registry(tmp_path / "env-home", batches=2)
    code, out, err = run("install-host")
    assert code == 0, err
    assert_ascii(out)
    assert str(target.manifest_path) in out and str(target.launcher) in out
    assert f"Extension uses  {tmp_path / 'env-home'}" in out
    assert "Contents        2 batch(es) (0 retired), integrity ok" in out
    assert "Next steps" in out and "next request" in out
    assert "restart" not in out.lower()
    assert "about:debugging#/runtime/this-firefox" in out
    assert out.count("Next steps") == 1
    assert "Load Temporary Add-on" in out
    assert str(PROJECT_ROOT / "extension" / "dist" / "manifest.json") in out
    assert "xpinstall.signatures.required" in out and "Install Add-on From File" in out
    assert str(PROJECT_ROOT / "extension" / "artifacts"
               / f"chemlinker-{__version__}-firefox.xpi") in out
    # which Firefox editions keep an unsigned file, and what ordinary Firefox does
    assert "Firefox ESR, Developer Edition and Nightly install it for good" in out
    assert "Ordinary Firefox (release, beta) refuses a file that Mozilla has not signed" in out
    assert "npm ci" in out and "npm run package" in out and "package:chrome" not in out
    assert "NEW, EMPTY" not in out
    assert "COMPOUND_REGISTRY_HOME is set" in out
    if target.platform == "windows":
        assert registry.values == {REGISTRY_KEY: str(target.manifest_path)}
        assert "HKCU\\Software\\Mozilla\\NativeMessagingHosts\\compound_registry" in out


@pytest.mark.parametrize("before", [True, False])
def test_install_host_home_option_before_or_after(run, target, tmp_path, before):
    chosen = create_registry(tmp_path / "chosen")
    argv = (["--home", str(chosen), "install-host"] if before
            else ["install-host", "--home", str(chosen)])
    code, out, err = run(*argv)
    assert code == 0, err
    assert json.loads(target.sidecar_path.read_text(encoding="utf-8")) == {"home": str(chosen)}
    assert f"Extension uses  {chosen}" in out and "host configuration" in out


def test_install_host_again_keeps_the_pinned_registry(run, target, tmp_path, env_home):
    chosen = create_registry(tmp_path / "chosen")
    assert run("--home", str(chosen), "install-host")[0] == 0
    code, out, _ = run("install-host")
    assert code == 0 and target.sidecar_path.exists()
    assert f"Keeping the registry {chosen}" in out and f"Extension uses  {chosen}" in out


def test_install_host_default_home_removes_the_pin(run, target, tmp_path, env_home):
    assert run("install-host", "--home", str(create_registry(tmp_path / "chosen")))[0] == 0
    code, out, _ = run("install-host", "--default-home")
    assert code == 0 and not target.sidecar_path.exists()
    assert "Removed the host configuration" in out
    assert f"Extension uses  {env_home}" in out


@pytest.mark.parametrize("argv", [
    ["install-host", "--home", "X", "--default-home"],
    ["--home", "X", "install-host", "--default-home"],
])
def test_home_and_default_home_are_a_usage_error(run, target, registry, argv):
    code, _, err = run(*argv)
    assert code == 2 and "--default-home" in err
    assert not target.manifest_path.exists() and registry.values == {}


def test_install_host_refuses_a_missing_registry(run, target, registry, tmp_path):
    code, out, err = run("install-host", "--home", str(tmp_path / "missing"))
    assert code == 1 and "error [registry_missing]" in err and "--create-registry" in err
    assert not target.manifest_path.exists() and registry.values == {}
    assert not (tmp_path / "missing").exists()


def test_install_host_create_registry(run, target, tmp_path):
    new = tmp_path / "new"
    code, out, err = run("install-host", "--home", str(new), "--create-registry")
    assert code == 0, err
    assert f"A NEW, EMPTY registry was created in {new}" in out
    assert (new / "registry.sqlite").is_file()


@pytest.mark.parametrize("answer,expected", [("y", 0), ("n", 1), ("", 1)])
def test_install_host_asks_before_creating(run, target, registry, tmp_path, monkeypatch,
                                            answer, expected):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or answer)
    new = tmp_path / "asked"
    code, out, err = run("install-host", "--home", str(new))
    assert code == expected and len(prompts) == 1 and "[y/N]" in prompts[0]
    assert "NEW, EMPTY" in out and str(new) in out
    if expected == 0:
        assert (new / "registry.sqlite").is_file() and target.manifest_path.is_file()
    else:
        assert "error [registry_missing]" in err
        assert not new.exists() and not target.manifest_path.exists() and registry.values == {}


def test_install_host_json_never_prompts(run, target, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("prompted in --json"))
    code, out, _ = run("--json", "install-host", "--home", str(tmp_path / "x"))
    assert code == 1 and json.loads(out)["error"]["code"] == "registry_missing"


def test_install_host_json(run, target, tmp_path):
    chosen = create_registry(tmp_path / "Régistry")
    code, out, _ = run("--json", "install-host", "--home", str(chosen))
    assert code == 0
    assert_ascii(out)
    doc = json.loads(out)
    assert doc["home_override"] == str(chosen)
    assert doc["manifest_path"] == str(target.manifest_path)
    assert doc["registry"]["batches"] == 0 and doc["registry"]["integrity"] == "ok"
    assert doc["manifest"]["allowed_extensions"] == ["compound-registry@registry.local"]


def test_install_host_non_ascii_path_is_escaped(run, target, tmp_path):
    code, out, _ = run("install-host", "--home", str(create_registry(tmp_path / "Régistry")))
    assert code == 0
    assert_ascii(out)
    assert "R\\xe9gistry" in out


def test_install_host_relative_home(run, target, registry):
    code, out, err = run("install-host", "--home", "relative-registry")
    assert code == 1 and "absolute" in err
    assert not target.manifest_path.exists() and registry.values == {}
    assert not Path("relative-registry").exists()


def test_install_host_missing_launcher_json(run, target, env_home):
    target.launcher.unlink()
    code, out, _ = run("install-host", "--json")
    assert code == 1
    assert json.loads(out)["error"]["code"] == "host_launcher_missing"


def _register_other_installation(target, tmp_path, registry) -> Path:
    other = tmp_path / "other-venv" / target.launcher.relative_to(tmp_path / "venv")
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other")
    target.manifest_dir.mkdir(parents=True, exist_ok=True)
    target.manifest_path.write_text(json.dumps(manifest_document(other)), encoding="utf-8")
    if target.platform == "windows":
        registry.values[REGISTRY_KEY] = str(target.manifest_path)
    return other


def test_install_host_conflict_and_force(run, target, registry, tmp_path, env_home):
    other = _register_other_installation(target, tmp_path, registry)
    code, _, err = run("install-host")
    assert code == 1 and "error [host_install_conflict]" in err and "--force" in err
    assert json.loads(target.manifest_path.read_text(encoding="utf-8"))["path"] == str(other)
    code, out, err = run("install-host", "--force")
    assert code == 0, err
    assert str(other) in out    # named in the warnings
    assert json.loads(target.manifest_path.read_text(encoding="utf-8"))["path"] == str(target.launcher)


def test_install_host_help(run):
    code, out, _ = run("install-host", "--help")
    assert code == 0
    for flag in ("--home", "--default-home", "--create-registry", "--force"):
        assert flag in out
    code, out, _ = run("--help")
    assert code == 0 and "install-host" in out and "uninstall-host" in out


# --------------------------------------------------------------------------- uninstall-host

def test_uninstall_host(run, target, registry, tmp_path):
    run("install-host", "--home", str(create_registry(tmp_path / "chosen")))
    code, out, err = run("uninstall-host")
    assert code == 0, err
    assert str(target.manifest_path) in out and str(target.sidecar_path) in out
    assert "was not touched" in out
    assert not target.manifest_path.exists() and registry.values == {}
    code, out, _ = run("uninstall-host", "--json")
    assert code == 0 and json.loads(out)["removed"] == []


def test_uninstall_host_conflict_and_force(run, target, registry, tmp_path):
    _register_other_installation(target, tmp_path, registry)
    before = dict(registry.values)
    code, _, err = run("uninstall-host")
    assert code == 1 and "error [host_install_conflict]" in err
    assert target.manifest_path.is_file() and registry.values == before
    code, _, err = run("uninstall-host", "--force")
    assert code == 0, err
    assert not target.manifest_path.exists() and registry.values == {}


# --------------------------------------------------------------------------- doctor

def test_doctor_reports_the_host_without_changing_the_exit_code(run, target, tmp_path, env_home):
    home = create_registry(tmp_path / "reg")
    code, out, _ = run("--home", str(home), "doctor")
    assert code == 0
    assert "Browser host    NOT registered with Firefox or Chrome" in out
    assert "install-host --browser firefox|chrome|both" in out
    assert "Firefox host" not in out and "Chrome host" not in out
    code, out, _ = run("--home", str(home), "--json", "doctor")
    doc = json.loads(out)
    assert code == 0 and doc["native_host"]["registered"] is False
    assert doc["native_host"]["problems"] and doc["integrity"] == "ok"
    assert doc["native_host_chrome"]["registered"] is False
    assert doc["native_host_chrome"]["browser"] == "chrome"


def test_doctor_with_chrome_only(run, target, tmp_path):
    """A Chrome-only installation is not told that the host is not registered."""
    home = create_registry(tmp_path / "reg")
    assert run("install-host", "--browser", "chrome", "--home", str(home))[0] == 0
    code, out, _ = run("--home", str(home), "doctor")
    assert code == 0
    assert f"Chrome host     registered, manifest {target.chrome_manifest_path}" in out
    assert f"Extension home  {home} (host configuration)" in out
    assert "NOT registered" not in out and "Firefox host" not in out
    assert "DIFFERENT registry" not in out and "warning:" not in out
    doc = json.loads(run("--home", str(home), "--json", "doctor")[1])
    assert doc["native_host"]["registered"] is False
    assert doc["native_host_chrome"]["registered"] is True
    assert doc["native_host_chrome"]["problems"] == []


def test_doctor_with_both_browsers(run, target, tmp_path):
    home = create_registry(tmp_path / "reg")
    assert run("install-host", "--browser", "both", "--home", str(home))[0] == 0
    code, out, _ = run("--home", str(home), "doctor")
    assert code == 0
    assert "Firefox host    registered" in out and "Chrome host     registered" in out
    assert "NOT registered" not in out and "warning:" not in out


def test_doctor_names_the_default_registry_without_host_config(run, target, env_home):
    assert run("install-host")[0] == 0
    code, out, _ = run("--home", str(env_home), "doctor")
    assert code == 0
    assert f"Extension home  {env_home} (default registry as Firefox sees it" in out
    assert "DIFFERENT registry" not in out and "warning:" not in out


def test_doctor_with_a_pinned_registry(run, target, tmp_path):
    home = create_registry(tmp_path / "reg")
    other = create_registry(tmp_path / "other")
    run("install-host", "--home", str(other))
    code, out, _ = run("--home", str(home), "doctor")
    assert code == 0
    assert "Firefox host    registered" in out
    assert f"Extension home  {other} (host configuration)" in out
    assert "DIFFERENT registry" in out
    run("install-host", "--home", str(home))
    code, out, _ = run("--home", str(home), "doctor")
    assert "DIFFERENT registry" not in out and "warning:" not in out
    doc = json.loads(run("--home", str(home), "--json", "doctor")[1])
    assert doc["native_host"]["problems"] == []
    assert doc["native_host"]["home_override"] == str(home)
    assert doc["native_host"]["extension_home"] == str(home)


def test_doctor_warns_when_the_extension_registry_is_missing(run, target, tmp_path):
    pinned = create_registry(tmp_path / "pinned")
    run("install-host", "--home", str(pinned))
    pinned.rename(tmp_path / "moved")
    code, out, _ = run("--home", str(tmp_path / "moved"), "doctor")
    assert code == 0 and "No registry was found" in out
    assert not pinned.exists()


# --------------------------------------------------------------------------- --browser

def test_install_host_chrome(run, target, registry, tmp_path):
    from compound_registry.install_host import CHROME_REGISTRY_KEY
    from compound_registry.protocol import CHROME_ORIGIN

    chosen = create_registry(tmp_path / "chosen")
    code, out, err = run("install-host", "--browser", "chrome", "--home", str(chosen))
    assert code == 0, err
    assert_ascii(out)
    assert "registered with Chrome for the current user" in out
    assert f"Chrome manifest {target.chrome_manifest_path}" in out
    assert f"Extension uses  {chosen}" in out
    assert "npm run package:chrome" in out and "chrome://extensions" in out
    assert str(PROJECT_ROOT / "chrome-extension") in out
    assert "Load unpacked" in out and "Developer mode" in out
    assert "about:debugging" not in out and "xpi" not in out
    doc = json.loads(target.chrome_manifest_path.read_text(encoding="utf-8"))
    assert doc["allowed_origins"] == [CHROME_ORIGIN] and doc["path"] == str(target.launcher)
    assert not target.manifest_path.exists()
    if target.platform == "windows":
        assert registry.values == {CHROME_REGISTRY_KEY: str(target.chrome_manifest_path)}
        assert "HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\compound_registry" in out


def test_install_host_both(run, target, registry, tmp_path):
    chosen = create_registry(tmp_path / "chosen")
    code, out, err = run("--home", str(chosen), "install-host", "--browser", "both")
    assert code == 0, err
    assert "registered with Firefox and Chrome" in out
    assert f"Manifest        {target.manifest_path}" in out
    assert f"Chrome manifest {target.chrome_manifest_path}" in out
    assert f"Host config     {target.sidecar_path} (shared by Firefox and Chrome)" in out
    assert str(PROJECT_ROOT / "extension" / "artifacts") in out
    assert "npm run package:chrome" in out and out.count("Next steps") == 1
    assert target.manifest_path.is_file() and target.chrome_manifest_path.is_file()
    doc = json.loads(run("--json", "install-host", "--browser", "both")[1])
    assert doc["browsers"] == ["firefox", "chrome"] and doc["home_choice"] == "kept"
    assert set(doc["registrations"]) == {"firefox", "chrome"}


def test_install_host_chrome_refuses_another_registry(run, target, registry, tmp_path):
    pinned = create_registry(tmp_path / "pinned")
    other = create_registry(tmp_path / "other")
    assert run("install-host", "--home", str(pinned))[0] == 0
    before = (dict(registry.values), target.sidecar_path.read_bytes())
    code, out, err = run("install-host", "--browser", "chrome", "--home", str(other))
    assert code == 1 and "error [host_install_conflict]" in err and "--browser both" in err
    assert (dict(registry.values), target.sidecar_path.read_bytes()) == before
    assert not target.chrome_manifest_path.exists()
    code, out, err = run("install-host", "--browser", "chrome")
    assert code == 0, err
    assert f"Keeping the registry {pinned}" in out and f"Extension uses  {pinned}" in out


def test_uninstall_host_chrome(run, target, registry, tmp_path):
    assert run("install-host", "--browser", "both", "--home",
               str(create_registry(tmp_path / "chosen")))[0] == 0
    code, out, err = run("uninstall-host", "--browser", "chrome")
    assert code == 0, err
    assert "no longer registered with Chrome" in out
    assert str(target.chrome_manifest_path) in out and "Kept the host configuration" in out
    assert not target.chrome_manifest_path.exists()
    assert target.manifest_path.is_file() and target.sidecar_path.is_file()
    code, out, _ = run("uninstall-host", "--browser", "both", "--json")
    doc = json.loads(out)
    assert code == 0 and str(target.sidecar_path) in doc["removed"]
    assert registry.values == {}


@pytest.mark.parametrize("command", ["install-host", "uninstall-host"])
def test_unknown_browser_is_a_usage_error(run, target, registry, command):
    code, _, err = run(command, "--browser", "edge")
    assert code == 2 and "--browser" in err
    assert not target.manifest_path.exists() and registry.values == {}


def test_browser_option_in_help(run):
    for command in ("install-host", "uninstall-host"):
        code, out, _ = run(command, "--help")
        assert code == 0 and "--browser" in out and "chrome" in out and "both" in out


def test_doctor_survives_a_failing_status(run, monkeypatch, tmp_path):
    def boom(**kwargs):
        raise OSError("registry unavailable")

    monkeypatch.setattr(install_host, "status", boom)
    code, out, _ = run("--home", str(create_registry(tmp_path / "reg")), "--json", "doctor")
    doc = json.loads(out)
    assert code == 0 and doc["native_host"]["registered"] is False
    assert "registry unavailable" in doc["native_host"]["problems"][0]


# --------------------------------------------------------------------------- host-status

def test_host_status_without_any_registration_or_registry(run, target, tmp_path):
    """Works where 'doctor' cannot: nothing registered, no registry."""
    assert not (tmp_path / "env-home").exists()
    code, out, err = run("host-status")
    assert code == 1 and err == ""
    assert_ascii(out)
    assert "Firefox host    NOT registered" in out and "Chrome host     NOT registered" in out
    assert "warning: The host is not registered for Firefox" in out
    code, out, _ = run("host-status", "--json")
    doc = json.loads(out)
    assert code == 1 and set(doc) == {"firefox", "chrome"}
    assert doc["firefox"]["registered"] is False and doc["chrome"]["registered"] is False


def test_host_status_of_a_registered_browser(run, target, tmp_path):
    home = create_registry(tmp_path / "reg")
    assert run("install-host", "--home", str(home))[0] == 0
    code, out, _ = run("host-status")
    assert code == 0          # Chrome is not registered, which is no problem
    assert f"Firefox host    registered, manifest {target.manifest_path}" in out
    assert f"Extension home  {home} (host configuration)" in out
    assert "Chrome host     NOT registered" in out
    code, out, _ = run("host-status", "--browser", "firefox", "--json")
    doc = json.loads(out)
    assert code == 0 and set(doc) == {"firefox"}
    assert doc["firefox"]["home_override"] == str(home) and doc["firefox"]["problems"] == []
    assert run("host-status", "--browser", "chrome")[0] == 1


def test_host_status_reports_a_missing_registry(run, target, tmp_path):
    home = create_registry(tmp_path / "reg")
    assert run("install-host", "--browser", "both", "--home", str(home))[0] == 0
    home.rename(tmp_path / "moved")
    code, out, _ = run("host-status")
    assert code == 1 and "No registry was found" in out
    assert "Firefox host    registered" in out and "Chrome host     registered" in out


def test_host_status_survives_a_failing_check(run, monkeypatch):
    def boom(**kwargs):
        raise OSError("registry unavailable")

    monkeypatch.setattr(install_host, "status", boom)
    code, out, _ = run("host-status", "--json")
    doc = json.loads(out)
    assert code == 1 and doc["chrome"]["check_failed"] is True
    assert "The Chrome host registration could not be checked" in doc["chrome"]["problems"][0]
    assert "allowed_origins" in doc["chrome"] and "allowed_extensions" in doc["firefox"]


def test_the_messages_name_a_command_that_exists(run):
    """install_host's texts send the user to 'compound-registry host-status'."""
    import inspect

    assert "'compound-registry host-status'" in inspect.getsource(install_host)
    code, out, _ = run("host-status", "--help")
    assert code == 0 and "--browser" in out


# --------------------------------------------------------------------------- sync folders

def test_install_host_and_init_warn_about_a_sync_folder(run, target, tmp_path, monkeypatch):
    cloud = tmp_path / "OneDrive - Example"
    monkeypatch.setenv("OneDrive", str(cloud))
    home = create_registry(cloud / "reg")
    code, out, err = run("install-host", "--home", str(home))
    assert code == 0, err
    assert out.count("is inside OneDrive") == 1 and "Keep the live database on a local disk" in out
    code, out, _ = run("--home", str(home), "init")
    assert code == 0 and "nothing was changed" in out and "is inside OneDrive" in out
    # a registry created by install-host: the warning comes once
    code, out, err = run("install-host", "--home", str(cloud / "new"), "--create-registry")
    assert code == 0, err
    assert "NEW, EMPTY" in out and out.count("is inside OneDrive") == 1


def test_the_create_prompt_shows_the_sync_folder_warning(run, target, tmp_path, monkeypatch):
    cloud = tmp_path / "OneDrive - Example"
    monkeypatch.setenv("OneDrive", str(cloud))
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    asked = []
    monkeypatch.setattr(cli, "_ask", lambda prompt: asked.append(prompt) or "n")
    code, out, err = run("install-host", "--home", str(cloud / "new"))
    assert code == 1 and "registry_missing" in err and len(asked) == 1
    assert "warning: The registry database" in out and "is inside OneDrive" in out
    assert not (cloud / "new").exists()
