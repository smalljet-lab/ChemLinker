"""Shared data types: the contract between chemistry, storage, CLI and host.

Everything here is a plain frozen dataclass with JSON-friendly fields, so the
same objects can be returned by the CLI, sent over native messaging, and stored
by any repository backend.

Terminology (see README / chem.py for the chemistry rules):

* CORE   - one constitution, all stereo ignored. Number ``core_no``.
* STEREO - one stereo variant within a core. Number ``stereo_no`` (from 1).
* BATCH  - one synthesis (one Sciformation experiment) of a stereo variant.
           Number ``batch_no`` (from 1 within core+stereo).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Stereo modes that can be stored in the registry.
#   none      - the structure has no stereo element at all
#   abs       - every stereo element is defined, absolute as drawn
#   rac       - racemate: the drawn structure and its mirror image, 1:1
#   rel       - single enantiomer of known relative, unknown absolute configuration
#   undefined - stereo elements exist but none is defined
#   partial   - some stereo elements defined, some not (no AND/OR groups)
#   mixed     - any other combination of ABS / AND / OR groups
STEREO_MODES = ("none", "abs", "rac", "rel", "undefined", "partial", "mixed")

# Modes a user may force when confirming a registration (chemistry rule 4).
MODE_OVERRIDES = ("abs", "rac", "rel", "undefined")


@dataclass(frozen=True)
class Normalized:
    """Result of ``chem.normalize``. The first five fields are what the
    registry works with; the rest is supporting information.

    core_key     Standard InChI with all stereo layers omitted. Lookup key of a CORE.
    stereo_key   Canonical JSON text describing the set of stereoisomers the
                 drawing denotes (see chem.py). Lookup key of a STEREO within a core.
    stereo_mode  One of STEREO_MODES, derived from stereo_key's structure.
    inchikey     InChIKey of the structure as drawn (with stereo). Display/search only.
    smiles       RDKit canonical isomeric CXSMILES. Display only, never a key.
    """

    core_key: str
    stereo_key: str
    stereo_mode: str
    inchikey: str
    smiles: str
    # --- supporting information -------------------------------------------
    inchi: str = ""              # standard InChI as drawn (with stereo layers)
    core_inchikey: str = ""      # InChIKey of core_key
    formula: str = ""            # Hill formula of all fragments, e.g. "C2H7N.ClH"
    detected_mode: str = ""      # mode before any override was applied
    mode_overridden: bool = False
    molblock: str = ""           # the input molfile verbatim; generated if input was SMILES
    input_format: str = "molfile"  # "molfile" or "smiles"
    warnings: tuple[str, ...] = ()
    # Stereo that is drawn but did NOT make it into the keys (a wedge on an atom
    # that is not a recognised stereocentre or axis, an allene, ...). Registering
    # such a structure could give two stereoisomers the same ID, so callers must
    # refuse to register unless the user explicitly accepts each of these.
    ignored_stereo: tuple[str, ...] = ()
    rdkit_version: str = ""
    inchi_version: str = ""


@dataclass(frozen=True)
class ExperimentRef:
    """Identifies one Sciformation experiment.

    ref   Stable identifier used for idempotency. For Sciformation this is the
          internal primary key as a string, e.g. "4180". Required.
    code  Human code shown in the ELN, e.g. "ABC-008". Optional.
    url   Link that opens the experiment. Optional.
    """

    ref: str
    code: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class CoreRecord:
    core_no: int
    core_key: str
    inchikey: str        # InChIKey of core_key
    formula: str
    created_at: str      # ISO 8601 UTC, e.g. "2026-11-17T15:04:05Z"


@dataclass(frozen=True)
class StereoRecord:
    core_no: int
    stereo_no: int
    core_stereo_id: str  # formatted "CORE_STEREO", e.g. "000042_02"
    stereo_key: str
    stereo_mode: str
    inchikey: str
    smiles: str
    molfile: str         # molfile of the first registration of this variant
    created_at: str
    batch_count: int = 0  # number of batches, retired ones included


@dataclass(frozen=True)
class BatchRecord:
    full_id: str         # formatted "CORE_STEREO_BATCH", e.g. "000042_02_003"
    core_no: int
    stereo_no: int
    batch_no: int
    core_stereo_id: str
    experiment_ref: str
    experiment_code: str | None
    experiment_url: str | None
    product_index: int   # 1-based position of the product within the reaction
    molfile: str
    smiles: str
    inchikey: str
    stereo_mode: str
    source: str          # "extension", "cli" or "backfill"
    created_at: str
    rdkit_version: str = ""
    inchi_version: str = ""
    retired_at: str | None = None
    retired_reason: str | None = None

    @property
    def retired(self) -> bool:
        return self.retired_at is not None


@dataclass(frozen=True)
class LookupResult:
    """What the registry knows about a normalised structure.

    core            The core, or None if this constitution was never registered.
    stereo          The exact stereo variant, or None if not registered.
    batches         All batches of ``stereo`` (oldest first, retired included).
    other_stereos   The other stereo variants of the same core.
    existing_batch  Active batch of this exact compound in the given experiment
                    (any product index). If set, registering again returns it.
    slot_conflict   Active batch that occupies (experiment_ref, product_index)
                    with a DIFFERENT compound. Registering is refused until it
                    is retired.
    """

    core: CoreRecord | None = None
    stereo: StereoRecord | None = None
    batches: tuple[BatchRecord, ...] = ()
    other_stereos: tuple[StereoRecord, ...] = ()
    existing_batch: BatchRecord | None = None
    slot_conflict: BatchRecord | None = None


@dataclass(frozen=True)
class NameRecord:
    """The name stored for one stereo variant (meant for the IUPAC name). An
    annotation only: never part of a key, may change or disappear.

    ``source`` says where the text came from: ``user`` (typed or pasted) or
    ``chemdraw`` (generated by the local ChemDraw); see
    ``protocol.NAME_SOURCES``."""

    core_stereo_id: str
    text: str
    updated_at: str      # ISO 8601 UTC of the last change
    source: str = "user"


@dataclass(frozen=True)
class YieldRecord:
    """The yields stored for one batch (one product of one experiment), in
    percent as the ELN shows them. An annotation only: never part of a key,
    may change or disappear. At least one of the two values is set (a batch
    without any yield has no record); see ``protocol.YIELD_KINDS``."""

    full_id: str
    isolated: float | None
    analytical: float | None
    updated_at: str      # ISO 8601 UTC of the last change


@dataclass(frozen=True)
class RegistrationResult:
    batch: BatchRecord
    created: bool          # False: the batch already existed (idempotent repeat)
    core_created: bool = False
    stereo_created: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)
