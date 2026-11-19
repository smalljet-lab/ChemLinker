"""compound_registry.chemdraw: the bridge to the local ChemDraw, with fake runners.

ChemDraw is never started here (tests/conftest.py switches it off for every
test); the PowerShell script's output is simulated.
"""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

from compound_registry import chemdraw
from compound_registry.chemdraw import ChemDrawAnswer, ChemDrawError, name_molfile, parse_output

MOLFILE = "\n  test\n\n  1  0  0  0  0  0  0  0  0  0999 V2000\n" \
          "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\nM  END\n"
PROG_ID = "ChemDraw_x64.Application"
# The fixture below replaces it; one test calls the real one with a fake subprocess.run.
_REAL_STOP_LEFTOVERS = chemdraw._stop_leftovers
# The real Windows PowerShell (None when there is none): the quoting of parameter values is
# checked against it with a probe script that only echoes what it received.
_REAL_POWERSHELL = chemdraw._powershell()


def result_line(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
    return "result:" + base64.b64encode(raw).decode("ascii")


def decoded_command(args) -> str:
    """The PowerShell text behind ``-EncodedCommand``."""
    return base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")


class FakeRunner:
    """Records each call and answers with fixed output."""

    def __init__(self, output: str = "", timed_out: bool = False, error: Exception | None = None):
        self.output, self.timed_out, self.error = output, timed_out, error
        self.calls: list[tuple[list[str], str, float]] = []

    def __call__(self, args, stdin_text, timeout):
        self.calls.append((args, stdin_text, timeout))
        if self.error is not None:
            raise self.error
        return self.output, self.timed_out


def _never(*_args, **_kwargs):
    raise AssertionError("the real ChemDraw bridge must not be used in tests")


@pytest.fixture(autouse=True)
def _fake_powershell(monkeypatch):
    # tests/conftest.py switches ChemDraw off for every test. Here the switch is
    # lifted so that the bridge's own logic can be tested, and everything that
    # could start a real program is replaced instead.
    monkeypatch.delenv("COMPOUND_REGISTRY_NO_CHEMDRAW", raising=False)
    monkeypatch.setattr(chemdraw, "_powershell", lambda: r"C:\fake\powershell.exe")
    monkeypatch.setattr(chemdraw, "_run", _never)
    monkeypatch.setattr(chemdraw, "_stop_leftovers", _never)
    monkeypatch.setattr(chemdraw, "find_prog_id", lambda: None)


def stops() -> tuple[list[list[int]], callable]:
    """(the 'before' lists the cleanup was called with, the cleanup)."""
    cleaned: list[list[int]] = []
    return cleaned, cleaned.append


# --------------------------------------------------------------------------- parse_output

def test_parse_output_reads_the_before_list_and_the_result():
    text = "\r\n".join(["  before:123,456,abc,  ", "noise",
                        result_line({"ok": True, "name": "methane"}), ""])
    assert parse_output(text) == ([123, 456], {"ok": True, "name": "methane"})


def test_parse_output_without_result():
    assert parse_output("") == (None, None)          # the script never got to ChemDraw
    assert parse_output("before:\nsomething else\n") == ([], None)   # none was running
    assert parse_output("before:7\nsomething else\n") == ([7], None)


@pytest.mark.parametrize("line", [
    "result:not base64!",
    "result:" + base64.b64encode(b"\xff\xfe").decode("ascii"),     # not UTF-8
    "result:" + base64.b64encode(b"{broken").decode("ascii"),        # not JSON
    result_line([1, 2]),                                             # not an object
    result_line("text"),
])
def test_parse_output_ignores_unreadable_results(line):
    assert parse_output(line) == (None, None)


def test_parse_output_keeps_the_last_readable_result():
    text = "\n".join([result_line({"ok": False}), "result:###", result_line({"ok": True})])
    assert parse_output(text)[1] == {"ok": True}


# --------------------------------------------------------------------------- name_molfile

def test_success():
    runner = FakeRunner("before:4321\n" + result_line(
        {"ok": True, "name": "  (S)-1-phenylethan-1-amine \n", "inchi": " InChI=1S/x ",
         "atoms": 9}))
    stopped, stop = stops()
    answer = name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID, cleanup=stop, timeout=12)
    assert answer == ChemDrawAnswer(name="(S)-1-phenylethan-1-amine", inchi="InChI=1S/x", atoms=9)
    assert stopped == []          # nothing is stopped when the script finished
    (args, stdin_text, timeout), = runner.calls
    assert timeout == 12
    assert args[0] == r"C:\fake\powershell.exe"
    assert "-NonInteractive" in args and "-File" not in args
    # The script's text is on the command line, so no execution policy applies.
    command = decoded_command(args)
    assert command.startswith("& {\n") and command.endswith("\n} -ProgId '" + PROG_ID + "'")
    assert chemdraw._SCRIPT.read_text(encoding="utf-8") in command
    # The molfile goes in as one line of base64 of its UTF-8 text.
    assert stdin_text.endswith("\n") and stdin_text.count("\n") == 1
    assert base64.b64decode(stdin_text.strip()).decode("utf-8") == MOLFILE


