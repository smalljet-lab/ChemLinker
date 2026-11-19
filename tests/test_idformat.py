"""Tests for compound_registry.idformat."""

from __future__ import annotations

import re

import pytest

from compound_registry.errors import ConfigError, IdOverflowError, NotFoundError
from compound_registry.idformat import IdFormat


def test_default_format_and_limits():
    fmt = IdFormat()
    assert fmt.template == IdFormat.DEFAULT == "{core:06d}_{stereo:02d}_{batch:03d}"
    assert fmt.format(42, 2, 3) == "000042_02_003"
    assert fmt.format_core_stereo(42, 2) == "000042_02"
    assert (fmt.max_core, fmt.max_stereo, fmt.max_batch) == (999999, 99, 999)
    assert fmt.pattern == r"^([0-9]{6})_([0-9]{2})_([0-9]{3})$"
    assert isinstance(fmt.regex, re.Pattern)
    assert fmt.regex.match("000042_02_003")


def test_parse_roundtrip():
    fmt = IdFormat()
    assert fmt.parse("000042_02_003") == (42, 2, 3)
    assert fmt.parse(fmt.format(999999, 99, 999)) == (999999, 99, 999)


@pytest.mark.parametrize(
    "text",
    ["", "42_2_3", "000042_02_0031", "000042-02-003", "x000042_02_003", "000042_02_003\n",
     "000042_02_003 ", "٠٠٠٠٤٢_٠٢_٠٠٣", "000000_01_001", "000001_00_001", "000001_01_000"],
)
def test_parse_rejects_non_ids(text):
    with pytest.raises(NotFoundError) as info:
        IdFormat().parse(text)
    assert info.value.code == "not_found"


def test_parse_rejects_non_string():
    with pytest.raises(NotFoundError):
        IdFormat().parse(None)  # type: ignore[arg-type]


def test_prefix_and_suffix_literals():
    fmt = IdFormat("UB-{core:05d}.{stereo:01d}/{batch:02d}x")
    assert fmt.format(7, 3, 12) == "UB-00007.3/12x"
    assert fmt.format_core_stereo(7, 3) == "UB-00007.3"
    assert fmt.parse("UB-00007.3/12x") == (7, 3, 12)
    # '.' must be literal, not "any character"
    with pytest.raises(NotFoundError):
        fmt.parse("UB-00007a3/12x")
    assert (fmt.max_core, fmt.max_stereo, fmt.max_batch) == (99999, 9, 99)
    assert fmt.pattern.startswith("^UB-")  # '-' left unescaped (JS-compatible)


def test_adjacent_fields_without_separator():
    fmt = IdFormat("{core:03d}{stereo:01d}{batch:02d}")
    assert fmt.format(1, 2, 3) == "001203"
    assert fmt.parse("001203") == (1, 2, 3)


@pytest.mark.parametrize("args", [(0, 1, 1), (1, 0, 1), (1, 1, 0), (-1, 1, 1),
                                  (1_000_000, 1, 1), (1, 100, 1), (1, 1, 1000)])
def test_format_out_of_range(args):
    with pytest.raises(IdOverflowError) as info:
        IdFormat().format(*args)
    assert info.value.code == "id_overflow"


def test_tiny_format_overflow():
    fmt = IdFormat("{core:01d}_{stereo:01d}_{batch:01d}")
    assert fmt.format(9, 9, 9) == "9_9_9"
    with pytest.raises(IdOverflowError):
        fmt.format(10, 1, 1)
    with pytest.raises(IdOverflowError):
        fmt.format_core_stereo(1, 10)


