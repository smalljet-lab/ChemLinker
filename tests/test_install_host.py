"""Tests for compound_registry.install_host with temp folders and a fake registry.

Nothing here touches the real HKCU, the real Mozilla folders or the real
%LOCALAPPDATA%: every call gets an explicit :class:`HostTarget`, and the real
``WindowsRegistry`` fails the test if it is ever used.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from compound_registry import install_host, protocol
from compound_registry.errors import RegistryError
from compound_registry.host import resolve_home
from compound_registry.install_host import (
    CHROME_MANIFEST_NAME,
    CHROME_REGISTRY_KEY,
    MANIFEST_NAME,
    REGISTRY_KEY,
    SIDECAR_NAME,
    HostTarget,
    chrome_manifest_document,
    default_target,
    install,
    locate_registry,
    manifest_document,
    read_host_config,
    status,
    uninstall,
)
from compound_registry.protocol import CHROME_ORIGIN, EXTENSION_ID, HOST_NAME
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


@pytest.fixture(autouse=True)
def _no_real_machine(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("the real Windows registry must not be used in tests")

    for name in ("read_default", "write_default", "delete_key"):
        monkeypatch.setattr(install_host.WindowsRegistry, name, forbidden)
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata-env"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


def create_registry(path: Path, batches: int = 0) -> Path:
    with RegistryService.open(path) as svc:
        for i in range(batches):
            svc.register(smiles="C" * (i + 1) + "O", experiment_ref=f"E{i}")
    return path


@pytest.fixture
def env_home(tmp_path) -> Path:
    """The default registry (via $COMPOUND_REGISTRY_HOME), existing."""
    return create_registry(tmp_path / "env-home")


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


def make_target(tmp_path: Path, platform: str, registry: FakeRegistry, venv: str = "venv",
                create_launcher: bool = True) -> HostTarget:
    target = default_target(platform=platform, prefix=tmp_path / venv,
                            user_home=tmp_path / "user", registry=registry)
    if create_launcher:
        target.launcher.parent.mkdir(parents=True, exist_ok=True)
        target.launcher.write_bytes(b"launcher")
    return target


@pytest.fixture(params=["windows", "macos", "linux"])
def target(request, tmp_path, registry) -> HostTarget:
    return make_target(tmp_path, request.param, registry)


@pytest.fixture
def win(tmp_path, registry) -> HostTarget:
    return make_target(tmp_path, "windows", registry)


def extension_home(target: HostTarget) -> tuple[Path | None, str]:
    """What a host started by Firefox for this registration would use."""
    choice = resolve_home([str(target.manifest_path), protocol.EXTENSION_ID])
    return choice.home, choice.source


def nothing_written(target: HostTarget, registry: FakeRegistry) -> bool:
    return (not target.manifest_path.exists() and not target.sidecar_path.exists()
            and registry.values == {})


# --------------------------------------------------------------------------- locations

def test_windows_manifest_is_outside_the_venv(tmp_path, registry):
    w = default_target(platform="windows", prefix=tmp_path / "v", user_home=tmp_path / "u",
                       registry=registry)
    assert w.manifest_path == (tmp_path / "u" / "AppData" / "Local" / "CompoundRegistry"
                               / "native-host" / MANIFEST_NAME)
    assert w.launcher == tmp_path / "v" / "Scripts" / "compound-registry-host.exe"
    explicit = default_target(platform="windows", prefix=tmp_path / "v",
                              local_appdata=tmp_path / "lad", registry=registry)
    assert explicit.manifest_dir == tmp_path / "lad" / "CompoundRegistry" / "native-host"


def test_windows_manifest_follows_localappdata(tmp_path, registry, monkeypatch):
    t = default_target(platform="windows", prefix=tmp_path / "v", registry=registry)
    assert t.manifest_dir == tmp_path / "localappdata-env" / "CompoundRegistry" / "native-host"
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    t = default_target(platform="windows", prefix=tmp_path / "v", registry=registry)
    assert t.manifest_dir == (tmp_path / "home" / "AppData" / "Local" / "CompoundRegistry"
                              / "native-host")


def test_unix_locations(tmp_path):
    m = default_target(platform="macos", prefix=tmp_path / "v", user_home=tmp_path / "u")
    assert m.manifest_path == (tmp_path / "u" / "Library" / "Application Support" / "Mozilla"
                               / "NativeMessagingHosts" / "compound_registry.json")
    assert m.launcher == tmp_path / "v" / "bin" / "compound-registry-host"
    lx = default_target(platform="linux", prefix=tmp_path / "v", user_home=tmp_path / "u")
    assert lx.manifest_path == (tmp_path / "u" / ".mozilla" / "native-messaging-hosts"
                                / "compound_registry.json")
    assert REGISTRY_KEY == r"Software\Mozilla\NativeMessagingHosts\compound_registry"


def test_default_target_uses_sys_prefix(registry):
    assert str(sys.prefix) in str(default_target(registry=registry).launcher)


def test_unknown_platform(tmp_path):
    with pytest.raises(RegistryError):
        default_target(platform="amiga", prefix=tmp_path)


# --------------------------------------------------------------------------- install basics

def test_install_writes_manifest(target, registry, env_home):
    result = install(target=target)
    manifest = target.manifest_path
    raw = manifest.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw and raw.endswith(b"\n")
    doc = json.loads(raw.decode("utf-8"))
    assert doc == manifest_document(target.launcher)
    assert doc["name"] == HOST_NAME and doc["type"] == "stdio"
    assert doc["allowed_extensions"] == [EXTENSION_ID] and doc["path"] == str(target.launcher)
    assert result["home_override"] is None and result["home_choice"] == "default"
    assert not target.sidecar_path.exists()
    if target.platform == "windows":
        assert registry.values == {REGISTRY_KEY: str(manifest)}
    else:
        assert registry.values == {}
    assert sorted(p.name for p in manifest.parent.iterdir()) == [MANIFEST_NAME]
    assert result["registry"] == {
        "home": str(env_home), "config_path": str(env_home / "config.toml"),
        "db_path": str(env_home / "registry.sqlite"), "created": False, "batches": 0,
        "retired": 0, "integrity": "ok"}
    assert result["registry_created"] is False and result["previous_registration"] is None
    s = status(target=target)
    assert s["registered"] and s["manifest_valid"] and s["launcher_exists"]
    assert s["problems"] == [] and s["allowed_extensions"] == [EXTENSION_ID]
    assert s["extension_home"] == str(env_home) and s["extension_registry_exists"] is True
    assert json.loads(json.dumps(s)) == s and json.loads(json.dumps(result)) == result


def test_install_reports_how_full_the_registry_is(win, tmp_path):
    full = create_registry(tmp_path / "full", batches=2)
    reg = install(full, target=win)["registry"]
    assert reg["batches"] == 2 and reg["integrity"] == "ok" and reg["home"] == str(full)


def test_install_with_home_writes_the_host_config(target, tmp_path):
    chosen = create_registry(tmp_path / "Registry ü")
    result = install(chosen, target=target)
    raw = target.sidecar_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert json.loads(raw.decode("utf-8")) == {"home": str(chosen)}
    assert result["home_override"] == str(chosen) and result["home_choice"] == "pinned"
    assert result["extension_home"] == str(chosen)
    assert result["extension_home_source"] == "host-config"
    assert extension_home(target) == (chosen, "host-config")
    assert status(target=target)["home_override"] == str(chosen)


def test_install_relative_home_is_refused(target, registry):
    for home in ("relative/reg", "", "reg"):
        with pytest.raises(RegistryError) as info:
            install(home, target=target)
        assert info.value.code == "invalid_argument"
    assert nothing_written(target, registry)


def test_home_and_default_home_exclude_each_other(target, tmp_path, registry):
    with pytest.raises(RegistryError) as info:
        install(tmp_path / "x", default_home=True, target=target)
    assert info.value.code == "invalid_argument" and nothing_written(target, registry)


def test_missing_launcher_is_refused(tmp_path, registry, env_home):
    t = make_target(tmp_path, "windows", registry, create_launcher=False)
    with pytest.raises(RegistryError) as info:
        install(target=t)
    assert info.value.code == "host_launcher_missing" and "install.ps1" in info.value.message
    assert nothing_written(t, registry)


def test_unwritable_manifest_folder_is_a_registry_error(tmp_path, registry, env_home):
    t = make_target(tmp_path, "linux", registry)
    t.manifest_dir.parent.mkdir(parents=True)
    t.manifest_dir.write_text("a file where the folder should be", encoding="utf-8")
    with pytest.raises(RegistryError) as info:
        install(target=t)
    assert info.value.code == "host_install_failed"


def test_env_warning(target, env_home):
    result = install(target=target)
    assert any("COMPOUND_REGISTRY_HOME is set" in w and "--home" in w for w in result["warnings"])


def test_no_env_names_the_user_default(target, tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOUND_REGISTRY_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    create_registry(tmp_path / "user" / "CompoundRegistry")
    result = install(target=target)
    assert result["extension_home"] == str(tmp_path / "user" / "CompoundRegistry")
    assert result["warnings"] == []


# --------------------------------------------------------------------------- registry existence

def test_missing_pinned_registry_is_refused_and_nothing_written(target, registry, tmp_path):
    missing = tmp_path / "not-there"
    with pytest.raises(RegistryError) as info:
        install(missing, target=target)
    assert info.value.code == "registry_missing"
    assert str(missing) in info.value.message and "--create-registry" in info.value.message
    assert "Nothing was installed" in info.value.message
    assert nothing_written(target, registry) and not missing.exists()


def test_missing_default_registry_is_refused(target, registry, tmp_path):
    with pytest.raises(RegistryError) as info:
        install(target=target)
    assert info.value.code == "registry_missing"
    assert str(tmp_path / "env-home") in info.value.message
    assert nothing_written(target, registry) and not (tmp_path / "env-home").exists()


def test_half_registry_counts_as_missing(target, registry, tmp_path):
    half = tmp_path / "half"
    half.mkdir()
    (half / "config.toml").write_text("", encoding="utf-8")
    with pytest.raises(RegistryError) as info:
        install(half, target=target)
    assert info.value.code == "registry_missing" and "registry.sqlite" in info.value.message
    assert nothing_written(target, registry)


def test_create_registry_flag(target, tmp_path):
    new = tmp_path / "new-registry"
    result = install(new, create_registry=True, target=target)
    assert result["registry_created"] is True and result["registry"]["created"] is True
    assert result["registry"]["batches"] == 0
    assert (new / "config.toml").is_file() and (new / "registry.sqlite").is_file()
    assert locate_registry(new).exists


def test_create_registry_on_an_existing_registry_creates_nothing(win, tmp_path):
    full = create_registry(tmp_path / "full", batches=1)
    result = install(full, create_registry=True, target=win)
    assert result["registry_created"] is False and result["registry"]["batches"] == 1


@pytest.mark.parametrize("answer", [True, False])
def test_confirm_create_callback(win, registry, tmp_path, answer):
    asked = []
    new = tmp_path / "asked"

    def confirm(path):
        asked.append(path)
        return answer

    if answer:
        assert install(new, confirm_create=confirm, target=win)["registry_created"] is True
    else:
        with pytest.raises(RegistryError) as info:
            install(new, confirm_create=confirm, target=win)
        assert info.value.code == "registry_missing"
        assert nothing_written(win, registry) and not new.exists()
    assert asked == [str(new)]


def test_creating_where_a_journal_exists_warns_about_recover(win, tmp_path):
    folder = tmp_path / "lost-db"
    folder.mkdir()
    (folder / "issued-ids.jsonl").write_text('{"event": "register"}\n', encoding="utf-8")
    result = install(folder, create_registry=True, target=win)
    assert any("recover" in w and "issued-ID journal" in w for w in result["warnings"])


# --------------------------------------------------------------------------- which registry: kept, pinned, default

@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_rerun_without_home_keeps_the_chosen_registry(tmp_path, registry, platform, env_home):
    """Reviewer's proof 1, now asserting the safe behaviour."""
    t = make_target(tmp_path, platform, registry)
    custom = create_registry(tmp_path / "D-CompoundRegistry")
    install(custom, target=t)
    result = install(target=t)    # e.g. install.ps1 again after a git pull
    assert extension_home(t) == (custom, "host-config")
    assert result["home_choice"] == "kept" and result["home_override"] == str(custom)
    assert result["registry"]["home"] == str(custom)
    assert any("Keeping the registry" in m and str(custom) in m for m in result["messages"])