def test_success_with_odd_fields_is_normalised():
    runner = FakeRunner(result_line({"ok": True, "name": None, "inchi": 5, "atoms": True}))
    answer = name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID)
    assert answer == ChemDrawAnswer(name="", inchi="", atoms=0)


def test_unicode_molfile_is_sent_as_utf8():
    text = MOLFILE.replace("test", "t\u00e9st \u03b2")
    runner = FakeRunner(result_line({"ok": True, "name": "x", "inchi": "y", "atoms": 1}))
    name_molfile(text, runner=runner, prog_id=PROG_ID)
    assert base64.b64decode(runner.calls[0][1]).decode("utf-8") == text


def test_failure_at_start():
    runner = FakeRunner(result_line({"ok": False, "stage": "start",
                                     "message": "Class not registered"}))
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID)
    assert info.value.code == "failed"
    assert info.value.message == "ChemDraw could not be started: Class not registered"


def test_failure_while_naming():
    runner = FakeRunner("before:9\n" + result_line({"ok": False, "stage": "name",
                                                 "message": "bad molfile"}))
    stopped, stop = stops()
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID, cleanup=stop)
    assert info.value.code == "failed"
    assert "could not read or name the structure: bad molfile" in info.value.message
    assert stopped == []


def test_failure_without_details():
    runner = FakeRunner(result_line({"ok": "yes"}))
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID)
    assert info.value.code == "failed" and "no details" in info.value.message


def test_no_result_line():
    runner = FakeRunner("before:77\nsomething went wrong\n")
    stopped, stop = stops()
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID, cleanup=stop)
    assert info.value.code == "failed" and "gave no answer" in info.value.message
    assert "Windows PowerShell" in info.value.message
    assert stopped == []


def test_timeout_cleans_up_with_the_before_list():
    runner = FakeRunner("before:111,222\n", timed_out=True)
    cleaned, cleanup = stops()
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID, cleanup=cleanup, timeout=3)
    assert info.value.code == "timeout"
    assert info.value.message == "ChemDraw did not answer within 3 seconds."
    assert cleaned == [[111, 222]]


def test_timeout_with_no_chemdraw_running_before_still_cleans_up():
    runner = FakeRunner("before:\n", timed_out=True)
    cleaned, cleanup = stops()
    with pytest.raises(ChemDrawError):
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID, cleanup=cleanup)
    assert cleaned == [[]]


def test_timeout_before_chemdraw_was_started_cleans_nothing():
    # No "before:" line: the script was stopped before it started ChemDraw.
    runner = FakeRunner("", timed_out=True)
    cleaned, cleanup = stops()
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID, cleanup=cleanup)
    assert info.value.code == "timeout" and cleaned == []


def test_timeout_wins_over_a_late_result():
    runner = FakeRunner("before:5\n" + result_line({"ok": True, "name": "x", "inchi": "y"}),
                        timed_out=True)
    cleaned, cleanup = stops()
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID, cleanup=cleanup)
    assert info.value.code == "timeout" and cleaned == [[5]]


def test_default_timeout_is_the_contract_value():
    from compound_registry import protocol
    assert chemdraw.DEFAULT_TIMEOUT_S == protocol.NAME_GENERATION_TIMEOUT_S < 30
    runner = FakeRunner(result_line({"ok": True, "name": "x", "inchi": "y", "atoms": 1}))
    name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID)
    assert runner.calls[0][2] == chemdraw.DEFAULT_TIMEOUT_S


def test_cleanup_script_is_shipped():
    assert chemdraw._CLEANUP.is_file() and chemdraw._SCRIPT.is_file()


def test_script_args_carry_the_script_text_and_quoted_parameters(tmp_path):
    script = tmp_path / "probe.ps1"
    script.write_text("param([string]$A = '', [string]$B = '')\nexit 0\n", encoding="utf-8")
    args = chemdraw.script_args("shell.exe", script, A="it's", B="1,2")
    assert args[:4] == ["shell.exe", "-NoProfile", "-NonInteractive", "-NoLogo"]
    assert args[4] == "-EncodedCommand" and len(args) == 6
    assert decoded_command(args) == ("& {\nparam([string]$A = '', [string]$B = '')\nexit 0\n\n}"
                                     " -A 'it''s' -B '1,2'")


TRICKY_VALUES = ["a\u2019b", "x\u2019; Set-Content -LiteralPath INJECTED.txt -Value hi; \u2018",
                 "\u2018\u201a\u201b'\u2019", "''", "\u2019", "it's $x $(1+1) `n \"q\" ; -Other zz"]


