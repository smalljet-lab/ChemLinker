"""Export of registry batches to CSV (for Excel) and SDF (for chemistry tools).

Both writers stream into a temporary file in the destination folder and
``os.replace`` it onto the destination only when everything was written, so a
failed export never leaves a half-written file (an existing file at the
destination stays untouched).

They raise ``OSError`` for file-system problems; the service layer turns that
into a ``RegistryError``.

Besides the batch fields (:data:`COLUMNS`), both files carry the ``name``
stored for the batch's stereo variant (:data:`NAME_COLUMN`, last), taken from
the ``names`` mapping ``{core_stereo_id: text}`` the caller passes; it is
empty (CSV) or left out (SDF) when there is none. After it come the stored
yields of the batch (:data:`YIELD_COLUMNS`, in percent), taken from the
``yields`` mapping ``{full_id: YieldRecord}``, empty / left out the same way.
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Mapping, TextIO

from .models import BatchRecord, YieldRecord

__all__ = ["COLUMNS", "NAME_COLUMN", "YIELD_COLUMNS", "EXPORT_COLUMNS", "export_csv",
           "export_sdf", "sdf_value", "sdf_molblock"]

#: Batch fields written to the CSV file and as data fields of the SDF file, in this order.
COLUMNS: tuple[str, ...] = (
    "full_id", "core_no", "stereo_no", "batch_no", "core_stereo_id", "stereo_mode",
    "inchikey", "smiles", "experiment_ref", "experiment_code", "experiment_url",
    "product_index", "source", "created_at", "retired_at", "retired_reason",
    "rdkit_version", "inchi_version",
)

#: The stored name of the batch's stereo variant (not a batch field).
NAME_COLUMN = "name"

#: The stored yields of the batch (not batch fields): column -> YieldRecord field.
YIELD_COLUMNS: dict[str, str] = {"yield_isolated": "isolated", "yield_analytical": "analytical"}

#: Every column of the CSV file / data field of the SDF file, in this order.
EXPORT_COLUMNS: tuple[str, ...] = COLUMNS + (NAME_COLUMN,) + tuple(YIELD_COLUMNS)


def _value(batch: BatchRecord, column: str, names: Mapping[str, str],
           yields: Mapping[str, YieldRecord]) -> object:
    if column == NAME_COLUMN:
        return names.get(batch.core_stereo_id)
    if column in YIELD_COLUMNS:
        record = yields.get(batch.full_id)
        return getattr(record, YIELD_COLUMNS[column]) if record is not None else None
    return getattr(batch, column)


def _cell(batch: BatchRecord, column: str, names: Mapping[str, str],
          yields: Mapping[str, YieldRecord]) -> str:
    value = _value(batch, column, names, yields)
    return "" if value is None else str(value)


def _atomic_write(destination: Path, newline: str | None, encoding: str,
                  write: Callable[[TextIO], int]) -> int:
    """Run ``write(fh)`` on a temp file next to ``destination``, then replace it."""
    destination = Path(os.path.abspath(Path(destination).expanduser()))
    folder = destination.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".partial", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as fh:
            count = write(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, destination)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise
    return count


# --------------------------------------------------------------------------- CSV

def export_csv(batches: Iterable[BatchRecord], destination: Path,
               names: Mapping[str, str] | None = None,
               yields: Mapping[str, YieldRecord] | None = None) -> int:
    """Write one row per batch to ``destination`` (UTF-8 with BOM, CRLF rows,
    header :data:`EXPORT_COLUMNS` first). Values are written verbatim.
    Returns the number of rows."""
    names = names or {}
    yields = yields or {}

    def write(fh: TextIO) -> int:
        writer = csv.writer(fh, dialect="excel")
        writer.writerow(EXPORT_COLUMNS)
        n = 0
        for batch in batches:
            writer.writerow([_cell(batch, c, names, yields) for c in EXPORT_COLUMNS])
            n += 1
        return n

    return _atomic_write(Path(destination), "", "utf-8-sig", write)


# --------------------------------------------------------------------------- SDF

def sdf_value(value: object) -> str:
    """One-line SDF data value: newlines become spaces, and a value that would
    start a line with ``>`` or ``$$$$`` gets a leading space so it can never be
    read as a field header or a record separator."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    if text.startswith((">", "$$$$")):
        text = " " + text
    return text


def sdf_molblock(molfile: str) -> str:
    """The stored molfile for an SDF record.

    Line endings become ``\\n``; the text is cut right after its first
    ``M  END`` line (anything after it would be read as SDF data), or
    ``M  END`` is appended if it is missing. A header line that starts with
    ``$$$$`` (which would end the record early) is blanked. Nothing else is
    changed.
    """
    text = (molfile or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if line.rstrip() == "M  END":
            out.append(line)
            break
        out.append("" if line.startswith("$$$$") else line)
    else:
        while out and not out[-1].strip():
            out.pop()
        out.append("M  END")
    return "\n".join(out) + "\n"


def export_sdf(batches: Iterable[BatchRecord], destination: Path,
               names: Mapping[str, str] | None = None,
               yields: Mapping[str, YieldRecord] | None = None) -> int:
    """Write one SDF record per batch: the stored molfile, one ``> <column>``
    data field per non-empty column of :data:`EXPORT_COLUMNS`, then ``$$$$``.
    UTF-8, ``\\n`` line endings. Returns the number of records."""
    names = names or {}
    yields = yields or {}

    def write(fh: TextIO) -> int:
        n = 0
        for batch in batches:
            fh.write(sdf_molblock(batch.molfile))
            for column in EXPORT_COLUMNS:
                value = sdf_value(_value(batch, column, names, yields))
                if not value.strip():
                    continue
                fh.write(f"> <{column}>\n{value}\n\n")
            fh.write("$$$$\n")
            n += 1
        return n

    return _atomic_write(Path(destination), "\n", "utf-8", write)