def test_default_home_flag_removes_the_pin(target, tmp_path, env_home):
    custom = create_registry(tmp_path / "custom")
    install(custom, target=target)
    result = install(default_home=True, target=target)
    assert result["sidecar_removed"] is True and not target.sidecar_path.exists()
    assert extension_home(target) == (None, "env")
    assert result["registry"]["home"] == str(env_home)


def test_default_home_flag_also_needs_an_existing_registry(target, tmp_path, registry):
    custom = create_registry(tmp_path / "custom")
    install(custom, target=target)
    with pytest.raises(RegistryError) as info:
        install(default_home=True, target=target)   # env-home does not exist
    assert info.value.code == "registry_missing"
    assert extension_home(target) == (custom, "host-config")   # unchanged


@pytest.mark.parametrize("content", [b"{oops", b'{"home": "relative"}', b"[]"])
def test_invalid_existing_host_config_needs_an_explicit_choice(target, tmp_path, env_home,
                                                               content):
    custom = create_registry(tmp_path / "custom")
    install(custom, target=target)
    target.sidecar_path.write_bytes(content)
    manifest_before = target.manifest_path.read_bytes()
    with pytest.raises(RegistryError) as info:
        install(target=target)
    assert info.value.code == "config_invalid"
    assert "--home" in info.value.message and "--default-home" in info.value.message
    assert target.sidecar_path.read_bytes() == content
    assert target.manifest_path.read_bytes() == manifest_before
    assert install(custom, target=target)["home_override"] == str(custom)
    target.sidecar_path.write_bytes(content)
    assert install(default_home=True, target=target)["sidecar_removed"] is True


