"""Creating a registry (service.create_registry, 'init', install-host) and
what an upgrade of the program does to an existing one."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tomllib
from contextlib import closing
from pathlib import Path

import pytest

from compound_registry import __version__, install_host, sqlite_repo
from compound_registry.cli import main
from compound_registry.config import (
    DEFAULT_BACKUP_FOLDER,
    DEFAULT_CONFIG_TEXT,
    DEFAULT_DATABASE,
    DEFAULT_FILES_FOLDER,
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_KEEP,
    DEFAULT_STEREO_MODE,
    config_text,
    load_config,
)
from compound_registry.errors import ConfigError, FormatMismatchError, RegistryError
from compound_registry.idformat import IdFormat
from compound_registry.journal import JOURNAL_FILENAME
from compound_registry.models import ExperimentRef, Normalized
from compound_registry.service import (
    FINGERPRINT_KEY,
    RegistryService,
    chemistry_fingerprint,
    create_registry,
    open_with_system,
    upgrade_registry,
)

LAB = "LAB{core:04d}_{stereo:02d}_{batch:03d}"
OLD_FINGERPRINT = "rdkit=2020.03.1;inchi=1.05;keyalg=1"
ETHANOL = "CCO"
AMINE_R = "C[C@H](N)c1ccccc1"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "home"


def stored_format(db: Path) -> str:
    with closing(sqlite3.connect(str(db))) as c:
        return c.execute("SELECT value FROM meta WHERE key = 'id_format'").fetchone()[0]


# --------------------------------------------------------------------------- config text

def test_the_default_config_text_states_the_defaults_once():
    data = tomllib.loads(DEFAULT_CONFIG_TEXT)
    assert data == {
        "id": {"format": IdFormat.DEFAULT},
        "storage": {"database": DEFAULT_DATABASE},
        "backup": {"folder": DEFAULT_BACKUP_FOLDER, "interval_hours": DEFAULT_INTERVAL_HOURS,
                   "keep": DEFAULT_KEEP},
        "chemistry": {"default_stereo_mode": DEFAULT_STEREO_MODE},
        "files": {"folder": DEFAULT_FILES_FOLDER},
    }
    assert "<" not in DEFAULT_CONFIG_TEXT and config_text() == DEFAULT_CONFIG_TEXT
    assert "backup folder of its own" in DEFAULT_CONFIG_TEXT


def test_config_text_with_an_id_format(tmp_path):
    text = config_text(LAB)
    assert tomllib.loads(text)["id"]["format"] == LAB
    assert text.replace(LAB, IdFormat.DEFAULT) == DEFAULT_CONFIG_TEXT
    with pytest.raises(ConfigError):
        config_text("no fields")
    defaults = load_config(tmp_path / "nothing", create=False)
    assert defaults.backup_keep == DEFAULT_KEEP
    assert defaults.backup_interval_hours == float(DEFAULT_INTERVAL_HOURS)
    assert defaults.db_path.name == DEFAULT_DATABASE


# --------------------------------------------------------------------------- create_registry

def test_creates_config_and_database(home):
    config, warnings = create_registry(home)
    assert warnings == []
    assert Path(config.config_path).read_text(encoding="utf-8") == DEFAULT_CONFIG_TEXT
    assert stored_format(config.db_path) == IdFormat.DEFAULT
    with RegistryService.open(home) as svc:
        assert svc.register(smiles=ETHANOL, experiment_ref="E1")["id"] == "000001_01_001"


def test_creates_with_the_chosen_id_format(home):
    config, warnings = create_registry(home, LAB)
    assert warnings == [] and config.id_format == LAB
    assert stored_format(config.db_path) == LAB
    assert tomllib.loads(Path(config.config_path).read_text(encoding="utf-8"))["id"]["format"] == LAB
    with RegistryService.open(home) as svc:
        assert svc.register(smiles=ETHANOL, experiment_ref="E1")["id"] == "LAB0001_01_001"


def test_uses_the_default_home_without_an_argument(tmp_path):
    config, _ = create_registry()
    assert Path(config.home) == tmp_path / "env-home" and Path(config.db_path).is_file()


@pytest.mark.parametrize("template", ["{core:04d}/{stereo:02d}/{batch:03d}",
                                      "{core:04d}:{stereo:02d}:{batch:03d}", "no fields"])
def test_an_unusable_format_creates_nothing(home, template):
    with pytest.raises(ConfigError):
        create_registry(home, template)
    assert not home.exists()


def test_a_format_is_refused_when_anything_exists(home):
    create_registry(home)
    before = (home / "config.toml").read_bytes()
    with pytest.raises(RegistryError) as info:
        create_registry(home, LAB)
    assert info.value.code == "invalid_argument" and "cannot be changed" in info.value.message
    assert (home / "config.toml").read_bytes() == before
    (home / "registry.sqlite").unlink()                 # a config.toml alone names a format too
    with pytest.raises(RegistryError) as info:
        create_registry(home, LAB)
    assert info.value.code == "invalid_argument"
    assert not (home / "registry.sqlite").exists()


def test_an_existing_registry_is_not_created_again(home):
    create_registry(home)
    with pytest.raises(RegistryError) as info:
        create_registry(home)
    assert info.value.code == "registry_exists"


def test_a_prepared_config_gives_the_format(home):
    home.mkdir()
    (home / "config.toml").write_text(f'[id]\nformat = "{LAB}"\n', encoding="utf-8")
    config, _ = create_registry(home)
    assert stored_format(config.db_path) == LAB
    assert (home / "config.toml").read_text(encoding="utf-8") == f'[id]\nformat = "{LAB}"\n'


def test_a_prepared_config_with_an_unusable_format_creates_nothing(home):
    home.mkdir()
    (home / "config.toml").write_text('[id]\nformat = "{core:04d}:{stereo:02d}:{batch:03d}"\n',
                                      encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        create_registry(home)
    assert "folder and file names" in info.value.message
    assert not (home / "registry.sqlite").exists()


def test_a_database_whose_config_was_lost_gets_a_default_config(home):
    create_registry(home)
    (home / "config.toml").unlink()
    config, _ = create_registry(home)
    assert Path(config.config_path).read_text(encoding="utf-8") == DEFAULT_CONFIG_TEXT


def test_a_config_written_for_a_database_of_another_format_is_removed_again(home):
    create_registry(home, LAB)
    (home / "config.toml").unlink()
    with pytest.raises(FormatMismatchError):
        create_registry(home)                           # the default does not fit the database
    assert not (home / "config.toml").exists()
    assert stored_format(home / "registry.sqlite") == LAB


def test_a_surviving_journal_is_continued_and_warned_about(home):
    config, _ = create_registry(home)
    with RegistryService.open(home) as svc:
        svc.register(smiles=ETHANOL, experiment_ref="E1")
        rid = svc.repo.registry_id
    Path(config.db_path).unlink()
    (home / "config.toml").unlink()
    config, warnings = create_registry(home)
    (warning,) = warnings
    assert "issued-ID journal" in warning and "compound-registry recover" in warning
    assert str(home / JOURNAL_FILENAME) in warning
    with RegistryService.open(home) as svc:
        assert svc.repo.registry_id == rid
        assert svc.recover()["recovered"] == ["000001_01_001"]


def test_the_warning_about_a_synced_folder_is_returned(home, monkeypatch, tmp_path):
    monkeypatch.setenv("OneDrive", str(tmp_path))
    _config, warnings = create_registry(home)
    (warning,) = warnings
    assert "OneDrive" in warning and "local disk" in warning


# --------------------------------------------------------------------------- the two callers

def test_init_creates_through_the_same_function(home, capsys):
    assert main(["--home", str(home), "--json", "init", "--id-format", LAB]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["created"] is True and result["id_format"] == LAB and result["warnings"] == []
    assert main(["--home", str(home), "--json", "init"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is False
    assert main(["--home", str(home), "--json", "init", "--id-format", LAB]) != 0
    assert "invalid_argument" in capsys.readouterr().out + capsys.readouterr().err
    assert main(["--home", str(home / "bad"), "--json", "init", "--id-format",
                 "{core:04d}/{stereo:02d}/{batch:03d}"]) != 0
    capsys.readouterr()
    assert not (home / "bad").exists()


def _target(tmp_path) -> install_host.HostTarget:
    from test_install_host import FakeRegistry, make_target

    return make_target(tmp_path, "linux", FakeRegistry())


def test_install_host_creates_with_an_id_format(tmp_path):
    new = tmp_path / "new"
    result = install_host.install(new, create_registry=True, id_format=LAB,
                                  target=_target(tmp_path))
    assert result["registry"]["created"] is True
    assert stored_format(new / "registry.sqlite") == LAB
    with pytest.raises(RegistryError) as info:
        install_host.install(new, create_registry=True, id_format=LAB, target=_target(tmp_path))
    assert info.value.code == "invalid_argument"
    with pytest.raises(ConfigError):
        install_host.install(tmp_path / "bad", create_registry=True,
                             id_format="{core:04d}:{stereo:02d}:{batch:03d}",
                             target=_target(tmp_path))
    assert not (tmp_path / "bad").exists()


# --------------------------------------------------------------------------- after an upgrade

def test_doctor_names_program_schema_and_registry(home):
    with RegistryService.open(home) as svc:
        doctor = svc.doctor()
        assert doctor["program_version"] == __version__
        assert doctor["schema_version"] == sqlite_repo.SCHEMA_VERSION == 1
        assert doctor["registry_id"] == svc.repo.registry_id
        assert doctor["read_only"] is False


def test_a_file_in_the_way_of_the_folder_is_named_as_such(tmp_path):
    (tmp_path / "afile").write_text("x", encoding="utf-8")
    for folder in (tmp_path / "afile", tmp_path / "afile" / "sub"):
        with pytest.raises(RegistryError) as info:
            create_registry(folder)
        assert info.value.code == "config_invalid"
        assert f"The folder {folder} cannot be created" in info.value.message
        assert "another program" not in info.value.message
    assert sorted(p.name for p in tmp_path.iterdir()) == ["afile"]


def test_upgrade_registry_never_creates_one(home):
    result = upgrade_registry(home)
    assert result["status"] == "no_registry" and str(home) in result["message"]
    assert not home.exists()
    home.mkdir()
    (home / "config.toml").write_text("", encoding="utf-8")
    assert upgrade_registry(home)["status"] == "no_registry"
    assert not (home / "registry.sqlite").exists()


def test_upgrade_with_unchanged_chemistry_checks_and_writes_nothing(home):
    with RegistryService.open(home) as svc:
        svc.register(smiles=ETHANOL, experiment_ref="E1")
        db = svc.config.db_path
    before = Path(db).read_bytes()
    result = upgrade_registry(home)
    assert result["status"] == "current" and result["checked"] == 0
    assert result["mismatches"] == [] and result["schema_upgrade"] is None
    assert result["program_version"] == __version__ and result["schema_version"] == 1
    assert result["home"] == str(home) and len(result["registry_id"]) == 32
    assert result["chemistry_fingerprint"]["changed"] is False
    assert Path(db).read_bytes() == before


def test_upgrade_accepts_a_new_chemistry_version_when_all_keys_are_unchanged(home):
    with RegistryService.open(home) as svc:
        svc.register(smiles=ETHANOL, experiment_ref="E1")
        svc.register(smiles=AMINE_R, experiment_ref="E2")
        svc.repo.set_meta(FINGERPRINT_KEY, OLD_FINGERPRINT)
    with RegistryService.open(home) as svc:
        with pytest.raises(RegistryError) as info:
            svc.register(smiles="CCCC", experiment_ref="E3")
        assert info.value.code == "chemistry_version_changed"
        assert "Run the installer again" in info.value.message
        assert "rekey-check --accept" in info.value.message
    result = upgrade_registry(home)
    assert result["status"] == "accepted" and result["checked"] == 2
    assert result["message"].startswith("2 structure(s) checked, keys unchanged, new chemistry "
                                        "version accepted")
    assert result["chemistry_fingerprint"] == {"stored": chemistry_fingerprint(),
                                               "current": chemistry_fingerprint(),
                                               "changed": False}
    with RegistryService.open(home) as svc:
        assert svc.register(smiles="CCCC", experiment_ref="E3")["created"]
    assert upgrade_registry(home)["status"] == "current"


def test_upgrade_changes_nothing_when_a_key_differs(home):
    with RegistryService.open(home) as svc:
        svc.register(smiles=AMINE_R, experiment_ref="E1")
        fake = Normalized(core_key="InChI=1S/C2H6O/fake", stereo_key="[]", stereo_mode="none",
                          inchikey="FAKE", smiles="CCO",
                          molblock=svc.get("000001_01_001", with_molfile=True)["molfile"])
        svc.repo.register(fake, ExperimentRef("E2"), 1)
        svc.repo.set_meta(FINGERPRINT_KEY, OLD_FINGERPRINT)
    result = upgrade_registry(home)
    assert result["status"] == "blocked" and result["checked"] == 2
    assert [m["id"] for m in result["mismatches"]] == ["000002_01_001"]
    message = result["message"]
    assert "000002_01_001" in message and "Nothing was changed" in message
    assert "registering stays blocked" in message and "previous program version" in message
    assert result["chemistry_fingerprint"]["stored"] == OLD_FINGERPRINT
    with RegistryService.open(home) as svc:
        assert svc.repo.get_meta(FINGERPRINT_KEY) == OLD_FINGERPRINT
        assert svc.chemistry_upgrade()["status"] == "blocked"
        with pytest.raises(RegistryError) as info:
            svc.register(smiles=ETHANOL, experiment_ref="E3")
        assert info.value.code == "chemistry_version_changed"


# --------------------------------------------------------------------------- opening a file on macOS

class FakeProcess:
    def __init__(self, code: int | None):
        self.code = code

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("open", timeout)
        return self.code


def test_open_with_system_hands_the_file_over_without_the_hosts_pipes():
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess(0)

    open_with_system("/tmp/route.cdxml", popen=popen)
    ((argv, kwargs),) = calls
    assert argv == ["open", "/tmp/route.cdxml"]
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True and kwargs["close_fds"] is True
    open_with_system("/tmp/route.cdxml", popen=lambda *a, **k: FakeProcess(None))  # still busy
    with pytest.raises(OSError) as info:
        open_with_system("/tmp/route.cdxml", popen=lambda *a, **k: FakeProcess(1))
    assert "code 1" in str(info.value)
