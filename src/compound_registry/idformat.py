"""The textual form of a registry ID: ``CORE_STEREO_BATCH``.

The template is a ``str.format`` pattern with exactly three fields, ``core``,
``stereo`` and ``batch``, in that order, each zero-padded to a fixed width
(``0<N>d``). Because every field has a fixed width and the literal text around
the fields contains no digits, every ID can be matched by one anchored regular
expression and parsed back into its three numbers without ambiguity.

The default ``{core:06d}_{stereo:02d}_{batch:03d}`` produces ``000042_02_003``.

IDs are also used as folder and file names (one folder per ``CORE_STEREO``,
files named after the full ID). A format chosen for a NEW registry must
therefore pass :meth:`IdFormat.require_file_safe`. :class:`IdFormat` itself
accepts more, so that a registry created with another format still opens.
"""

from __future__ import annotations

import re
import string

from .errors import ConfigError, IdOverflowError, NotFoundError
from .files import folder_name

_FIELDS = ("core", "stereo", "batch")
_SPEC_RE = re.compile(r"0([1-9][0-9]*)d")
# 18 digits still fit a signed 64-bit SQLite INTEGER.
_MAX_WIDTH = 18
# Characters escaped in the regex source. ``-`` is deliberately not escaped so
# the pattern is also valid in JavaScript ``u``-mode regular expressions.
_REGEX_SPECIALS = frozenset("\\.^$*+?()[]{}|/")


def _escape(text: str) -> str:
    return "".join("\\" + ch if ch in _REGEX_SPECIALS else ch for ch in text)


def _check_literal(template: str, literal: str) -> None:
    for ch in literal:
        if ch in "{}":
            raise ConfigError(
                f"ID format {template!r}: literal text may not contain braces"
            )
        if ch.isdigit():
            raise ConfigError(
                f"ID format {template!r}: literal text may not contain digits "
                f"({literal!r}); IDs would not be parseable unambiguously"
            )
        if ch.isspace() or not ch.isprintable():
            raise ConfigError(
                f"ID format {template!r}: literal text may not contain spaces "
                f"or control characters ({literal!r})"
            )