# --------------------------------------------------------------------------- other installations

@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_another_installation_is_not_replaced_silently(tmp_path, registry, platform, env_home):
    """Reviewer's proof 2, now asserting the safe behaviour."""
    a = make_target(tmp_path, platform, registry, "venvA")
    b = make_target(tmp_path, platform, registry, "venvB")
    reg_a = create_registry(tmp_path / "regA")
    install(reg_a, target=a)
    before = (dict(registry.values), a.manifest_path.read_bytes())
    with pytest.raises(RegistryError) as info:
        install(target=b)
    assert info.value.code == "host_install_conflict"
    assert str(a.launcher) in info.value.message and "--force" in info.value.message
    assert (dict(registry.values), a.manifest_path.read_bytes()) == before

    result = install(target=b, force=True)
    assert json.loads(b.manifest_path.read_text(encoding="utf-8"))["path"] == str(b.launcher)
    assert any(str(a.launcher) in w for w in result["warnings"])
    assert result["previous_registration"]["kind"] == "other"
    assert result["previous_registration"]["launcher"] == str(a.launcher)
    # the registry chosen for A is kept (the extension keeps its registry)
    assert result["home_choice"] == "kept" and result["home_override"] == str(reg_a)


def test_a_moved_installation_is_replaced_with_a_message(tmp_path, registry, env_home):
    a = make_target(tmp_path, "windows", registry, "venvA")
    install(target=a)
    a.launcher.unlink()     # project folder moved / deleted
    b = make_target(tmp_path, "windows", registry, "venvB")
    result = install(target=b)
    assert result["previous_registration"]["kind"] == "stale"
    assert any("no longer exists" in m for m in result["messages"])
    assert registry.values[REGISTRY_KEY] == str(b.manifest_path)


def test_a_broken_registration_is_replaced(win, registry, tmp_path, env_home):
    registry.values[REGISTRY_KEY] = str(tmp_path / "gone" / MANIFEST_NAME)
    result = install(target=win)
    assert result["previous_registration"]["kind"] == "broken"
    assert any("unusable" in m for m in result["messages"])
    assert registry.values[REGISTRY_KEY] == str(win.manifest_path)


def _registration_elsewhere(t: HostTarget, registry: FakeRegistry, home: Path | None) -> Path:
    """Register ``t``'s launcher through a manifest in a folder that is not
    ``t.manifest_dir`` (as after %LOCALAPPDATA% changed)."""
    manifest = t.launcher.parent.parent / "elsewhere" / MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(manifest_document(t.launcher)), encoding="utf-8")
    if home is not None:
        (manifest.parent / SIDECAR_NAME).write_text(json.dumps({"home": str(home)}),
                                                    encoding="utf-8")
    registry.values[REGISTRY_KEY] = str(manifest)
    return manifest


def test_registration_in_another_folder_is_taken_over_with_its_registry(win, registry, tmp_path,
                                                                        env_home):
    chosen = create_registry(tmp_path / "chosen")
    elsewhere = _registration_elsewhere(win, registry, chosen)
    result = install(target=win)
    assert registry.values[REGISTRY_KEY] == str(win.manifest_path)
    assert result["home_choice"] == "kept" and result["home_override"] == str(chosen)
    assert json.loads(win.sidecar_path.read_text(encoding="utf-8")) == {"home": str(chosen)}
    assert elsewhere.exists()                    # only the registration moved
    assert "legacy_removed" not in result
    assert extension_home(win) == (chosen, "host-config")


def test_registration_of_another_environment_in_another_folder(tmp_path, registry, env_home):
    other = make_target(tmp_path, "windows", registry, "other")
    elsewhere = _registration_elsewhere(other, registry, None)
    mine = make_target(tmp_path, "windows", registry)
    with pytest.raises(RegistryError) as info:
        install(target=mine)
    assert info.value.code == "host_install_conflict"
    install(target=mine, force=True)
    assert elsewhere.exists()     # not ours: only the registration moved


# --------------------------------------------------------------------------- uninstall

def test_uninstall(target, registry, tmp_path):
    install(create_registry(tmp_path / "chosen"), target=target)
    result = uninstall(target=target)
    assert str(target.manifest_path) in result["removed"]
    assert str(target.sidecar_path) in result["removed"]
    assert not target.manifest_path.exists() and not target.sidecar_path.exists()
    assert registry.values == {}
    if target.platform == "windows":
        assert result["removed"][0].startswith("HKCU\\")
        assert not target.manifest_dir.exists()          # our own empty folder
    else:
        assert target.manifest_dir.exists()               # the Mozilla folder stays
    again = uninstall(target=target)
    assert again["removed"] == [] and "Nothing to remove" in again["messages"][0]
    assert status(target=target)["registered"] is False
    assert (tmp_path / "chosen" / "registry.sqlite").is_file()   # the registry is untouched


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_uninstall_leaves_another_installation_alone(tmp_path, registry, platform, env_home):
    """Reviewer's proof 3, now asserting the safe behaviour."""
    a = make_target(tmp_path, platform, registry, "venvA")
    b = make_target(tmp_path, platform, registry, "venvB")
    install(target=a)
    before = dict(registry.values)
    with pytest.raises(RegistryError) as info:
        uninstall(target=b)
    assert info.value.code == "host_install_conflict"
    assert registry.values == before and a.manifest_path.is_file()
    result = uninstall(target=b, force=True)
    assert registry.values == {} and not a.manifest_path.exists()
    assert result["previous_launcher"] == str(a.launcher)
    assert any("--force" in m for m in result["messages"])


def test_uninstall_a_registration_whose_program_is_gone(tmp_path, registry, env_home):
    a = make_target(tmp_path, "windows", registry, "venvA")
    install(target=a)
    a.launcher.unlink()
    b = make_target(tmp_path, "windows", registry, "venvB")
    result = uninstall(target=b)
    assert registry.values == {} and str(a.manifest_path) in result["removed"]


