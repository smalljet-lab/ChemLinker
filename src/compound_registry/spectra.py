"""What is stored about a measured spectrum of a batch (see ``spectra`` in
:mod:`compound_registry.sqlite_repo`).

The MestReNova add-on sends, per spectrum of a linked document (1H, 13C, any
other nucleus, 2D), the shift report as MestReNova wrote it (HTML; the
multiplet report, or for a spectrum without multiplets that is not 1H the
peak list, see ``report_kind``), the acquisition parameters and the
paths of the saved ``.mnova`` and ``.pdf`` files. :func:`clean_spectrum`
turns one such object into ``(spectrum_key, data)``: only known fields, with
size limits, and the report also as plain text (``1H NMR (500 MHz, CDCl3)
δ 7.40 (t, J = 1.9 Hz, 1H), ...``).

A spectrum is an annotation of a batch, like a name: it never takes part in
an ID and may be recorded again (the newer data replaces the older).

A spectrum the chemist marked as crude (``"crude": true``, the "Crude" box of
the add-on) is recorded with its ``.mnova`` document only: no report, no
parameters and no PDF are stored, and the catalogue shows just the path.
"""

from __future__ import annotations

import html
import math
import re
from typing import Any

from .errors import RegistryError

__all__ = ["clean_spectrum", "report_text", "spectrum_key", "TEXT_FIELDS", "REPORT_KINDS",
           "CRUDE_DROPPED"]

#: Text fields of a spectrum and their length limits.
TEXT_FIELDS: dict[str, int] = {
    "title": 500, "comment": 2000, "sample": 200, "nucleus": 20, "solvent": 100,
    "experiment": 100, "pulse_sequence": 100, "report_kind": 20, "report": 20000, "report_html": 60000,
    "mnova_file": 1000, "pdf_file": 1000,
}
#: What a report was made from: the multiplets (shift, multiplicity, J,
#: integral) or, without multiplets, the picked peaks (shifts only).
REPORT_KINDS = ("multiplets", "peaks")
#: What is never stored for a crude spectrum (``"crude": true``): only where
#: its document lies is recorded, no report, no parameters, no PDF.
CRUDE_DROPPED = ("report", "report_html", "report_kind", "pdf_file", "parameters")
_MAX_PARAMETERS = 200
_MAX_PARAMETER_NAME = 100
_MAX_PARAMETER_VALUE = 2000
_MAX_KEY = 300


def _invalid(message: str) -> RegistryError:
    return RegistryError(message, code="invalid_argument")


def report_text(report_html: str) -> str:
    """A multiplet report written as HTML, as one line of plain text."""
    text = re.sub(r"(?is)<(head|style|script)\b.*?</\1>", "", report_html or "")
    text = re.sub(r"(?s)<[^>]*>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _text(raw: dict[str, Any], field: str) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise _invalid(f"{field} of a spectrum must be text, got {type(value).__name__}.")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    if len(text) > TEXT_FIELDS[field]:
        raise _invalid(f"{field} of a spectrum is too long ({len(text)} characters, limit "
                       f"{TEXT_FIELDS[field]}).")
    return text


def spectrum_key(title: str | None, nucleus: str | None, dimensions: int | None) -> str:
    """Names one measurement: the dataset name the spectrometer gave
    (``user.011.1.fid``) with nucleus and dimension, so that recording the
    same spectrum again replaces the earlier record."""
    first_line = (title or "").split("\n", 1)[0].strip()
    parts = [first_line or "untitled", nucleus or "?", f"{dimensions or 1}D"]
    return " | ".join(parts)[:_MAX_KEY]


def clean_spectrum(raw: Any) -> tuple[str, dict[str, Any]]:
    """``(spectrum_key, data)`` for one spectrum sent by the add-on.

    Unknown fields are dropped. ``report`` is derived from ``report_html``
    when only the HTML is given. With ``"crude": true`` the fields of
    :data:`CRUDE_DROPPED` are left out. ``invalid_argument`` for anything that is
    not an object, a field of the wrong type or over its limit.
    """
    if not isinstance(raw, dict):
        raise _invalid("A spectrum must be an object.")
    data: dict[str, Any] = {}
    for field in TEXT_FIELDS:
        text = _text(raw, field)
        if text is not None:
            data[field] = text
    if "report" not in data and "report_html" in data:
        text = report_text(data["report_html"])
        if text:
            data["report"] = text[:TEXT_FIELDS["report"]]
    if "report_kind" in data and (data["report_kind"] not in REPORT_KINDS
                                  or "report" not in data):
        if data["report_kind"] not in REPORT_KINDS:
            raise _invalid(f"report_kind must be one of {', '.join(REPORT_KINDS)}, got "
                           f"{data['report_kind']!r}.")
        del data["report_kind"]

    frequency = raw.get("frequency_mhz")
    if frequency is not None:
        if isinstance(frequency, bool) or not isinstance(frequency, (int, float)) \
                or not math.isfinite(frequency) or frequency <= 0:
            raise _invalid(f"frequency_mhz must be a positive number, got {frequency!r}.")
        data["frequency_mhz"] = round(float(frequency), 6)
    dimensions = raw.get("dimensions")
    if dimensions is not None:
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) \
                or not 1 <= dimensions <= 4:
            raise _invalid(f"dimensions must be 1 to 4, got {dimensions!r}.")
        data["dimensions"] = dimensions

    parameters = raw.get("parameters")
    if parameters is not None:
        if not isinstance(parameters, dict):
            raise _invalid("parameters of a spectrum must be an object.")
        if len(parameters) > _MAX_PARAMETERS:
            raise _invalid(f"Too many parameters ({len(parameters)}, limit {_MAX_PARAMETERS}).")
        cleaned: dict[str, str] = {}
        for name, value in parameters.items():
            if not isinstance(name, str) or not name.strip() or value is None:
                continue
            text = str(value).strip()
            if text:
                cleaned[name.strip()[:_MAX_PARAMETER_NAME]] = text[:_MAX_PARAMETER_VALUE]
        if cleaned:
            data["parameters"] = cleaned

    crude = raw.get("crude")
    if crude is not None and not isinstance(crude, bool):
        raise _invalid(f"crude of a spectrum must be true or false, got {crude!r}.")
    if crude:
        data["crude"] = True
        for field in CRUDE_DROPPED:
            data.pop(field, None)

    key = raw.get("key")
    if key is not None and (not isinstance(key, str) or len(key) > _MAX_KEY):
        raise _invalid("The key of a spectrum must be text of at most "
                       f"{_MAX_KEY} characters.")
    key = (key or "").strip() or spectrum_key(
        data.get("title"), data.get("nucleus"), data.get("dimensions"))
    return key, data
