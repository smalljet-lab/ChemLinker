"""Tests for compound_registry.config."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from compound_registry.config import (
    DEFAULT_CONFIG_TEXT,
    Config,
    default_home,
    load_config,
    sync_folder_warning,
)
from compound_registry.errors import ConfigError
from compound_registry.idformat import IdFormat


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Never touch the real home folder; no OneDrive variables leak in."""
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


def write_config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")


def test_default_home_from_env(tmp_path):
    assert default_home() == tmp_path / "home"


def test_default_home_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("COMPOUND_REGISTRY_HOME")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    assert default_home() == tmp_path / "user" / "CompoundRegistry"


def test_creates_default_file(tmp_path):
    cfg = load_config()
    home = tmp_path / "home"
    assert isinstance(cfg, Config)
    assert cfg.home == home
    assert cfg.config_path == home / "config.toml"
    assert cfg.config_path.read_text(encoding="utf-8") == DEFAULT_CONFIG_TEXT
    assert cfg.db_path == home / "registry.sqlite"
    assert cfg.backup_dir == home / "backups"
    assert cfg.backup_interval_hours == 24.0
    assert cfg.backup_keep == 30
    assert cfg.id_format == IdFormat.DEFAULT
    assert cfg.default_stereo_mode == "abs"
    # the written template is valid TOML and contains the documented keys
    data = tomllib.loads(DEFAULT_CONFIG_TEXT)
    assert data["id"]["format"] == IdFormat.DEFAULT
    assert data["backup"]["keep"] == 30
    # loading again does not rewrite
    assert load_config() == cfg


def test_no_create_uses_defaults_and_writes_nothing(tmp_path):
    home = tmp_path / "elsewhere"
    cfg = load_config(home, create=False)
    assert not home.exists()
    assert cfg.db_path == home / "registry.sqlite"
    assert cfg.backup_keep == 30


def test_explicit_home_argument(tmp_path):
    cfg = load_config(tmp_path / "explicit")
    assert cfg.config_path == tmp_path / "explicit" / "config.toml"
    assert cfg.config_path.exists()


def test_custom_values_and_relative_paths(tmp_path):
    home = tmp_path / "h"
    write_config(home, """
[id]
format = "UB-{core:05d}_{stereo:02d}_{batch:03d}"
[storage]
database = "data/reg.sqlite"
[backup]
folder = "../shared/bk"
interval_hours = 0.5
keep = 0
[chemistry]
default_stereo_mode = "rac"
[some_other_component]
anything = 1
""")
    cfg = load_config(home)
    assert cfg.id_format == "UB-{core:05d}_{stereo:02d}_{batch:03d}"
    assert cfg.db_path == home / "data" / "reg.sqlite"
    assert cfg.backup_dir == tmp_path / "shared" / "bk"
    assert cfg.backup_interval_hours == 0.5
    assert cfg.backup_keep == 0
    assert cfg.default_stereo_mode == "rac"


def test_absolute_and_tilde_paths(tmp_path, monkeypatch):
    fake_user = tmp_path / "user"
    monkeypatch.setenv("USERPROFILE", str(fake_user))
    monkeypatch.setenv("HOME", str(fake_user))
    abs_db = tmp_path / "abs" / "r.sqlite"
    home = tmp_path / "h"
    write_config(home, f"""
[storage]
database = '{abs_db}'
[backup]
folder = "~/bk"
""")
    cfg = load_config(home)
    assert cfg.db_path == abs_db
    assert cfg.backup_dir == fake_user / "bk"


def test_missing_sections_use_defaults(tmp_path):
    home = tmp_path / "h"
    write_config(home, "[backup]\nkeep = 5\n")
    cfg = load_config(home)
    assert cfg.backup_keep == 5
    assert cfg.id_format == IdFormat.DEFAULT
    assert cfg.db_path == home / "registry.sqlite"