def test_uninstall_leaves_a_manifest_in_another_folder_in_place(win, registry):
    elsewhere = _registration_elsewhere(win, registry, None)
    result = uninstall(target=win)
    assert registry.values == {} and elsewhere.exists()
    assert str(elsewhere) not in result["removed"]
    assert any("left in place" in m and str(elsewhere) in m for m in result["messages"])


# --------------------------------------------------------------------------- status

def test_status_not_registered(target, env_home):
    s = status(target=target)
    assert s["registered"] is False and s["manifest_exists"] is False
    assert len(s["problems"]) == 1 and "not registered" in s["problems"][0]
    assert "install-host" in s["problems"][0]
    assert s["extension_home"] == str(env_home)


def test_status_registry_value_without_manifest(win, env_home):
    install(target=win)
    win.manifest_path.unlink()
    s = status(target=win)
    assert s["registered"] is True and s["manifest_exists"] is False
    assert any("is missing" in p for p in s["problems"])


def test_status_launcher_missing(target, env_home):
    install(target=target)
    target.launcher.unlink()
    s = status(target=target)
    assert s["launcher_exists"] is False
    assert any("does not exist" in p for p in s["problems"])


def test_status_other_venv(tmp_path, registry, target, env_home):
    other = make_target(tmp_path, target.platform, registry, venv="other-venv")
    install(target=other)
    s = status(target=target)
    assert s["launcher"] == str(other.launcher) and s["launcher_exists"]
    assert any("another Python environment" in p for p in s["problems"])


def test_status_manifest_in_another_folder(win, registry, env_home):
    _registration_elsewhere(win, registry, None)
    s = status(target=win)
    assert any("not the one of this installation" in p for p in s["problems"])


def test_status_registry_missing(target, tmp_path):
    custom = create_registry(tmp_path / "custom")
    install(custom, target=target)
    custom.rename(tmp_path / "moved")
    s = status(target=target)
    assert s["extension_home"] == str(custom) and s["extension_registry_exists"] is False
    assert any("No registry was found" in p for p in s["problems"])
    assert not custom.exists()


def test_status_wrong_allowed_extensions(target, env_home):
    install(target=target)
    doc = json.loads(target.manifest_path.read_text(encoding="utf-8"))
    doc["allowed_extensions"] = ["evil@example.org"]
    target.manifest_path.write_text(json.dumps(doc), encoding="utf-8")
    s = status(target=target)
    assert s["allowed_extensions"] == ["evil@example.org"]
    assert any("evil@example.org" in p and EXTENSION_ID in p for p in s["problems"])


@pytest.mark.parametrize("content,fragment", [
    ("not json", "not valid"),
    ("[]", "not a JSON object"),
    ('{"name": "other", "type": "stdio", "path": "x", "allowed_extensions": []}', "name"),
    ('{"name": "compound_registry", "type": "pipe", "path": "x", "allowed_extensions": []}',
     "type"),
    ('{"name": "compound_registry", "type": "stdio", "allowed_extensions": []}', "program path"),
    ('{"name": "compound_registry", "type": "stdio", "path": "x"}', "allowed_extensions"),
])
def test_status_invalid_manifest(target, content, fragment, env_home):
    install(target=target)
    target.manifest_path.write_text(content, encoding="utf-8")
    s = status(target=target)
    assert s["manifest_exists"] and not s["manifest_valid"]
    assert any(fragment in p for p in s["problems"])


def test_status_relative_launcher_on_windows_is_relative_to_the_manifest(tmp_path, registry,
                                                                         env_home):
    # manifest folder next to Scripts
    win = default_target(platform="windows", prefix=tmp_path / "venv",
                         local_appdata=tmp_path / "venv" / "lad", registry=registry)
    win.launcher.parent.mkdir(parents=True)
    win.launcher.write_bytes(b"x")
    install(target=win)
    doc = json.loads(win.manifest_path.read_text(encoding="utf-8"))
    doc["path"] = r"..\..\..\Scripts\compound-registry-host.exe"
    win.manifest_path.write_text(json.dumps(doc), encoding="utf-8")
    s = status(target=win)
    assert s["launcher_exists"] and s["problems"] == []


@pytest.mark.parametrize("content", [b"{oops", b'{"home": "rel"}', b"[]", b"\xff"])
def test_status_invalid_host_config(target, content, env_home):
    install(target=target)
    target.sidecar_path.write_bytes(content)
    s = status(target=target)
    assert s["home_override"] is None and s["extension_home_source"] == "host-config"
    assert any(SIDECAR_NAME in p and "invalid" in p for p in s["problems"])


def test_status_unreadable_host_config(target, env_home):
    install(target=target)
    target.sidecar_path.mkdir()
    s = status(target=target)
    assert any("cannot be read" in p for p in s["problems"])


# --------------------------------------------------------------------------- read_host_config / locate_registry

@pytest.mark.parametrize("content", [b'{"home": "ABS"}', b'\xef\xbb\xbf{"home": "ABS", "x": 1}'])
def test_read_host_config_valid(tmp_path, content):
    absolute = str(tmp_path / "reg")
    path = tmp_path / SIDECAR_NAME
    path.write_bytes(content.replace(b"ABS", json.dumps(absolute)[1:-1].encode()))
    assert read_host_config(path) == (absolute, None)


def test_read_host_config_missing(tmp_path):
    home, problem = read_host_config(tmp_path / "nope.json")
    assert home is None and "cannot be read" in problem


def test_locate_registry_creates_nothing(tmp_path):
    loc = locate_registry(tmp_path / "nothing")
    assert not loc.exists and len(loc.missing) == 2
    assert not (tmp_path / "nothing").exists()


# --------------------------------------------------------------------------- real winreg code path

def test_windows_registry_calls_winreg_correctly(monkeypatch):
    """The real class against a stand-in ``winreg`` module (never the real one)."""
    monkeypatch.undo()   # drop the autouse guard for this class only ...
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", "unused")
    calls = []
    store: dict[str, str] = {}

    class Handle:
        def __init__(self, key):
            self.key = key

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.SimpleNamespace(HKEY_CURRENT_USER="HKCU", KEY_WRITE="KW", REG_SZ="SZ")

    def create(root, key, reserved, access):
        calls.append(("CreateKeyEx", root, key, reserved, access))
        return Handle(key)

    def set_value(handle, name, reserved, kind, value):
        calls.append(("SetValueEx", handle.key, name, reserved, kind, value))
        store[handle.key] = value

    def open_key(root, key):
        if key not in store:
            raise FileNotFoundError(key)
        return Handle(key)

    def query(handle, name):
        return store[handle.key], "SZ"

    def delete(root, key):
        calls.append(("DeleteKey", root, key))
        if store.pop(key, None) is None:
            raise FileNotFoundError(key)

    fake.CreateKeyEx, fake.SetValueEx, fake.OpenKey = create, set_value, open_key
    fake.QueryValueEx, fake.DeleteKey = query, delete
    monkeypatch.setitem(sys.modules, "winreg", fake)

    reg = install_host.WindowsRegistry()
    assert reg.read_default(REGISTRY_KEY) is None
    reg.write_default(REGISTRY_KEY, r"C:\x\compound_registry.json")
    assert reg.read_default(REGISTRY_KEY) == r"C:\x\compound_registry.json"
    assert reg.delete_key(REGISTRY_KEY) is True
    assert reg.delete_key(REGISTRY_KEY) is False
    assert calls[0] == ("CreateKeyEx", "HKCU", REGISTRY_KEY, 0, "KW")
    assert calls[1] == ("SetValueEx", REGISTRY_KEY, "", 0, "SZ", r"C:\x\compound_registry.json")
    assert calls[2] == ("DeleteKey", "HKCU", REGISTRY_KEY)


