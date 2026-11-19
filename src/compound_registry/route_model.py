"""Synthesis routes: the data model shared by the route builder and the
ChemDraw writer.

CONTRACT (written before the code that uses it). Three pieces meet here:

``routes.build_route(target, data) -> Route``
    Pure: walks back from a stereo variant through the stored reactions and
    returns every step that led to it (rules in ``protocol.py`` under
    "Synthesis routes"). It reads the registry only through ``RouteData``.

``cdxml.render_route(route) -> str``
    Pure: lays the route out and returns a CDXML document (text) that the
    ChemDraw desktop program opens as a reaction scheme.

``RegistryService.route(core_stereo_id, open=True) -> dict``
    Implements ``RouteData`` over the repository, calls the two functions
    above, writes the file into the compound's folder and opens it.

Nothing in this module does I/O.

Display texts
-------------
The builder decides WHAT is written; the writer decides WHERE. Every text
below is final, one line, plain (no markup). The writer may only:
* wrap ``Step.above`` / ``Step.below`` lines that are too wide for the
  arrow at ", " boundaries;
* show digit runs as subscripts in ``Step.above`` / ``Step.below`` texts
  that the builder marked as formula-like (``Text.formula``).

Run line format (builder), exactly::

    "ABC-001, SM = 2.31 g, 52.4 %"          # all known
    "ABC-001, 52.4 %"                       # limiting reactant or its mass unknown
    "ABC-004 (EtOH), SM = 1.25 g, 64.5 %"   # this run's conditions differ (see Step)

Masses: the ELN value converted to g when it is 1000 mg or more, written with
three significant figures ("2.31 g", "93.0 mg", "0.915 g" stays "915 mg");
yields with at most two decimals and no trailing zeros ("52.4 %", "76.2 %",
"15 %").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence

__all__ = ["BatchRef", "RouteData", "Text", "Participant", "Candidate", "Run", "Step",
           "MissingReaction", "Route", "MAX_ROUTE_DEPTH", "MAX_ROUTE_STEPS"]

#: The builder stops walking back after this many generations (a note says so).
MAX_ROUTE_DEPTH = 30
#: ...and after this many steps in total.
MAX_ROUTE_STEPS = 200


@dataclass(frozen=True)
class BatchRef:
    """One ACTIVE batch (retired batches are never given to the builder)."""

    full_id: str
    core_stereo_id: str
    experiment_ref: str
    experiment_code: str | None
    experiment_url: str | None
    product_index: int
    #: Stored yields in percent, or None.
    isolated: float | None
    analytical: float | None


class RouteData(Protocol):
    """What the builder may ask the registry. Implemented by the service."""

    def batches_of(self, core_stereo_id: str) -> Sequence[BatchRef]:
        """Active batches of this stereo variant, in any order."""
        ...

    def batches_in(self, experiment_ref: str) -> Sequence[BatchRef]:
        """Active batches made in this experiment, by product index."""
        ...

    def reaction(self, experiment_ref: str) -> dict | None:
        """The stored reaction of this experiment, exactly in the shape of
        ``set_reaction``'s ``reaction`` parameter (every documented key
        present), or None when none is stored."""
        ...

    def identify(self, molfile: str) -> str | None:
        """The ``CORE_STEREO`` of the registered stereo variant whose keys equal
        those of this structure, read with the registry's default stereo mode
        (exactly what "Create ID" would find). None when it is not registered
        or the structure cannot be read. Never raises."""
        ...

    def stereo_molfile(self, core_stereo_id: str) -> str | None:
        """The molfile to draw for a registered stereo variant (the structure
        of its first active batch), or None."""
        ...

    def limiting_choice(self, experiment_ref: str) -> tuple[int, str | None] | None:
        """The starting material the user chose for this experiment
        (``set_limiting``): ``(row, name recorded at that time)``, or None
        for automatic."""
        ...

    def stereo_label(self, core_stereo_id: str) -> str:
        """The text written under a registered compound: its ``CORE_STEREO``
        (e.g. "EX0001_01")."""
        ...


@dataclass(frozen=True)
class Text:
    """One display line. ``formula`` true: digit runs that directly follow a
    letter or ")" are subscripts (e.g. "Pd(PPh3)4", "K2CO3", "CH2Cl2")."""

    text: str
    formula: bool = False


@dataclass(frozen=True)
class Participant:
    """A compound drawn in the scheme.

    ``key`` identifies it across steps and decides which runs form one step:
      "cs:<CORE_STEREO>"   a registered stereo variant;
      "ik:<InChIKey>"      an unregistered structure (RDKit's standard InChIKey);
      "nm:<name>"          no readable structure: the lower-cased name;
      "rw:<ref>:<row>"     nothing usable: one experiment's row (never merges).
    ``molfile`` None means it is written as ``name`` text instead of drawn.
    ``label`` is written under the drawing (the CORE_STEREO for registered
    compounds, else None).
    """

    key: str
    molfile: str | None
    name: str | None
    label: str | None
    core_stereo_id: str | None = None


@dataclass(frozen=True)
class Candidate:
    """One reactant of a run that can be chosen as its starting material."""

    row: int
    #: Name as stored, else its formula, else None.
    name: str | None
    eq: float | None
    #: Formatted like on a run line ("1.90 g", "66.0 mg"), or None.
    mass: str | None


@dataclass(frozen=True)
class Run:
    """One experiment drawn under a step's arrow."""

    experiment_ref: str
    experiment_code: str | None
    experiment_url: str | None
    yield_percent: float
    yield_kind: Literal["isolated", "analytical"]
    #: Mass of the limiting reactant in grams, or None when unknown.
    sm_grams: float | None
    #: The final line (see "Run line format").
    line: str
    #: The CORE_STEREO this run makes (the step's product).
    compound: str = ""
    #: Reactant row used as starting material (None: none), who chose it
    #: ("user" / "auto" / None), and what the automatic rule gives.
    limiting_row: int | None = None
    limiting_by: Literal["user", "auto"] | None = None
    auto_row: int | None = None
    #: True when two or more reactants have 1 equivalent.
    ambiguous: bool = False
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class Step:
    """One reaction arrow: ``reactants`` -> ``product``.

    ``reactants`` are in the order of the first run's reactants grid, the
    registered one with the longest own history first. ``above`` are the
    reagent lines, ``below`` the solvent/temperature lines followed by one
    line per run (``runs[i].line``). ``runs`` are in natural ELN-number order.
    """

    product: Participant
    reactants: tuple[Participant, ...]
    above: tuple[Text, ...]
    below: tuple[Text, ...]
    runs: tuple[Run, ...]


@dataclass(frozen=True)
class MissingReaction:
    """An experiment of the route whose reaction is not stored."""

    experiment_ref: str
    experiment_code: str | None
    experiment_url: str | None
    #: The CORE_STEREO made there.
    compound: str


@dataclass(frozen=True)
class Route:
    """``steps`` in synthesis order: a step comes after every step that makes
    one of its reactants. The last step makes ``target`` (when any step does).
    """

    target: Participant
    steps: tuple[Step, ...]
    missing: tuple[MissingReaction, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