@pytest.mark.parametrize(
    "text, key",
    [
        ('[id]\nformat = "{core}_{stereo}_{batch}"\n', "id.format"),
        ("[id]\nformat = 5\n", "id.format"),
        ('[storage]\ndatabase = ""\n', "storage.database"),
        ("[storage]\ndatabase = 3\n", "storage.database"),
        ('[backup]\nfolder = ""\n', "backup.folder"),
        ('[backup]\ninterval_hours = "daily"\n', "backup.interval_hours"),
        ("[backup]\ninterval_hours = -1\n", "backup.interval_hours"),
        ("[backup]\ninterval_hours = true\n", "backup.interval_hours"),
        ("[backup]\ninterval_hours = nan\n", "backup.interval_hours"),
        ("[backup]\nkeep = -1\n", "backup.keep"),
        ("[backup]\nkeep = 2.5\n", "backup.keep"),
        ('[chemistry]\ndefault_stereo_mode = "partial"\n', "chemistry.default_stereo_mode"),
        ("[backup]\nkepp = 3\n", "kepp"),
        ('id = "x"\n', "[id]"),
        ("this is not toml", "TOML"),
    ],
)
def test_invalid_values_name_the_key(tmp_path, text, key):
    home = tmp_path / "h"
    write_config(home, text)
    with pytest.raises(ConfigError) as info:
        load_config(home)
    assert key in info.value.message
    assert info.value.code == "config_invalid"


@pytest.mark.parametrize("value", ["relative-home", "sub/dir", "./home", "D:reg-home"])
def test_relative_home_from_env_rejected(monkeypatch, tmp_path, value):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", value)
    with pytest.raises(ConfigError) as info:
        default_home()
    assert "absolute" in info.value.message and "COMPOUND_REGISTRY_HOME" in info.value.message
    with pytest.raises(ConfigError):
        load_config()
    assert not (Path.cwd() / value).exists()


@pytest.mark.parametrize("value", ["relative-home", Path("a") / "b", "D:reg-home"])
def test_relative_home_argument_rejected(value):
    with pytest.raises(ConfigError) as info:
        load_config(value)
    assert "absolute" in info.value.message


def test_tilde_home_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", "~/reg")
    assert default_home() == tmp_path / "user" / "reg"
    assert load_config("~/reg2", create=False).home == tmp_path / "user" / "reg2"


@pytest.mark.parametrize(
    "text, key",
    [
        ("[storage]\ndatabase = 'D:reg.sqlite'\n", "storage.database"),
        ("[storage]\ndatabase = 'c:data\\\\reg.sqlite'\n", "storage.database"),
        ("[backup]\nfolder = 'E:backups'\n", "backup.folder"),
    ],
)
def test_drive_relative_paths_rejected(tmp_path, text, key):
    home = tmp_path / "h"
    write_config(home, text)
    with pytest.raises(ConfigError) as info:
        load_config(home)
    assert key in info.value.message


def test_drive_absolute_and_plain_relative_paths_still_work(tmp_path):
    home = tmp_path / "h"
    write_config(home, "[storage]\ndatabase = 'data/reg.sqlite'\n[backup]\nfolder = 'bk'\n")
    cfg = load_config(home)
    assert cfg.db_path == home / "data" / "reg.sqlite" and cfg.backup_dir == home / "bk"


def test_backup_folder_equal_to_database_rejected(tmp_path):
    home = tmp_path / "h"
    write_config(home, '[storage]\ndatabase = "x"\n[backup]\nfolder = "x"\n')
    with pytest.raises(ConfigError):
        load_config(home)


# ----------------------------------------------------------------------------- sync folders

def test_local_path_has_no_warning(tmp_path):
    assert sync_folder_warning(tmp_path / "registry.sqlite") is None


@pytest.mark.parametrize(
    "parts",
    [
        ("OneDrive", "Documents"),
        ("OneDrive - Contoso Ltd", "lab"),
        ("Google Drive",),
        ("My Drive", "lab"),
        ("Dropbox",),
        ("Dropbox (Team)",),
        ("iCloudDrive",),
        ("onedrive",),
    ],
)
def test_sync_folder_components(tmp_path, parts):
    path = tmp_path.joinpath(*parts, "registry.sqlite")
    warning = sync_folder_warning(path)
    assert warning is not None
    assert "local disk" in warning


def test_onedrive_env_var(tmp_path, monkeypatch):
    root = tmp_path / "CorpSync"
    monkeypatch.setenv("OneDriveCommercial", str(root))
    assert sync_folder_warning(root / "sub" / "r.sqlite") is not None
    assert sync_folder_warning(tmp_path / "CorpSyncOther" / "r.sqlite") is None


def test_unc_path_warns():
    assert sync_folder_warning(Path(r"\\server\share\registry.sqlite")) is not None


def test_similar_names_do_not_warn(tmp_path):
    assert sync_folder_warning(tmp_path / "MyDropboxNotes" / "r.sqlite") is None
    assert sync_folder_warning(tmp_path / "OneDriveBackupScripts" / "r.sqlite") is None