# --------------------------------------------------------------------------- Chrome

def chrome_home(target: HostTarget) -> tuple[Path | None, str, str | None]:
    """What a host started by Chrome for this registration would use."""
    choice = resolve_home([CHROME_ORIGIN, "--parent-window=0"], target=target)
    return choice.home, choice.source, choice.error.code if choice.error else None


def chrome_nothing_written(target: HostTarget, registry: FakeRegistry) -> bool:
    return not target.chrome_manifest_path.exists() and CHROME_REGISTRY_KEY not in registry.values


def test_chrome_locations(tmp_path, registry):
    w = default_target(platform="windows", prefix=tmp_path / "v", user_home=tmp_path / "u",
                       registry=registry)
    assert w.chrome_manifest_path == w.manifest_dir / CHROME_MANIFEST_NAME   # shared folder
    assert CHROME_MANIFEST_NAME == "compound_registry.chrome.json"
    assert CHROME_REGISTRY_KEY == r"Software\Google\Chrome\NativeMessagingHosts\compound_registry"
    m = default_target(platform="macos", prefix=tmp_path / "v", user_home=tmp_path / "u")
    assert m.chrome_manifest_path == (tmp_path / "u" / "Library" / "Application Support"
                                      / "Google" / "Chrome" / "NativeMessagingHosts"
                                      / "compound_registry.json")
    lx = default_target(platform="linux", prefix=tmp_path / "v", user_home=tmp_path / "u")
    assert lx.chrome_manifest_path == (tmp_path / "u" / ".config" / "google-chrome"
                                       / "NativeMessagingHosts" / "compound_registry.json")
    # the one host configuration stays next to the Firefox manifest
    assert lx.sidecar_path == lx.manifest_dir / SIDECAR_NAME
    assert install_host.host_config_path(lx, "chrome", lx.chrome_manifest_path) == lx.sidecar_path
    assert install_host.host_config_path(w, "chrome", w.chrome_manifest_path) == w.sidecar_path


def test_chrome_manifest_document(tmp_path):
    doc = chrome_manifest_document(tmp_path / "host.exe")
    assert doc == {"name": HOST_NAME, "description": install_host.DESCRIPTION,
                   "path": str(tmp_path / "host.exe"), "type": "stdio",
                   "allowed_origins": ["chrome-extension://imkbdobpgmdgbpfeencchphiegjledoj/"]}


def test_install_chrome_alone_on_a_fresh_machine(target, registry, tmp_path):
    chosen = create_registry(tmp_path / "chosen")
    result = install(chosen, target=target, browsers=("chrome",))
    raw = target.chrome_manifest_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert json.loads(raw.decode("utf-8")) == chrome_manifest_document(target.launcher)
    assert not target.manifest_path.exists()                   # Firefox not registered
    assert json.loads(target.sidecar_path.read_text(encoding="utf-8")) == {"home": str(chosen)}
    if target.platform == "windows":
        assert registry.values == {CHROME_REGISTRY_KEY: str(target.chrome_manifest_path)}
        assert result["registry_key"] == "HKCU\\" + CHROME_REGISTRY_KEY
    else:
        assert registry.values == {}
    assert result["browsers"] == ["chrome"]
    assert result["manifest_path"] == str(target.chrome_manifest_path)
    assert result["manifest"]["allowed_origins"] == [CHROME_ORIGIN]
    assert result["home_choice"] == "pinned" and result["extension_home"] == str(chosen)
    assert chrome_home(target) == (chosen, "host-config", None)
    s = status(target=target, browser="chrome")
    assert s["registered"] and s["problems"] == [] and s["allowed_origins"] == [CHROME_ORIGIN]
    assert s["extension_home"] == str(chosen) and s["browser"] == "chrome"
    assert status(target=target)["registered"] is False          # Firefox status unchanged


def test_install_both(target, registry, tmp_path):
    chosen = create_registry(tmp_path / "chosen")
    result = install(chosen, target=target, browsers=("firefox", "chrome"))
    assert json.loads(target.manifest_path.read_text(encoding="utf-8")) == \
        manifest_document(target.launcher)
    assert json.loads(target.chrome_manifest_path.read_text(encoding="utf-8")) == \
        chrome_manifest_document(target.launcher)
    if target.platform == "windows":
        assert registry.values == {REGISTRY_KEY: str(target.manifest_path),
                                   CHROME_REGISTRY_KEY: str(target.chrome_manifest_path)}
        assert sorted(p.name for p in target.manifest_dir.iterdir()) == sorted(
            [MANIFEST_NAME, CHROME_MANIFEST_NAME, SIDECAR_NAME])
    # top level = Firefox, as before; both under "registrations"
    assert result["manifest_path"] == str(target.manifest_path)
    assert set(result["registrations"]) == {"firefox", "chrome"}
    assert extension_home(target) == (chosen, "host-config")
    assert chrome_home(target) == (chosen, "host-config", None)
    assert install_host._browsers("both") == ("firefox", "chrome")
    assert install(target=target, browsers="both")["home_choice"] == "kept"


def test_chrome_reuses_the_registry_pinned_for_firefox(target, registry, tmp_path, env_home):
    pinned = create_registry(tmp_path / "pinned")
    install(pinned, target=target)
    sidecar_before = target.sidecar_path.read_bytes()
    firefox_manifest = target.manifest_path.read_bytes()
    result = install(target=target, browsers=("chrome",))
    assert result["home_choice"] == "kept" and result["home_override"] == str(pinned)
    assert target.sidecar_path.read_bytes() == sidecar_before
    assert target.manifest_path.read_bytes() == firefox_manifest
    assert chrome_home(target) == (pinned, "host-config", None)
    assert extension_home(target) == (pinned, "host-config")
    # the same --home is fine, too
    assert install(pinned, target=target, browsers=("chrome",))["home_override"] == str(pinned)


