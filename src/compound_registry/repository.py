"""Storage interface.

The rest of the program talks to the registry only through this protocol, so a
different backend (for example Google Sheets/Drive) can be added later without
touching chemistry, CLI, host or extension.

Rules every backend must honour
-------------------------------
* IDs are permanent. Nothing is ever deleted and no number is ever reused.
* ``register`` is atomic and idempotent (see its docstring).
* CORE numbers start at 1; STEREO numbers start at 1 within a core; BATCH
  numbers start at 1 within a core+stereo.
* A number that would not fit the fixed width of the ID format raises
  ``IdOverflowError`` and allocates nothing.

Optional features
-----------------
The service looks these up with ``getattr`` and works (with less) without
them; :class:`~compound_registry.sqlite_repo.SqliteRepository` has all of them.

* ``get_meta`` / ``set_meta``, ``record_event``, ``stats``,
  ``integrity_check``, ``registration_details``, ``replay``: chemistry
  version guard, journal catch-up, recovery and ``doctor``.
* Listings for the compound list (``RegistryService.compounds``), all
  read-only: ``list_cores() -> list[CoreRecord]`` (by CORE number);
  ``list_stereos(core_nos=None, *, with_molfile=True) -> list[StereoRecord]``
  (by CORE then STEREO number, ``batch_count`` filled; ``molfile`` is ``""``
  without ``with_molfile``); ``batch_summaries() -> list[dict]`` (every batch
  without its structure, oldest first); ``stereo_inchis(core_stereo_ids=None)
  -> dict[str, str]`` (the InChI stored with each variant's first batch).
  Without them ``compounds`` is refused with ``not_supported``.
* Names (one free-text name per CORE_STEREO, an annotation that is never part
  of a key): ``get_names() -> dict[str, NameRecord]``,
  ``get_name(core_stereo_id) -> NameRecord | None`` and
  ``set_name(core_stereo_id, text_or_None, source="user") -> (NameRecord |
  None, changed)`` (``source`` one of ``protocol.NAME_SOURCES``, stored with
  the text; ``changed`` when the text or the source differs); both raise
  ``NotFoundError`` for an unknown ``core_stereo_id``, and every change must
  be recorded in the backend's event log. Without them names are reported as
  null and ``set_name`` / ``generate_name`` are refused with ``not_supported``.
* Yields (the isolated and analytical yield in percent of one batch, an
  annotation mirrored from the ELN): ``get_yields(full_ids=None) -> dict[str,
  YieldRecord]`` (every stored yield, or those of ``full_ids``, in one
  query), ``set_yield(full_id, isolated, analytical) -> (YieldRecord | None,
  changed)`` (both None removes the stored yield; nothing is written when
  the values are already stored; ``NotFoundError`` for an unknown
  ``full_id``; every change recorded in the event log) and
  ``batch_yields() -> list[dict]`` (every batch without its structure, with
  its experiment, the name of its stereo variant and its yield). Without
  them yields are reported as null and ``set_yield`` / ``yields`` are
  refused with ``not_supported``.
* Reactions (what an experiment page shows about its reaction, an
  annotation mirrored from the ELN, one per experiment; see protocol.py,
  "Reactions"): ``get_reaction(experiment_ref) -> dict | None``
  (``{"experiment_ref", "experiment_code", "experiment_url", "data",
  "updated_at", "digest"}``, ``data`` the stored JSON object),
  ``set_reaction(experiment_ref, experiment_code, experiment_url, data) ->
  (dict, changed)`` (replaces the stored reaction as a whole; nothing is
  written when it is already stored; ``experiment_code`` / ``experiment_url``
  None keep the stored ones; every change recorded in the event log as
  ``reaction_set`` with the experiment and a digest, not the data) and
  ``list_reactions(with_data=False) -> list[dict]``. Without them
  ``set_reaction``, ``reaction`` and ``route`` are refused with
  ``not_supported``.
* Starting-material choices (one per stored reaction, see protocol.py,
  "Synthesis routes"): ``get_limiting(experiment_ref) -> (row, name) |
  None`` (None: automatic or no reaction stored) and
  ``set_limiting(experiment_ref, row_or_None, name) -> changed`` (``row``
  None clears the choice; nothing is written when it is already stored;
  ``NotFoundError`` when no reaction is stored; never changed by
  ``set_reaction``; every change recorded in the event log as
  ``limiting_set`` with ``experiment_ref``, ``code``, ``row``, ``name``).
  Without them ``set_limiting`` is refused with ``not_supported`` and every
  route uses the automatic choice.
* ``get_stereo(core_stereo_id) -> StereoRecord | None`` (with its molfile):
  needed by ``RegistryService.generate_name``; without it that is refused
  with ``not_supported``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

from .models import BatchRecord, ExperimentRef, LookupResult, Normalized, RegistrationResult


@runtime_checkable
class RegistryRepository(Protocol):
    def lookup(
        self,
        norm: Normalized,
        experiment_ref: str | None = None,
        product_index: int | None = None,
    ) -> LookupResult:
        """Read-only. Report what is registered for this structure.

        ``existing_batch`` and ``slot_conflict`` are only filled when
        ``experiment_ref`` (and for the conflict, ``product_index``) is given.
        """
        ...

    def register(
        self,
        norm: Normalized,
        experiment: ExperimentRef,
        product_index: int,
        source: str = "cli",
    ) -> RegistrationResult:
        """Issue (or return) the batch ID for this compound in this experiment.

        One atomic transaction, in this order:

        1. If an ACTIVE batch of this exact compound (same core_key, stereo_key
           and stereo_mode) already exists for ``experiment.ref`` -> return it
           with ``created=False``. The product index does not matter here: the
           same compound in the same experiment is always the same batch.
        2. If an ACTIVE batch of a DIFFERENT compound occupies
           (``experiment.ref``, ``product_index``) -> raise ``SlotConflictError``
           and allocate nothing.
        3. Otherwise create the core and/or stereo variant if they are new,
           allocate the next batch number and store the batch.

        Retired batches are ignored in steps 1 and 2, so re-registering after a
        retirement issues a NEW batch number.
        """
        ...

    def retire(self, full_id: str, reason: str) -> BatchRecord:
        """Mark a batch as retired with a reason. The row and its number stay.
        Raises ``NotFoundError`` for an unknown ID. Retiring twice is an error."""
        ...

    def get_batch(self, full_id: str) -> BatchRecord | None:
        ...

    def find_batches(self, experiment_ref: str) -> tuple[BatchRecord, ...]:
        """All batches (retired included) of one experiment, by product index."""
        ...

    def iter_batches(self, include_retired: bool = True) -> Iterator[BatchRecord]:
        """Every batch ordered by (core_no, stereo_no, batch_no). For export."""
        ...

    def backup_to(self, destination: Path) -> None:
        """Write a consistent copy of the whole registry to ``destination``."""
        ...

    def close(self) -> None:
        ...