def test_format_rejects_non_int():
    with pytest.raises(TypeError):
        IdFormat().format("1", 1, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        IdFormat().format(True, 1, 1)


@pytest.mark.parametrize(
    "template",
    [
        "",
        "{core:06d}_{stereo:02d}",                      # batch missing
        "{core:06d}_{stereo:02d}_{batch:03d}_{core:02d}",  # duplicate
        "{stereo:02d}_{core:06d}_{batch:03d}",          # wrong order
        "{core:6d}_{stereo:02d}_{batch:03d}",           # not zero padded
        "{core}_{stereo:02d}_{batch:03d}",              # no width
        "{core:00d}_{stereo:02d}_{batch:03d}",          # zero width
        "{core:06x}_{stereo:02d}_{batch:03d}",          # not decimal
        "{core:06d}_{stereo:02d}_{batch:03d}_{extra:01d}",
        "{core!r}_{stereo:02d}_{batch:03d}",
        "{}_{}_{}",
        "{core:06d}1{stereo:02d}_{batch:03d}",          # digit in literal
        "A1-{core:06d}_{stereo:02d}_{batch:03d}",       # digit in prefix
        "{core:06d}_{stereo:02d}_{batch:03d}{{",        # brace literal
        "{core:06d} {stereo:02d}_{batch:03d}",          # whitespace
        "{core:06d}_{stereo:02d}_{batch:03d",           # unbalanced
        "{core:019d}_{stereo:02d}_{batch:03d}",         # too wide
    ],
)
def test_invalid_templates(template):
    with pytest.raises(ConfigError) as info:
        IdFormat(template)
    assert info.value.code == "config_invalid"


def test_invalid_template_type():
    with pytest.raises(ConfigError):
        IdFormat(None)  # type: ignore[arg-type]


def test_equality():
    assert IdFormat() == IdFormat(IdFormat.DEFAULT)
    assert IdFormat() != IdFormat("UB-{core:06d}_{stereo:02d}_{batch:03d}")
    assert hash(IdFormat()) == hash(IdFormat())


@pytest.mark.parametrize("template", [
    IdFormat.DEFAULT,
    "EX{core:04d}_{stereo:02d}_{batch:03d}",
    "UB-{core:06d}.{stereo:02d}.{batch:03d}",
    "x{core:04d}_{stereo:02d}-{batch:03d}",
])
def test_formats_whose_ids_can_be_file_names(template):
    IdFormat(template).require_file_safe()


@pytest.mark.parametrize("template", [
    "{core:04d}/{stereo:02d}/{batch:03d}",
    "{core:04d}:{stereo:02d}:{batch:03d}",
    "..\\{core:04d}_{stereo:02d}_{batch:03d}",
    "<b>{core:04d}_{stereo:02d}_{batch:03d}",
    "é{core:04d}_{stereo:02d}_{batch:03d}",       # a letter, but not ASCII
    ".x{core:04d}_{stereo:02d}_{batch:03d}",
    "{core:04d}_{stereo:02d}_{batch:03d}.",
    "COM{core:01d}_{stereo:02d}_{batch:03d}",            # the CORE alone is the device COM1
    "LPT{core:01d}_{stereo:02d}_{batch:03d}",
    "x{core:04d}*{stereo:02d}?{batch:03d}",
    "x%{core:04d}_{stereo:02d}_{batch:03d}",
    'x"{core:04d}_{stereo:02d}_{batch:03d}',
    "x" * 100 + "{core:04d}_{stereo:02d}_{batch:03d}",
])
def test_formats_refused_for_a_new_registry(template):
    fmt = IdFormat(template)        # a format, so the rule below is what refuses it
    with pytest.raises(ConfigError) as info:
        fmt.require_file_safe()
    assert info.value.code == "config_invalid"
    assert "folder and file names" in info.value.message


@pytest.mark.parametrize("template", [
    "x {core:04d}_{stereo:02d}_{batch:03d}",
    "{core:04d}_{stereo:02d}_{batch:03d} ",
    "x\n{core:04d}_{stereo:02d}_{batch:03d}",
    "{core:04d}_{stereo:02d}.",
])
def test_templates_that_are_not_formats_at_all(template):
    with pytest.raises(ConfigError) as info:
        IdFormat(template)
    assert info.value.code == "config_invalid"


def test_a_format_that_is_not_file_safe_is_still_a_format():
    # A registry created with such a format must keep opening.
    fmt = IdFormat("{core:04d}:{stereo:02d}:{batch:03d}")
    assert fmt.format(1, 2, 3) == "0001:02:003" and fmt.parse("0001:02:003") == (1, 2, 3)