def test_chrome_alone_never_repoints_the_registry(target, registry, tmp_path, env_home):
    pinned = create_registry(tmp_path / "pinned")
    other = create_registry(tmp_path / "other")
    install(pinned, target=target)
    before = (dict(registry.values), target.sidecar_path.read_bytes())
    for kwargs in ({"home": other}, {"default_home": True}):
        with pytest.raises(RegistryError) as info:
            install(target=target, browsers=("chrome",), **kwargs)
        assert info.value.code == "host_install_conflict"
        assert str(pinned) in info.value.message and "--browser both" in info.value.message
        assert (dict(registry.values), target.sidecar_path.read_bytes()) == before
        assert chrome_nothing_written(target, registry)
    assert extension_home(target) == (pinned, "host-config")
    # switching both browsers together is the documented way
    install(other, target=target, browsers="both")
    assert extension_home(target) == (other, "host-config")
    assert chrome_home(target) == (other, "host-config", None)


def test_chrome_alone_keeps_firefox_on_the_default_registry(target, registry, tmp_path, env_home):
    install(target=target)                   # Firefox, default registry (no host config)
    with pytest.raises(RegistryError) as info:
        install(create_registry(tmp_path / "other"), target=target, browsers=("chrome",))
    assert info.value.code == "host_install_conflict" and "default registry" in info.value.message
    assert not target.sidecar_path.exists() and chrome_nothing_written(target, registry)
    result = install(target=target, browsers=("chrome",))
    # the default registry is now pinned for both browsers (no environment dependence)
    assert result["home_choice"] == "default-pinned" and result["extension_home"] == str(env_home)
    assert result["home_override"] == str(env_home)
    assert json.loads(target.sidecar_path.read_text(encoding="utf-8")) == {"home": str(env_home)}
    assert any("Pinned the registry" in m for m in result["messages"])
    assert any("COMPOUND_REGISTRY_HOME is set here" in w for w in result["warnings"])
    assert chrome_home(target) == (env_home, "host-config", None)
    assert extension_home(target) == (env_home, "host-config")


def test_chrome_alone_refuses_a_firefox_registration_in_another_folder(win, registry, tmp_path,
                                                                      env_home):
    _registration_elsewhere(win, registry, create_registry(tmp_path / "chosen"))
    with pytest.raises(RegistryError) as info:
        install(target=win, browsers=("chrome",))
    assert info.value.code == "host_install_conflict" and "--browser both" in info.value.message
    assert chrome_nothing_written(win, registry)
    result = install(target=win, browsers="both")       # moves Firefox here, adds Chrome
    assert result["home_override"] == str(tmp_path / "chosen")
    assert chrome_home(win) == (tmp_path / "chosen", "host-config", None)


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_chrome_registration_of_another_installation(tmp_path, registry, platform, env_home):
    a = make_target(tmp_path, platform, registry, "venvA")
    b = make_target(tmp_path, platform, registry, "venvB")
    install(target=a, browsers=("chrome",))
    before = (dict(registry.values), a.chrome_manifest_path.read_bytes())
    for call in (lambda: install(target=b, browsers=("chrome",)),
                 lambda: uninstall(target=b, browsers=("chrome",))):
        with pytest.raises(RegistryError) as info:
            call()
        assert info.value.code == "host_install_conflict"
        assert info.value.message.startswith("Chrome's registration")
        assert (dict(registry.values), a.chrome_manifest_path.read_bytes()) == before
    result = install(target=b, browsers=("chrome",), force=True)
    assert result["previous_registration"]["kind"] == "other"
    assert any(w.startswith("Chrome: Replaced") for w in result["warnings"])


def test_different_registries_for_the_two_browsers_need_an_explicit_choice(win, registry,
                                                                           tmp_path, env_home):
    pinned = create_registry(tmp_path / "pinned")
    install(pinned, target=win)
    # a Chrome registration in another folder with another host configuration
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / CHROME_MANIFEST_NAME).write_text(
        json.dumps(chrome_manifest_document(win.launcher)), encoding="utf-8")
    (elsewhere / SIDECAR_NAME).write_text(json.dumps({"home": str(tmp_path / "x")}),
                                          encoding="utf-8")
    registry.values[CHROME_REGISTRY_KEY] = str(elsewhere / CHROME_MANIFEST_NAME)
    # the host refuses to pick one
    home, source, code = chrome_home(win)
    assert home is None and code == "config_invalid"
    problems = status(target=win, browser="chrome")["problems"]
    assert any("DIFFERENT registries" in p for p in problems)
    with pytest.raises(RegistryError) as info:
        install(target=win, browsers="both")
    assert info.value.code == "config_invalid" and "DIFFERENT registries" in info.value.message
    assert registry.values[CHROME_REGISTRY_KEY] == str(elsewhere / CHROME_MANIFEST_NAME)
    install(pinned, target=win, browsers="both")
    assert chrome_home(win) == (pinned, "host-config", None)


def test_firefox_install_reports_that_chrome_follows(target, registry, tmp_path, env_home):
    install(create_registry(tmp_path / "a"), target=target, browsers="both")
    result = install(create_registry(tmp_path / "b"), target=target)     # Firefox only
    assert any("Chrome is registered too" in m for m in result["messages"])
    assert chrome_home(target) == (tmp_path / "b", "host-config", None)


def test_uninstall_chrome_keeps_firefox_and_the_host_config(target, registry, tmp_path):
    chosen = create_registry(tmp_path / "chosen")
    install(chosen, target=target, browsers="both")
    result = uninstall(target=target, browsers=("chrome",))
    assert str(target.chrome_manifest_path) in result["removed"]
    assert not target.chrome_manifest_path.exists()
    assert target.manifest_path.is_file() and target.sidecar_path.is_file()
    assert any("Kept the host configuration" in m and "Firefox" in m for m in result["messages"])
    assert extension_home(target) == (chosen, "host-config")
    if target.platform == "windows":
        assert registry.values == {REGISTRY_KEY: str(target.manifest_path)}
        assert result["removed"][0] == "HKCU\\" + CHROME_REGISTRY_KEY
    assert status(target=target, browser="chrome")["registered"] is False
    assert chrome_home(target)[2] == "config_invalid"      # no registration: no guessing
    again = uninstall(target=target, browsers=("chrome",))
    assert again["removed"] == [] and "not registered for Chrome" in again["messages"][-1]


