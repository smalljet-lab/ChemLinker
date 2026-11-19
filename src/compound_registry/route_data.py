"""The registry as the route builder sees it.

:class:`RegistryRouteData` implements
:class:`~compound_registry.route_model.RouteData` over what the service reads
from the repository once per ``route`` call: the batches, their stored yields
and the stored reactions. Rules:

* Only ACTIVE batches are given to the builder; retired ones do not exist
  for it (a retired batch was issued by mistake).
* ``reaction`` returns a fresh copy of the stored JSON object every time, so
  the builder may change what it gets without changing what others see.
* ``identify`` never raises: a structure that cannot be read, or any other
  problem, counts as "not registered" (None). Answers are remembered per
  molfile text for the lifetime of the object.

Nothing here writes anything.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import BatchRecord, YieldRecord
from .route_model import BatchRef

__all__ = ["RegistryRouteData", "batch_ref"]


def batch_ref(batch: BatchRecord, record: YieldRecord | None) -> BatchRef:
    """The :class:`BatchRef` of a batch with its stored yield (or none)."""
    return BatchRef(
        full_id=batch.full_id,
        core_stereo_id=batch.core_stereo_id,
        experiment_ref=batch.experiment_ref,
        experiment_code=batch.experiment_code,
        experiment_url=batch.experiment_url,
        product_index=batch.product_index,
        isolated=record.isolated if record is not None else None,
        analytical=record.analytical if record is not None else None,
    )


class RegistryRouteData:
    """:class:`~compound_registry.route_model.RouteData` over a snapshot of
    the registry.

    ``batches``: every batch (retired ones are left out here);
    ``yields``: the stored yields by full ID;
    ``get_reaction``: the stored reaction object of an experiment (internal
    key), or None;
    ``identify``: the ``CORE_STEREO`` of a molfile, or None (may raise; any
    exception counts as None);
    ``get_limiting``: the starting material chosen for an experiment,
    ``(row, name)`` or None (optional; without it every choice is automatic;
    may raise, which counts as None).
    """

    def __init__(self, batches: Iterable[BatchRecord], yields: Mapping[str, YieldRecord],
                 get_reaction: Callable[[str], dict[str, Any] | None],
                 identify: Callable[[str], str | None],
                 get_limiting: Callable[[str], tuple[int, str | None] | None] | None = None):
        active = [b for b in batches if not b.retired]
        self._by_stereo: dict[str, list[BatchRecord]] = {}
        self._by_experiment: dict[str, list[BatchRecord]] = {}
        for b in active:
            self._by_stereo.setdefault(b.core_stereo_id, []).append(b)
            self._by_experiment.setdefault(b.experiment_ref, []).append(b)
        for listed in self._by_stereo.values():
            listed.sort(key=lambda b: b.batch_no)
        for listed in self._by_experiment.values():
            listed.sort(key=lambda b: (b.product_index, b.batch_no))
        self._refs = {b.full_id: batch_ref(b, yields.get(b.full_id)) for b in active}
        self._get_reaction = get_reaction
        self._reactions: dict[str, dict[str, Any] | None] = {}
        self._identify = identify
        self._identified: dict[str, str | None] = {}
        self._get_limiting = get_limiting
        self._limiting: dict[str, tuple[int, str | None] | None] = {}

    # -- RouteData ---------------------------------------------------------------
    def batches_of(self, core_stereo_id: str) -> Sequence[BatchRef]:
        """Active batches of this stereo variant (oldest first)."""
        return tuple(self._refs[b.full_id] for b in self._by_stereo.get(core_stereo_id, ()))

    def batches_in(self, experiment_ref: str) -> Sequence[BatchRef]:
        """Active batches made in this experiment, by product index."""
        return tuple(self._refs[b.full_id] for b in self._by_experiment.get(experiment_ref, ()))

    def reaction(self, experiment_ref: str) -> dict[str, Any] | None:
        """A copy of the stored reaction of this experiment, or None."""
        if experiment_ref not in self._reactions:
            data = self._get_reaction(experiment_ref)
            self._reactions[experiment_ref] = data if isinstance(data, dict) else None
        stored = self._reactions[experiment_ref]
        return copy.deepcopy(stored) if stored is not None else None

    def identify(self, molfile: str) -> str | None:
        """The registered ``CORE_STEREO`` of this structure, or None. Never raises."""
        if not isinstance(molfile, str) or not molfile.strip():
            return None
        if molfile not in self._identified:
            try:
                found = self._identify(molfile)
            except Exception:  # noqa: BLE001 - an unreadable structure is simply unknown
                found = None
            self._identified[molfile] = found if isinstance(found, str) and found else None
        return self._identified[molfile]

    def stereo_molfile(self, core_stereo_id: str) -> str | None:
        """The structure of the first active batch of this stereo variant."""
        listed = self._by_stereo.get(core_stereo_id)
        return listed[0].molfile if listed and listed[0].molfile else None

    def limiting_choice(self, experiment_ref: str) -> tuple[int, str | None] | None:
        """The starting material chosen for this experiment, ``(row, name
        recorded then)``, or None (automatic). Never raises: a choice that
        cannot be read counts as automatic."""
        if self._get_limiting is None:
            return None
        if experiment_ref not in self._limiting:
            try:
                found = self._get_limiting(experiment_ref)
            except Exception:  # noqa: BLE001 - an unreadable choice means "automatic"
                found = None
            ok = (isinstance(found, tuple) and len(found) == 2
                  and isinstance(found[0], int) and not isinstance(found[0], bool)
                  and (found[1] is None or isinstance(found[1], str)))
            self._limiting[experiment_ref] = (found[0], found[1]) if ok else None
        return self._limiting[experiment_ref]

    def stereo_label(self, core_stereo_id: str) -> str:
        return core_stereo_id
