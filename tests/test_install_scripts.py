"""The installer and updater scripts at the project root.

The scripts are read as text, checked for syntax, and the update scripts are
run in a copy with a stand-in installer. Nothing is installed: no uv, no
Python environment, no browser registration.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from compound_registry.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("install.ps1", "install.sh", "update.ps1", "update.sh", "install.cmd", "update.cmd")
SH = shutil.which("sh")
POWERSHELL = shutil.which("powershell")
GIT = shutil.which("git")
CMD = shutil.which("cmd") if sys.platform == "win32" else None


def text_of(name: str) -> str:
    return (ROOT / name).read_bytes().decode("ascii")       # ASCII only: PowerShell 5.1, cmd


def setup_options() -> list[str]:
    parser = build_parser()
    for action in parser._subparsers._group_actions:         # noqa: SLF001 - argparse has no API
        setup = action.choices["setup"]
        return sorted(option for a in setup._actions for option in a.option_strings
                      if option.startswith("--") and option != "--help")
    raise AssertionError("no subcommands")


def run(argv, cwd, **kwargs):
    done = subprocess.run([str(a) for a in argv], cwd=str(cwd), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
                          check=False, **kwargs)
    return done.returncode, done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


# --------------------------------------------------------------------------- as text

@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_plain_ascii(name):
    text = text_of(name)
    assert "\t" not in text
    if name.endswith(".sh"):
        assert "\r" not in text and text.startswith("#!/bin/sh\n")


@pytest.mark.parametrize("name", ["install.ps1", "install.sh"])
def test_the_installer_documents_every_setup_option(name):
    text = text_of(name)
    options = setup_options()
    assert "--home" in options and "--dry-run" in options and len(options) == 12
    for option in options:
        assert re.search(rf"^\s*#?\s*{re.escape(option)}\b", text, re.MULTILINE), option


@pytest.mark.parametrize("name", ["install.ps1", "install.sh"])
def test_the_installer_only_bootstraps(name):
    text = text_of(name)
    assert "uv sync --locked --no-dev" in text
    assert ".python" in text and ".uv-cache" in text
    assert "UV_PYTHON_INSTALL_DIR" in text and "UV_CACHE_DIR" in text
    assert "Install uv now? [y/N]" in text                  # never without asking
    assert "Close Firefox/Chrome" in text and "then run this again" in text
    assert "compound-registry-host" in text                  # looks for the running program
    assert "Next steps" not in text and "npm" not in text    # the one list is the wizard's
    assert "xpinstall" not in text and "install-host" not in text


def test_the_official_uv_installers_are_used():
    assert "irm https://astral.sh/uv/install.ps1 | iex" in text_of("install.ps1")
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in text_of("install.sh")


def test_the_old_powershell_parameters_still_exist():
    text = text_of("install.ps1")
    for parameter, option in (("$RegistryHome", '"--home"'), ("$CreateRegistry", '"--create-registry"'),
                              ("$Browser", '"--browser"'), ("$Force", '"--force"')):
        assert parameter in text and option in text
    # Windows PowerShell 5.1: no pipeline chain operators, no ternary
    for name in ("install.ps1", "update.ps1"):
        code = "\n".join(line for line in text_of(name).splitlines()
                         if not line.lstrip().startswith("#"))
        assert "&&" not in code and "||" not in code and " ? " not in code


@pytest.mark.parametrize("name", ["update.ps1", "update.sh"])
def test_the_updater(name):
    text = text_of(name)
    assert "pull --ff-only" in text and "status --porcelain" in text
    assert "--upgrade" in text and "--no-pull" in text
    assert "not a git checkout" in text and "unpack it over this folder" in text
    assert "install.ps1" in text if name.endswith(".ps1") else "install.sh" in text


@pytest.mark.parametrize("name", ["install", "update"])
def test_the_double_click_wrappers(name):
    text = text_of(f"{name}.cmd")
    assert "\r\n" in text
    lines = text.split("\r\n")
    assert lines[0] == "@echo off" and lines[-1] == ""
    assert all(line.startswith("rem ") for line in lines[1:-2])
    # cmd reads the file again after every line, and an update replaces the file: everything
    # that runs is on the one line that starts the script
    assert lines[-2] == (f'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0{name}.ps1" %*'
                         ' & call set "CODE=%%ERRORLEVEL%%" & echo.& pause'
                         " & call exit /b %%CODE%%")


def run_line(line: str, cwd: Path):
    """Run a command line exactly as typed (no quoting by Python)."""
    done = subprocess.run(line, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=120, check=False)
    return done.returncode, done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


@pytest.mark.skipif(CMD is None or POWERSHELL is None, reason="no cmd / Windows PowerShell")
@pytest.mark.parametrize("name", ["install", "update"])
def test_a_wrapper_that_is_replaced_while_it_runs(tmp_path, name):
    """What 'git pull' does to update.cmd in the middle of an update: the
    run that is under way must end as the old file says, with the exit code
    of the script."""
    folder = tmp_path / "project folder"
    folder.mkdir()
    wrapper = folder / f"{name}.cmd"
    shutil.copy(ROOT / f"{name}.cmd", wrapper)
    newer = text_of(f"{name}.cmd").replace(
        "powershell ", "echo A NEW LINE\r\necho ANOTHER NEW LINE\r\npowershell ", 1)
    (folder / "newer.txt").write_bytes(newer.encode("ascii"))
    (folder / f"{name}.ps1").write_text(
        'foreach ($a in $args) { Write-Host "script got [$a]" }\r\n'
        f'Copy-Item -Force (Join-Path $PSScriptRoot "newer.txt") (Join-Path $PSScriptRoot "{name}.cmd")\r\n'
        'exit 7\r\n', encoding="ascii", newline="")
    code, out = run_line(f'"{CMD}" /d /c .\\{name}.cmd --dry-run "a b"', folder)
    assert wrapper.read_bytes() == newer.encode("ascii")
    assert re.findall(r"script got \[(.*?)\]", out) == ["--dry-run", "a b"], out
    assert "NEW LINE" not in out and "not recognized" not in out, out
    assert code == 7


# --------------------------------------------------------------------------- syntax

@pytest.mark.skipif(SH is None, reason="no sh")
@pytest.mark.parametrize("name", ["install.sh", "update.sh"])
def test_sh_syntax_and_help(name):
    code, out = run([SH, "-n", ROOT / name], ROOT)
    assert code == 0, out
    code, out = run([SH, ROOT / name, "--help"], ROOT)
    assert code == 0 and out.startswith(("Installs", "Brings")) and "--dry-run" in out
    assert "set -u" not in out and "main()" not in out


@pytest.mark.skipif(POWERSHELL is None, reason="no Windows PowerShell")
@pytest.mark.parametrize("name", ["install.ps1", "update.ps1"])
def test_powershell_syntax(name):
    command = ("$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::"
               f"ParseFile('{ROOT / name}', [ref]$t, [ref]$e); "
               "foreach ($x in $e) { $x.Message }; exit $e.Count")
    code, out = run([POWERSHELL, "-NoProfile", "-Command", command], ROOT)
    assert code == 0, out


@pytest.mark.skipif(POWERSHELL is None, reason="no Windows PowerShell")
def test_powershell_arguments_survive_a_space_and_a_backslash_at_the_end(tmp_path):
    """A folder as Tab completion or a paste gives it must reach the program
    as one argument, with the arguments after it intact."""
    source = text_of("install.ps1")
    start = source.index("function Get-SafeArgument")
    function = source[start:source.index("function Test-Console")]
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        function + "\n"
        "$values = @('D:\\Lab Data\\Registry\\', '\"D:\\Lab Data\\Registry\\\"', \"'D:\\x y/'\",\n"
        "            'D:\\', '--yes', '  D:\\plain\\  ', '')\n"
        "$safe = @($values | ForEach-Object { Get-SafeArgument $_ })\n"
        "$safe | ForEach-Object { \"[$_]\" }\n"
        f"& '{sys.executable}' -c \"import sys; print(sys.argv[1:])\" @safe after\n",
        encoding="ascii")
    code, out = run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", probe],
                    tmp_path)
    assert code == 0, out
    lines = out.strip().splitlines()
    assert lines[:7] == ["[D:\\Lab Data\\Registry]", "[D:\\Lab Data\\Registry]", "[D:\\x y]",
                         "[D:\\]", "[--yes]", "[D:\\plain]", '[""]']
    assert lines[7] == str(["D:\\Lab Data\\Registry", "D:\\Lab Data\\Registry", "D:\\x y",
                            "D:\\", "--yes", "D:\\plain", "", "after"])


def argument_probe(folder: Path, name: str) -> Path:
    """``<name>.ps1`` up to where its arguments are ready, followed by a
    program that prints them, as ``folder/<name>.ps1``."""
    source = text_of(f"{name}.ps1")
    marker = "# --- uv ---" if name == "install" else "$Before = Get-ProgramVersion"
    arguments = "setup @SetupArgs" if name == "install" else "nopull=$NoPull @Rest"
    probe = folder / f"{name}.ps1"
    probe.write_text(
        source[:source.index(marker)]
        + f"& '{sys.executable}' -c \"import sys; print(sys.argv[1:])\" {arguments}\r\n",
        encoding="ascii", newline="")
    return probe


GLUED = [
    (r'--dry-run --yes --create-registry --home "D:\Lab Data\Registry\" --id-prefix ABC',
     ["--dry-run", "--yes", "--create-registry", "--home", r"D:\Lab Data\Registry",
      "--id-prefix", "ABC"]),
    (r'--home "D:\Lab Data\Registry\" --yes', ["--home", r"D:\Lab Data\Registry", "--yes"]),
    (r'-RegistryHome "D:\Lab Data\Registry\" --eln-origin "https://eln.example.org" --dry-run',
     ["--home", r"D:\Lab Data\Registry", "--eln-origin", "https://eln.example.org",
      "--dry-run"]),
    (r'--home "D:\Lab Data\Registry\"', ["--home", r"D:\Lab Data\Registry"]),
    (r'--home "D:\Lab Data\Registry" --yes', ["--home", r"D:\Lab Data\Registry", "--yes"]),
    (r'--home D:\Registry\ --mnova "no"', ["--home", r"D:\Registry", "--mnova", "no"]),
]


@pytest.mark.skipif(CMD is None or POWERSHELL is None, reason="no cmd / Windows PowerShell")
@pytest.mark.parametrize("typed, expected", GLUED)
def test_a_quoted_folder_with_a_backslash_at_the_end_on_the_real_command_line(
        tmp_path, typed, expected):
    """Started with 'powershell -File' or install.cmd, PowerShell itself reads
    a backslash in front of the closing quote as a quote character: the
    options after the folder must still arrive as options."""
    folder = tmp_path / "project folder (x86)"
    folder.mkdir()
    probe = argument_probe(folder, "install")
    shutil.copy(ROOT / "install.cmd", folder / "install.cmd")
    wanted = str(["setup", *expected])
    code, out = run_line(
        f'"{POWERSHELL}" -NoProfile -ExecutionPolicy Bypass -File "{probe}" {typed}', folder)
    assert code == 0 and out.strip() == wanted, out
    code, out = run_line(f'"{CMD}" /d /c .\\install.cmd {typed}', folder)
    assert code == 0 and out.strip().splitlines()[0] == wanted, out


@pytest.mark.skipif(POWERSHELL is None, reason="no Windows PowerShell")
def test_the_updater_finds_its_own_options_behind_such_a_folder(tmp_path):
    probe = argument_probe(tmp_path, "update")
    code, out = run_line(
        f'"{POWERSHELL}" -NoProfile -ExecutionPolicy Bypass -File "{probe}" '
        r'--home "D:\Lab Data\Registry\" --no-pull --dry-run', tmp_path)
    assert code == 0, out
    assert out.strip() == str(["nopull=True", "--home", r"D:\Lab Data\Registry", "--dry-run"])


# --------------------------------------------------------------------------- the updater, run

def updater_copy(tmp_path: Path, kind: str) -> Path:
    """A folder with the update script and an installer that only reports
    how it was called."""
    folder = tmp_path / "project folder"
    (folder / "src" / "compound_registry").mkdir(parents=True)
    (folder / "src" / "compound_registry" / "__init__.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8")
    shutil.copy(ROOT / f"update.{kind}", folder / f"update.{kind}")
    if kind == "sh":
        (folder / "install.sh").write_text(
            '#!/bin/sh\nfor a in "$@"; do echo "installer got [$a]"; done\nexit 7\n',
            encoding="ascii", newline="\n")
    else:
        (folder / "install.ps1").write_text(
            'foreach ($a in $args) { Write-Host "installer got [$a]" }\r\nexit 7\r\n',
            encoding="ascii", newline="")
    return folder


def run_updater(folder: Path, kind: str, *args: str):
    if kind == "sh":
        return run([SH, folder / "update.sh", *args], folder)
    return run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                folder / "update.ps1", *args], folder)


KINDS = [pytest.param("sh", marks=pytest.mark.skipif(SH is None, reason="no sh")),
         pytest.param("ps1", marks=pytest.mark.skipif(POWERSHELL is None,
                                                      reason="no Windows PowerShell"))]


@pytest.mark.parametrize("kind", KINDS)
def test_update_outside_a_git_checkout(tmp_path, kind):
    folder = updater_copy(tmp_path, kind)
    code, out = run_updater(folder, kind, "--mnova", "no")
    assert "installed program version 1.2.3" in out
    assert "not a git checkout" in out and "unpack it over this folder" in out
    assert re.findall(r"installer got \[(.*?)\]", out) == ["--upgrade", "--mnova", "no"]
    assert code == 7 and "The update did not finish (exit code 7)" in out


@pytest.mark.parametrize("kind", KINDS)
def test_update_no_pull_and_dry_run(tmp_path, kind):
    folder = updater_copy(tmp_path, kind)
    (folder / ".git").mkdir()                        # would be asked, but nothing is fetched
    code, out = run_updater(folder, kind, "--no-pull", "--browser", "both")
    assert "Not fetching anything" in out and "git pull" not in out
    assert re.findall(r"installer got \[(.*?)\]", out) == ["--upgrade", "--browser", "both"]
    code, out = run_updater(folder, kind, "--dry-run")
    assert "Dry run: nothing is fetched" in out
    assert re.findall(r"installer got \[(.*?)\]", out) == ["--upgrade", "--dry-run"]


def git(folder: Path, *args: str) -> str:
    code, out = run([GIT, "-c", "user.name=Test", "-c", "user.email=test@example.org",
                     "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
                     "-C", folder, *args], folder)
    assert code == 0, out
    return out


@pytest.mark.skipif(GIT is None, reason="no git")
@pytest.mark.parametrize("kind", KINDS)
def test_update_in_a_git_checkout(tmp_path, kind):
    upstream = updater_copy(tmp_path / "upstream", kind)
    git(upstream, "init", "-q", ".")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", "one")
    folder = tmp_path / "checkout"
    code, out = run([GIT, "clone", "-q", upstream, folder], tmp_path)
    assert code == 0, out
    version_file = Path("src") / "compound_registry" / "__init__.py"

    # nothing new
    code, out = run_updater(folder, kind)
    assert "git pull --ff-only" in out and "Program version: 1.2.3 (unchanged)" in out
    assert re.findall(r"installer got \[(.*?)\]", out) == ["--upgrade"]

    # a new version upstream
    (upstream / version_file).write_text('__version__ = "1.3.0"\n', encoding="utf-8")
    git(upstream, "commit", "-q", "-am", "two")
    code, out = run_updater(folder, kind)
    assert "Program version: 1.2.3 -> 1.3.0" in out
    assert (folder / version_file).read_text(encoding="utf-8") == '__version__ = "1.3.0"\n'

    # local changes: stop before fetching, and do not start the installer
    (upstream / version_file).write_text('__version__ = "1.4.0"\n', encoding="utf-8")
    git(upstream, "commit", "-q", "-am", "three")
    (folder / version_file).write_text('__version__ = "1.3.0"\n# mine\n', encoding="utf-8")
    code, out = run_updater(folder, kind)
    assert code == 1 and "changed locally" in out and "__init__.py" in out
    assert "git stash" in out and "installer got" not in out
    assert "# mine" in (folder / version_file).read_text(encoding="utf-8")

    # a diverged branch: git refuses, the installer is not started
    git(folder, "commit", "-q", "-am", "mine")
    code, out = run_updater(folder, kind)
    assert code == 1 and "git pull --ff-only failed" in out and "diverged" in out
    assert "installer got" not in out
    # ... unless the user says to go on with the files that are there
    code, out = run_updater(folder, kind, "--no-pull")
    assert re.findall(r"installer got \[(.*?)\]", out) == ["--upgrade"]


@pytest.mark.skipif(GIT is None, reason="no git")
@pytest.mark.parametrize("kind", KINDS)
def test_update_of_a_checkout_that_has_nothing_to_pull(tmp_path, kind):
    """A checked-out tag and a branch without upstream: said as such, before
    git is asked to pull."""
    upstream = updater_copy(tmp_path / "upstream", kind)
    git(upstream, "init", "-q", ".")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", "one")
    git(upstream, "tag", "v1.2.3")
    folder = tmp_path / "checkout"
    code, out = run([GIT, "-c", "advice.detachedHead=false", "clone", "-q", "--branch", "v1.2.3",
                     upstream, folder], tmp_path)
    assert code == 0, out
    no_pull = "--no-pull" if kind == "sh" else "-NoPull"

    code, out = run_updater(folder, kind)
    assert code == 1 and "at a fixed version (a tag or a single commit)" in out
    assert "git checkout main" in out and no_pull in out
    assert "git pull" not in out and "Usual reasons" not in out and "installer got" not in out

    git(folder, "checkout", "-q", "-b", "mine")
    code, out = run_updater(folder, kind)
    assert code == 1 and "The branch 'mine' of this checkout has no upstream branch" in out
    assert "git pull" not in out and "Usual reasons" not in out and "installer got" not in out

    code, out = run_updater(folder, kind, "--no-pull")
    assert re.findall(r"installer got \[(.*?)\]", out) == ["--upgrade"]