def test_uninstall_firefox_keeps_the_host_config_chrome_uses(target, registry, tmp_path):
    chosen = create_registry(tmp_path / "chosen")
    install(chosen, target=target, browsers="both")
    result = uninstall(target=target)                       # Firefox only (the default)
    assert str(target.manifest_path) in result["removed"]
    assert str(target.sidecar_path) not in result["removed"] and target.sidecar_path.is_file()
    assert any("Chrome's registration still uses it" in m for m in result["messages"])
    assert chrome_home(target) == (chosen, "host-config", None)
    if target.platform == "windows":
        assert registry.values == {CHROME_REGISTRY_KEY: str(target.chrome_manifest_path)}
        assert target.manifest_dir.is_dir()      # not empty: Chrome's manifest is there


def test_uninstall_both(target, registry, tmp_path):
    install(create_registry(tmp_path / "chosen"), target=target, browsers="both")
    result = uninstall(target=target, browsers="both")
    for path in (target.manifest_path, target.chrome_manifest_path, target.sidecar_path):
        assert not path.exists() and str(path) in result["removed"]
    assert registry.values == {}
    if target.platform == "windows":
        assert not target.manifest_dir.exists()
    assert (tmp_path / "chosen" / "registry.sqlite").is_file()


def test_chrome_alone_uninstall_without_firefox_removes_the_host_config(target, registry,
                                                                        tmp_path):
    install(create_registry(tmp_path / "chosen"), target=target, browsers=("chrome",))
    result = uninstall(target=target, browsers=("chrome",))
    assert str(target.sidecar_path) in result["removed"] and not target.sidecar_path.exists()
    assert registry.values == {}


def test_status_chrome(target, registry, tmp_path, env_home):
    s = status(target=target, browser="chrome")
    assert s["registered"] is False and "not registered for Chrome" in s["problems"][0]
    assert "--browser chrome" in s["problems"][0]
    install(target=target, browsers=("chrome",))
    doc = json.loads(target.chrome_manifest_path.read_text(encoding="utf-8"))
    doc["allowed_origins"] = ["chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"]
    target.chrome_manifest_path.write_text(json.dumps(doc), encoding="utf-8")
    s = status(target=target, browser="chrome")
    assert any("aaaa" in p and CHROME_ORIGIN in p for p in s["problems"])
    doc.pop("allowed_origins")
    target.chrome_manifest_path.write_text(json.dumps(doc), encoding="utf-8")
    s = status(target=target, browser="chrome")
    assert not s["manifest_valid"] and any("allowed_origins" in p for p in s["problems"])


def test_unknown_browser_is_refused(target, registry, env_home):
    for browsers in ("edge", (), ("firefox", "safari")):
        with pytest.raises(RegistryError) as info:
            install(target=target, browsers=browsers)
        assert info.value.code == "invalid_argument"
    with pytest.raises(RegistryError):
        status(target=target, browser="edge")
    assert nothing_written(target, registry)


def test_chrome_without_a_chrome_folder(tmp_path, registry, env_home):
    t = HostTarget("linux", tmp_path / "moz", tmp_path / "venv" / "bin" / "host", registry)
    t.launcher.parent.mkdir(parents=True)
    t.launcher.write_bytes(b"x")
    with pytest.raises(RegistryError) as info:
        install(target=t, browsers=("chrome",))
    assert info.value.code == "host_install_failed" and not t.manifest_dir.exists()
    install(target=t)                       # Firefox alone still works


# --------------------------------------------------------------------------- fail closed

class _UnreadableKey(FakeRegistry):
    """A FakeRegistry where reading one key fails (no access)."""

    def __init__(self, broken: str):
        super().__init__()
        self.broken: str | None = broken

    def read_default(self, key):
        if key == self.broken:
            raise PermissionError(13, "Access is denied")
        return super().read_default(key)


def test_unreadable_firefox_key_is_never_taken_for_not_registered(tmp_path, env_home):
    """Firefox is pinned to A (manifest in another folder) and its key cannot be read: registering
    Chrome with B must be refused, not accepted as 'Firefox not registered'."""
    fake = _UnreadableKey(REGISTRY_KEY)
    win = make_target(tmp_path, "windows", fake)
    a = create_registry(tmp_path / "A")
    b = create_registry(tmp_path / "B")
    fake.broken = None
    _registration_elsewhere(win, fake, a)
    fake.broken = REGISTRY_KEY
    before = dict(fake.values)
    for kwargs in ({"home": b, "browsers": ("chrome",)}, {"browsers": ("chrome",)},
                   {"home": b, "browsers": "both"}, {"home": b}):
        with pytest.raises(RegistryError) as info:
            install(target=win, **kwargs)
        assert info.value.code == "host_install_failed"
        assert "cannot be read" in info.value.message and "Firefox" in info.value.message
        assert fake.values == before and not win.manifest_dir.exists()
    # the host started by Chrome refuses too, while the key cannot be read
    assert chrome_home(win)[2] == "config_invalid"


def test_unreadable_chrome_key_refuses_a_firefox_install(tmp_path, env_home):
    fake = _UnreadableKey(CHROME_REGISTRY_KEY)
    win = make_target(tmp_path, "windows", fake)
    with pytest.raises(RegistryError) as info:
        install(create_registry(tmp_path / "A"), target=win)
    assert info.value.code == "host_install_failed" and "Chrome" in info.value.message
    assert fake.values == {} and not win.manifest_dir.exists()


def test_status_chrome_reports_an_unreadable_firefox_key(tmp_path, env_home):
    fake = _UnreadableKey(REGISTRY_KEY)
    win = make_target(tmp_path, "windows", fake)
    fake.broken = None
    install(create_registry(tmp_path / "A"), target=win, browsers="both")
    fake.broken = REGISTRY_KEY
    problems = status(target=win, browser="chrome")["problems"]
    assert any("Firefox's registration cannot be read" in p for p in problems)


def test_firefox_manifest_under_another_name_is_not_shared_with_chrome(win, registry, tmp_path,
                                                                       env_home):
    """Firefox's host ignores the host configuration next to a manifest that is
    not named compound_registry.json, so the installer must not assume it."""
    a = create_registry(tmp_path / "A")
    win.manifest_dir.mkdir(parents=True)
    other = win.manifest_dir / "other.json"
    other.write_text(json.dumps(manifest_document(win.launcher)), encoding="utf-8")
    registry.values[REGISTRY_KEY] = str(other)
    win.sidecar_path.write_text(json.dumps({"home": str(a)}), encoding="utf-8")
    before = dict(registry.values)
    # Chrome alone: refused (Firefox does not share the host configuration)
    with pytest.raises(RegistryError) as info:
        install(target=win, browsers=("chrome",))
    assert info.value.code == "host_install_conflict" and "--browser both" in info.value.message
    assert registry.values == before and chrome_nothing_written(win, registry)
    # both without a choice: Firefox is on the environment's registry, the file says A
    with pytest.raises(RegistryError) as info:
        install(target=win, browsers="both")
    assert info.value.code == "config_invalid" and "DIFFERENT registries" in info.value.message
    assert "other.json" in info.value.message and registry.values == before
    # an explicit choice for both browsers works
    install(a, target=win, browsers="both")
    assert extension_home(win) == (a, "host-config")
    assert chrome_home(win) == (a, "host-config", None)