class IdFormat:
    """A validated ID template.

    Attributes
    ----------
    template    The template string as given.
    pattern     Anchored regex source, e.g. ``^([0-9]{6})_([0-9]{2})_([0-9]{3})$``.
                Uses ``[0-9]`` (not ``\\d``) so only ASCII digits match, and is
                portable to JavaScript.
    regex       ``pattern`` compiled.
    max_core, max_stereo, max_batch
                Largest number that fits each field (``10**width - 1``).
    """

    DEFAULT = "{core:06d}_{stereo:02d}_{batch:03d}"

    def __init__(self, template: str = DEFAULT):
        if not isinstance(template, str) or not template:
            raise ConfigError("ID format must be a non-empty string")
        try:
            parts = list(string.Formatter().parse(template))
        except ValueError as exc:
            raise ConfigError(f"ID format {template!r} is not a valid format string: {exc}") from None

        literals: list[str] = [""]
        names: list[str] = []
        widths: list[int] = []
        for literal, name, spec, conversion in parts:
            literals[-1] += literal
            if name is None:
                continue
            if conversion is not None:
                raise ConfigError(f"ID format {template!r}: conversions like '!r' are not allowed")
            if name not in _FIELDS:
                raise ConfigError(
                    f"ID format {template!r}: unknown field {{{name}}}; "
                    "only {core}, {stereo} and {batch} are allowed"
                )
            m = _SPEC_RE.fullmatch(spec or "")
            if m is None:
                raise ConfigError(
                    f"ID format {template!r}: field {{{name}}} needs a zero-padded "
                    f"fixed width such as '{{{name}:03d}}', got {spec!r}"
                )
            width = int(m.group(1))
            if width > _MAX_WIDTH:
                raise ConfigError(
                    f"ID format {template!r}: field {{{name}}} is wider than {_MAX_WIDTH} digits"
                )
            names.append(name)
            widths.append(width)
            literals.append("")

        if tuple(names) != _FIELDS:
            raise ConfigError(
                f"ID format {template!r} must contain {{core}}, {{stereo}} and {{batch}} "
                f"exactly once each, in that order (found {names})"
            )
        for literal in literals:
            _check_literal(template, literal)

        self.template: str = template
        self._literals: tuple[str, ...] = tuple(literals)  # 4 items: before, between x2, after
        self._widths: tuple[int, ...] = tuple(widths)
        self.max_core: int = 10 ** widths[0] - 1
        self.max_stereo: int = 10 ** widths[1] - 1
        self.max_batch: int = 10 ** widths[2] - 1
        lit = [_escape(x) for x in literals]
        self.pattern: str = (
            f"^{lit[0]}([0-9]{{{widths[0]}}}){lit[1]}([0-9]{{{widths[1]}}})"
            f"{lit[2]}([0-9]{{{widths[2]}}}){lit[3]}$"
        )
        self.regex: re.Pattern[str] = re.compile(self.pattern)
        # Shorter forms used for searching: CORE_STEREO and CORE alone. A lone
        # CORE number may be typed without its leading zeros ("42").
        self._core_stereo_regex: re.Pattern[str] = re.compile(
            f"^{lit[0]}([0-9]{{{widths[0]}}}){lit[1]}([0-9]{{{widths[1]}}})$")
        # With a prefix ("EX{core:04d}_..."), the bare number ("42") is accepted too.
        self._core_regex: re.Pattern[str] = re.compile(
            f"^(?:{lit[0]})?([0-9]{{1,{widths[0]}}})$")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _check(label: str, value: int, maximum: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{label} number must be an int, got {value!r}")
        if value < 1 or value > maximum:
            raise IdOverflowError(
                f"{label} number {value} does not fit the ID format "
                f"(allowed 1..{maximum})"
            )

    # ------------------------------------------------------------------ public
    def format(self, core: int, stereo: int, batch: int) -> str:
        """Full ID, e.g. ``000042_02_003``. Raises ``IdOverflowError`` if a
        number is < 1 or does not fit its width."""
        self._check("CORE", core, self.max_core)
        self._check("STEREO", stereo, self.max_stereo)
        self._check("BATCH", batch, self.max_batch)
        return self.template.format(core=core, stereo=stereo, batch=batch)

    def format_core_stereo(self, core: int, stereo: int) -> str:
        """The ID cut right after the stereo field, e.g. ``000042_02``."""
        self._check("CORE", core, self.max_core)
        self._check("STEREO", stereo, self.max_stereo)
        w = self._widths
        lit = self._literals
        return f"{lit[0]}{core:0{w[0]}d}{lit[1]}{stereo:0{w[1]}d}"

    def format_core(self, core: int) -> str:
        """The ID cut right after the core field, e.g. ``000042``. For display
        and searching only; nothing is ever issued at this level."""
        self._check("CORE", core, self.max_core)
        return f"{self._literals[0]}{core:0{self._widths[0]}d}"

    def parse(self, full_id: str) -> tuple[int, int, int]:
        """``(core, stereo, batch)`` of a full ID. Text that is not an ID of
        this format (or has a zero number) raises ``NotFoundError``."""
        m = self.regex.fullmatch(full_id) if isinstance(full_id, str) else None
        if m is None:
            raise NotFoundError(f"{full_id!r} is not an ID of the form {self.template!r}")
        numbers = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if min(numbers) < 1:
            raise NotFoundError(f"{full_id!r} contains a zero number and cannot exist")
        return numbers

    def parse_partial(self, text: str) -> tuple[int, int | None, int | None]:
        """``(core, stereo, batch)`` of a full ID, of a ``CORE_STEREO``
        (batch is None) or of a ``CORE`` alone (stereo and batch are None).
        A lone CORE may be given without leading zeros and without the
        format's leading text (``EX0042``, ``EX42`` or ``42``). For searching only:
        nothing is ever issued from a partial ID. Raises ``NotFoundError``
        for any other text or a zero number."""
        if isinstance(text, str):
            m = self.regex.fullmatch(text)
            if m is not None:
                return self.parse(text)
            m = self._core_stereo_regex.fullmatch(text)
            if m is not None:
                core, stereo = int(m.group(1)), int(m.group(2))
                if min(core, stereo) >= 1:
                    return core, stereo, None
            m = self._core_regex.fullmatch(text)
            if m is not None and int(m.group(1)) >= 1:
                return int(m.group(1)), None, None
        example = self.template.format(core=42, stereo=2, batch=3)
        short = self.format_core_stereo(42, 2)
        raise NotFoundError(
            f"{text!r} is not an ID: give a full ID such as {example}, a compound such as "
            f"{short}, or a CORE number such as {short[:len(short) - self._widths[1] - len(self._literals[1])]}.")

    def require_file_safe(self) -> None:
        """Raise ``ConfigError`` unless every ID of this format can be used
        as a folder name and in a file name on every system (the rule of
        :func:`compound_registry.files.folder_name`): the text around the
        numbers may only hold the letters A-Z and a-z, ``_``, ``-`` and
        ``.``; an ID does not start or end with a dot and is not a Windows
        device name. Checked when a registry is created, because the format
        can never be changed afterwards."""
        samples = (self.format_core(1), self.format_core_stereo(1, 1), self.format(1, 1, 1),
                   self.format(self.max_core, self.max_stereo, self.max_batch))
        if any(folder_name(sample) is None for sample in samples):
            raise ConfigError(
                f"ID format {self.template!r} cannot be used for a new registry: IDs are also "
                f"folder and file names, so the text around the numbers may only contain the "
                f"letters A-Z and a-z, '_', '-' and '.', and an ID such as "
                f"{self.format(1, 1, 1)!r} may not start or end with a dot, be a Windows "
                f"device name (CON, NUL, COM1 ...) or be longer than 100 characters."
            )

    # ------------------------------------------------------------------ dunder
    def __eq__(self, other: object) -> bool:
        return isinstance(other, IdFormat) and other.template == self.template

    def __hash__(self) -> int:
        return hash(self.template)

    def __repr__(self) -> str:
        return f"IdFormat({self.template!r})"