def test_script_args_double_every_quote_that_ends_a_powershell_string(tmp_path):
    script = tmp_path / "probe.ps1"
    script.write_text("exit 0\n", encoding="utf-8")
    args = chemdraw.script_args("shell.exe", script, A="a\u2019b\u2018c\u201ad\u201be'f")
    assert decoded_command(args).endswith(
        " -A 'a\u2019\u2019b\u2018\u2018c\u201a\u201ad\u201b\u201be''f'")


@pytest.mark.skipif(_REAL_POWERSHELL is None, reason="needs Windows PowerShell")
@pytest.mark.parametrize("value", TRICKY_VALUES)
def test_powershell_receives_a_parameter_value_unchanged(tmp_path, value):
    script = tmp_path / "probe.ps1"
    script.write_text("param([string]$A = '')\n[Console]::Out.Write([Convert]::ToBase64String("
                      "[Text.Encoding]::UTF8.GetBytes($A)))\n", encoding="utf-8")
    done = subprocess.run(chemdraw.script_args(_REAL_POWERSHELL, script, A=value),
                          stdin=subprocess.DEVNULL, capture_output=True, cwd=tmp_path, timeout=60)
    assert done.returncode == 0
    assert base64.b64decode(done.stdout).decode("utf-8") == value
    assert not (tmp_path / "INJECTED.txt").exists()


def test_both_scripts_fit_on_a_command_line():
    for script in (chemdraw._SCRIPT, chemdraw._CLEANUP):
        args = chemdraw.script_args("shell.exe", script, ProgId=PROG_ID)
        assert sum(len(a) + 3 for a in args) < 32000


def test_script_too_long_for_a_command_line(tmp_path):
    script = tmp_path / "long.ps1"
    script.write_text("# " + "x" * 30000 + "\n", encoding="utf-8")
    with pytest.raises(OSError):
        chemdraw.script_args("shell.exe", script)


def test_unavailable_without_the_script(monkeypatch, tmp_path):
    monkeypatch.setattr(chemdraw, "_SCRIPT", tmp_path)   # a folder: is_file() is false
    runner = FakeRunner()
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID)
    assert info.value.code == "unavailable" and runner.calls == []


def test_cleanup_runs_the_cleanup_script_with_the_before_list(monkeypatch):
    seen = []
    monkeypatch.setattr(chemdraw.subprocess, "run", lambda args, **kwargs: seen.append(args))
    _REAL_STOP_LEFTOVERS([12, 34])
    _REAL_STOP_LEFTOVERS([])
    assert [decoded_command(a).rsplit("\n}", 1)[1] for a in seen] == [" -Before '12,34'",
                                                                      " -Before 'none'"]
    assert chemdraw._CLEANUP.read_text(encoding="utf-8") in decoded_command(seen[0])


def test_runner_that_cannot_start():
    runner = FakeRunner(error=OSError("access denied"))
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID)
    assert info.value.code == "failed" and "access denied" in info.value.message


def test_unavailable_when_switched_off(monkeypatch):
    monkeypatch.setenv("COMPOUND_REGISTRY_NO_CHEMDRAW", "1")
    runner = FakeRunner(result_line({"ok": True, "name": "x"}))
    assert chemdraw.available() is False
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner)
    assert info.value.code == "unavailable" and "not found" in info.value.message
    # The switch also wins over an explicitly given automation class.
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID)
    assert info.value.code == "unavailable"
    assert runner.calls == []


def test_find_prog_id_respects_the_switch(monkeypatch):
    monkeypatch.undo()                      # the real find_prog_id again
    monkeypatch.setenv("COMPOUND_REGISTRY_NO_CHEMDRAW", "1")
    assert chemdraw.find_prog_id() is None and chemdraw.available() is False


@pytest.mark.parametrize("text, expected", [
    ("2-acetoxybenzoic acid", False), ("a\tb", True), ("a\nb", True), ("a\x9bb", True),
    ("a\u2028b", True), ("a\u2029b", True), ("\u03b2-alanine", False),
])
def test_has_control(text, expected):
    assert chemdraw.has_control(text) is expected


def test_unavailable_without_powershell(monkeypatch):
    monkeypatch.setattr(chemdraw, "_powershell", lambda: None)
    runner = FakeRunner()
    with pytest.raises(ChemDrawError) as info:
        name_molfile(MOLFILE, runner=runner, prog_id=PROG_ID)
    assert info.value.code == "unavailable" and runner.calls == []


@pytest.mark.parametrize("blank", ["", "   \n  ", None, 5])
def test_blank_molfile(blank):
    runner = FakeRunner()
    with pytest.raises(ChemDrawError) as info:
        name_molfile(blank, runner=runner, prog_id=PROG_ID)
    assert info.value.code == "failed" and runner.calls == []