def test_status_chrome_reports_a_firefox_manifest_under_another_name(win, registry, tmp_path,
                                                                     env_home):
    install(create_registry(tmp_path / "A"), target=win, browsers="both")
    other = win.manifest_dir / "other.json"
    other.write_bytes(win.manifest_path.read_bytes())
    registry.values[REGISTRY_KEY] = str(other)
    problems = status(target=win, browser="chrome")["problems"]
    assert any("other.json" in p and "not named" in p for p in problems)
    assert chrome_home(win)[2] == "config_invalid"


@pytest.mark.parametrize("browsers", [("chrome",), "both"])
def test_chrome_never_leaves_the_registry_unpinned(target, registry, tmp_path, env_home,
                                                   monkeypatch, browsers):
    """Unpinned, each browser would use $COMPOUND_REGISTRY_HOME as its own
    process sees it; the installer pins what it resolves now instead."""
    result = install(target=target, browsers=browsers)
    assert result["home_choice"] == "default-pinned" and result["home_override"] == str(env_home)
    assert read_host_config(target.sidecar_path) == (str(env_home), None)
    # the browsers' environments no longer matter
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "elsewhere"))
    assert chrome_home(target) == (env_home, "host-config", None)
    if browsers == "both":
        assert extension_home(target) == (env_home, "host-config")
    # --default-home with Chrome pins the default as resolved now, too
    other = create_registry(tmp_path / "elsewhere")
    result = install(target=target, browsers="both", default_home=True)
    assert result["home_override"] == str(other) and target.sidecar_path.is_file()
    assert chrome_home(target) == (other, "host-config", None)


def test_firefox_default_home_keeps_chrome_pinned(target, registry, tmp_path, env_home):
    """Firefox alone with --default-home must not leave Chrome's shared host
    configuration unpinned (Chrome would then refuse, or differ from Firefox)."""
    install(create_registry(tmp_path / "A"), target=target, browsers="both")
    result = install(target=target, default_home=True)                  # Firefox only
    assert result["home_choice"] == "default-pinned" and not result["sidecar_removed"]
    assert read_host_config(target.sidecar_path) == (str(env_home), None)
    assert chrome_home(target) == (env_home, "host-config", None)
    assert extension_home(target) == (env_home, "host-config")
    # without Chrome, --default-home still removes the pin (unchanged)
    uninstall(target=target, browsers=("chrome",))
    result = install(target=target, default_home=True)
    assert result["home_choice"] == "default" and result["sidecar_removed"]
    assert extension_home(target) == (None, "env")


def test_status_chrome_without_host_config(target, registry, tmp_path, env_home):
    install(create_registry(tmp_path / "A"), target=target, browsers=("chrome",))
    target.sidecar_path.unlink()
    problems = status(target=target, browser="chrome")["problems"]
    assert any("no host configuration" in p for p in problems)
    assert chrome_home(target)[2] == "config_invalid"


def test_linux_chrome_folder_follows_xdg_config_home(tmp_path, monkeypatch):
    xdg = "/home/tester/xdg"           # only a path computation; nothing is written
    monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    lx = default_target(platform="linux", prefix=tmp_path / "v")
    assert lx.chrome_manifest_dir == Path(xdg) / "google-chrome" / "NativeMessagingHosts"
    # unset, empty or relative: ~/.config, like Chrome
    for value in (None, "", "relative/xdg"):
        if value is None:
            monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        else:
            monkeypatch.setenv("XDG_CONFIG_HOME", value)
        lx = default_target(platform="linux", prefix=tmp_path / "v")
        assert lx.chrome_manifest_dir == (Path.home() / ".config" / "google-chrome"
                                          / "NativeMessagingHosts")
    # tests (user_home given) never see the real variable; an explicit config_home wins
    monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    lx = default_target(platform="linux", prefix=tmp_path / "v", user_home=tmp_path / "u")
    assert lx.chrome_manifest_dir == tmp_path / "u" / ".config" / "google-chrome" \
        / "NativeMessagingHosts"
    lx = default_target(platform="linux", prefix=tmp_path / "v", config_home=tmp_path / "c")
    assert lx.chrome_manifest_dir == tmp_path / "c" / "google-chrome" / "NativeMessagingHosts"
    # macOS is not affected
    m = default_target(platform="macos", prefix=tmp_path / "v")
    assert "google-chrome" not in str(m.chrome_manifest_dir)


# --------------------------------------------------------------------------- configured, warnings

def test_configured_reports_what_is_registered(target, tmp_path, env_home):
    from compound_registry.install_host import configured

    nothing = configured(target=target)
    assert nothing["browsers"] == [] and nothing["home"] is None
    assert set(nothing["status"]) == {"firefox", "chrome"}
    chosen = create_registry(tmp_path / "chosen")
    install(chosen, target=target, browsers=("chrome",))
    now = configured(target=target)
    assert now["browsers"] == ["chrome"] and now["home"] == str(chosen)
    install(target=target, browsers="both")
    assert configured(target=target)["browsers"] == ["firefox", "chrome"]


def test_configured_without_a_chrome_folder(tmp_path, registry, env_home):
    t = make_target(tmp_path, "linux", registry)
    t = HostTarget(t.platform, t.manifest_dir, t.launcher)        # Chrome unknown here
    install(target=t)
    now = configured_of(t)
    assert now["browsers"] == ["firefox"] and set(now["status"]) == {"firefox"}
    assert now["home"] == str(env_home)


def configured_of(t: HostTarget) -> dict:
    from compound_registry.install_host import configured

    return configured(target=t)


def test_install_warns_about_a_sync_folder(target, tmp_path, monkeypatch):
    cloud = tmp_path / "OneDrive - Example"
    monkeypatch.setenv("OneDrive", str(cloud))
    result = install(create_registry(cloud / "reg"), target=target)
    assert [w for w in result["warnings"] if "is inside OneDrive" in w]
    created = install(cloud / "new", target=target, create_registry=True)
    assert len([w for w in created["warnings"] if "is inside OneDrive" in w]) == 1
    local = install(create_registry(tmp_path / "local"), target=target)
    assert not [w for w in local["warnings"] if "OneDrive" in w]


def test_no_trace_of_the_pre_release_manifest_folder(win, registry, env_home):
    result = install(target=win)
    assert "legacy_removed" not in result
    assert not hasattr(win, "legacy_manifest_dir")
