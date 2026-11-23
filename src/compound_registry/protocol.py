"""Native-messaging protocol between the browser extension (Firefox and Chrome)
and the host.

This module is the CONTRACT. ``extension/src/shared/protocol.ts`` mirrors it;
change both together and bump :data:`PROTOCOL_VERSION` for anything that is not
purely additive. It contains no I/O: ``host.py`` does the framing and calls the
service, this module only says what a valid request is and what goes back.

Framing (native messaging)
--------------------------
Each message is one JSON document encoded as UTF-8, preceded by its byte length
as an unsigned 32-bit integer in NATIVE byte order. The host reads frames from
binary stdin and writes frames to binary stdout until stdin reaches EOF.
Nothing but frames may ever reach stdout.

The browser refuses messages from the host that are larger than 1 MB, so every
response must be at most :data:`MAX_RESPONSE_BYTES`. The host refuses requests
larger than :data:`MAX_REQUEST_BYTES`.

Request
-------
``{"id": <text or integer>, "v": 1, "op": "<name>", "params": {...}}``

* ``id``     chosen by the extension, echoed in the response. Text of 1..64
             characters or a non-negative integer.
* ``v``      :data:`PROTOCOL_VERSION`. Any other value is answered with
             ``protocol_mismatch``, except for ``ping`` (which reports the
             host's version so the extension can explain the mismatch).
* ``params`` an object; unknown keys are refused (``bad_request``).

Response
--------
``{"id": <echoed id or null>, "ok": true,  "result": {...}}``
``{"id": <echoed id or null>, "ok": false, "error": {"code": "...", "message": "...", ...}}``

``id`` is null when the request's id could not be read. ``error`` is
``RegistryError.to_dict()`` for registry errors (so ``slot_conflict`` carries
``existing_id``) or one of :data:`HOST_ERROR_CODES`.

Operations
----------
``ping``      params ``{}``
              result: :data:`PING_RESULT_KEYS` - the keys of
              ``RegistryService.doctor()`` listed there, plus
              ``protocol_version``, ``host_version``, ``home_source``
              (one of :data:`HOME_SOURCES`) and ``mode_options``.

``analyze``   params: one of ``molfile`` / ``smiles``; optional ``mode``,
              ``experiment_ref``, ``product_index``.
              result: ``RegistryService.analyze(..., with_svg=True)`` verbatim.
              Read-only.

``register``  params: one of ``molfile`` / ``smiles``; ``mode`` (optional);
              ``experiment_ref`` and ``product_index`` (required);
              ``experiment_code``, ``experiment_url`` (optional);
              ``confirm`` (required): ``{"inchikey": ..., "stereo_mode": ...,
              "key_digest": ...}`` copied from the ``structure`` of the
              analysis the user looked at;
              ``accept_ignored_stereo`` (optional): the exact
              ``structure.ignored_stereo`` texts the user accepted.
              The host re-analyses the structure and refuses with
              ``confirmation_mismatch`` (nothing written) unless all three
              values equal ``confirm``. ``key_digest`` is the decisive one: it
              covers exactly the keys that select the CORE_STEREO (see
              ``service.key_digest``); the other two make the refusal
              explainable. ``ignored_stereo`` counts as
              accepted only when the accepted list equals it as a set.
              result: ``RegistryService.register(..., source="extension")``
              verbatim.

``retire``    params: ``id`` (the full ID), ``reason`` (non-blank).
              result: ``RegistryService.retire`` verbatim.

``find``      params: ``id`` - a full ID, a ``CORE_STEREO`` or a ``CORE`` number.
              result: ``RegistryService.find_by_id(..., with_svg=True)``
              verbatim: every batch, and so every experiment, of that
              compound. Read-only. ``not_found`` when nothing matches.

``compounds`` params, all optional: ``text`` (one line of search text);
              at most one of ``molfile`` / ``smiles`` (a drawn query
              structure) with ``match`` (one of :data:`MATCH_MODES`, default
              ``substructure``); ``order`` (one of :data:`ORDERS`, by CORE
              number, default ``asc``); ``offset`` (default 0) and ``limit``
              (1..:data:`MAX_PAGE_SIZE`, default :data:`DEFAULT_PAGE_SIZE`).
              result: ``RegistryService.compounds(..., with_svg=True)``
              verbatim: the registered compounds, ONE ENTRY PER CORE with its
              stereo variants, optionally filtered, one page at a time.
              Read-only. See "Compound search" below.

``set_name``  params: ``core_stereo_id`` (a registered ``CORE_STEREO``) and
              ``name`` (required key: one line of text, or null to remove the
              stored name).
              result: ``RegistryService.set_name`` verbatim. ``not_found``
              when no such stereo variant exists.

``generate_name`` params: ``core_stereo_id`` (a registered ``CORE_STEREO``);
              ``replace`` (optional true/false, default false: an already
              stored name is kept and nothing is generated).
              result: ``RegistryService.generate_name`` verbatim:
              ``{"core_stereo_id", "name", "generated", "changed", "message",
              "warnings"}``. ``name`` is the name stored after the call (or
              null); ``generated`` is true when this call got a name from the
              local naming program and stored it; otherwise ``message`` says
              in plain words why not (no naming program on this PC, the
              structure could not be named safely, a name is already stored).
              Not getting a name is never an error. ``not_found`` when no such
              stereo variant exists. The naming program gets
              :data:`NAME_GENERATION_TIMEOUT_S` seconds (usually it needs
              about 3); stopping it after that can take a few seconds more.
              The host answers one request at a time, so the limit is kept
              below the 30 s the extension waits for other operations. See
              "Names" below.

``set_yield`` params: ``id`` (a full ID); ``experiment_ref`` and
              ``product_index`` (required: the experiment and product row the
              yields were read from); ``isolated`` and ``analytical``
              (required keys: a number of percent from 0 to
              :data:`MAX_YIELD_PERCENT`, or null for "none shown").
              result: ``RegistryService.set_yield`` verbatim: ``{"id",
              "yield", "changed", "warnings"}``; ``yield`` is what is stored
              after the call (see "Yields" below), null when both values are
              null. ``not_found`` for an unknown ID; ``yield_mismatch``
              (nothing written) when the batch was not registered for exactly
              this ``experiment_ref`` and ``product_index``; ``bad_request``
              for a value that is not a number in range.

``yields``    params, all optional: ``text`` (one line; see "Yields");
              ``only_recorded`` (true/false, default false: true lists only
              batches that have a yield); ``order`` (one of :data:`ORDERS`,
              default ``asc``); ``offset`` (default 0) and ``limit``
              (1..:data:`MAX_YIELD_PAGE_SIZE`, default
              :data:`DEFAULT_YIELD_PAGE_SIZE`).
              result: ``RegistryService.yields`` verbatim: ``{"total",
              "registry_total", "offset", "limit", "items", "warnings"}``.
              Read-only. See "Yields" below.

``compound_files`` params: ``core_stereo_id`` (a registered ``CORE_STEREO``).
              result: ``RegistryService.compound_files`` verbatim: the
              compound's folder on this PC, the recorded spectra of its
              batches and the files in the folder. Read-only. ``not_found``
              when no such stereo variant exists. See "Compound folders and
              spectra" below.

``read_pdf``  params: ``core_stereo_id``; exactly one of ``name`` (a PDF
              lying directly in the compound's folder) or ``full_id`` with
              ``spectrum_key`` (the PDF recorded for that spectrum of a batch
              of this stereo variant); ``offset`` (default 0).
              result: ``RegistryService.read_pdf`` verbatim: ``{"name",
              "size", "offset", "data", "eof"}``, one piece of the file
              (``data`` is base64 of at most :data:`PDF_CHUNK_BYTES` bytes
              starting at ``offset``). Read-only. ``not_found`` for an unknown
              stereo variant or spectrum; ``file_unavailable`` when there is
              no such file, it is not a PDF, or it is larger than
              :data:`MAX_PDF_BYTES`. See "Compound folders and spectra".

``set_reaction`` params: ``experiment_ref`` (required), ``experiment_code``,
              ``experiment_url`` (optional), ``reaction`` (required): what the
              experiment page shows about the reaction (see "Reactions").
              result: ``RegistryService.set_reaction`` verbatim:
              ``{"experiment_ref", "changed", "updated_at", "warnings"}``.
              ``not_found`` when no batch (active or retired) of the registry
              was made in that experiment: only experiments that produced a
              registered compound are stored.

``route``     params: ``core_stereo_id`` (a registered ``CORE_STEREO``),
              ``open`` (default true).
              result: ``RegistryService.route`` verbatim (see "Synthesis
              routes"). Writes one new ChemDraw file into the compound's
              folder and, when ``open`` is true, asks Windows to open it (with
              ChemDraw). Never overwrites a file. ``not_found`` when no such
              stereo variant exists.

``set_limiting`` params: ``experiment_ref`` (required), ``row`` (required:
              the reactant row, 1..999, that is the starting material "SM" of
              this experiment's run line, or null = automatic again).
              result: ``RegistryService.set_limiting`` verbatim:
              ``{"experiment_ref", "row", "name", "changed", "warnings"}``
              (``name`` = that reactant's name, null for automatic).
              ``not_found`` when no reaction is stored for the experiment;
              ``invalid_argument`` when it has no reactant in that row. See
              "Synthesis routes".

Display-only additions (protocol version unchanged, purely additive)
--------------------------------------------------------------------
* ``folder`` in ``register``: ``{"path", "created"}``, the compound's folder
  on this PC (``created`` true when this call made it), or null when no
  folder could be made (then ``warnings`` says why). ``files_folder`` in
  ``ping``: the folder that holds the compound folders (text), or null.
* ``yield`` on every batch object of ``analyze`` (``batches``,
  ``slot_conflict``), ``register`` and ``retire`` (``batch``) and ``find``
  (``variants[].batches``, ``experiments[].batches``): the stored yield
  ``{"isolated", "analytical", "updated_at"}`` or null. See "Yields".
* ``structure.properties`` in ``analyze`` / ``register`` and
  ``variants[].properties`` in ``find``: ``{"sum_formula", "mol_weight",
  "exact_mass", "composition": [{"element", "percent"}, ...]}`` or null when
  they cannot be computed. All fragments of the structure together (a salt
  counts with its counter-ion, like the ELN's own MW). ``mol_weight`` is the
  average molar mass in g/mol (3 decimals), ``exact_mass`` the monoisotopic
  mass (5 decimals), ``composition`` the mass percentages (2 decimals) in the
  element order of ``sum_formula`` (Hill). Never part of any key.
* ``name`` in ``analyze`` (top level) and on every variant in ``find``: the
  name stored for that ``CORE_STEREO`` as ``{"text", "updated_at",
  "source"}``, or null (also null while the compound is not registered).
  ``source`` is one of :data:`NAME_SOURCES`. ``find`` variants also carry
  ``inchi``.
* ``name_generator`` in ``ping``: ``{"available": true/false, "label": text
  or null}``: whether a local naming program was found on this PC and what
  to call it ("ChemDraw"). Found does not mean it will work (licence).

Names
-----
A name (meant for the IUPAC name) is text stored per ``CORE_STEREO``. It is
an annotation: it is not part of any key, may be changed or removed at any
time, and every change is recorded in the registry's event log.

A name comes from one of two places (``source``):

``user``      typed or pasted by the user (``set_name``).
``chemdraw``  generated by the ChemDraw desktop program installed on the same
              PC (``generate_name``). The host starts its own hidden ChemDraw,
              hands it the structure, reads the name back and closes it;
              a ChemDraw window the user has open is not touched, and nothing
              leaves the PC. RDKit cannot name structures, and no online
              naming service is ever used.

A generated name is stored only when it can be trusted: the structure
ChemDraw read back must be the registered one (same standard InChI), and the
name must say what the registry's stereo variant says ("rac-" for a racemate,
"rel-" for relative stereo, neither for the others). Variants this cannot be
checked for (mixed AND/OR groups, chiral axes) are not named automatically.
Editing a generated name makes it a ``user`` name.

Yields
------
A yield belongs to one batch, that is to one product of one experiment (one
full ID). Two values can be stored, both in percent as the ELN shows them:
``isolated`` and ``analytical``; each is a number or null. The stored object
is ``{"isolated", "analytical", "updated_at"}``; a batch for which both are
null has no yield (null). Like a name, a yield is an annotation: it is not
part of any key, it is not written to the issued-ID journal, it may change at
any time, and every change is recorded in the registry's event log.

The registry mirrors the ELN. The extension reads the two yield fields of a
product row while the experiment is shown read-only (the saved state, never a
half-typed value) and sends ``set_yield`` whenever they differ from what is
stored, also when the ELN shows none any more (both null: the stored yield is
removed). The host rounds a value to two decimals. ``experiment_ref`` and
``product_index`` are a guard, not a lookup: a yield read from another
experiment or another product row is never stored for this ID.

``yields`` lists the batches of the registry, ONE ROW PER BATCH (retired ones
included), each with its experiment and its yield (null when none is
recorded), ordered by ELN number (digit runs compared as numbers, so ABC-9
comes before ABC-10; batches without an ELN number last, by their internal
key), then by product number. A row is ``{"experiment_ref",
"experiment_code", "experiment_url", "product_index", "full_id",
"core_stereo_id", "name", "yield", "created_at", "retired"}``; ``name`` is the
text of the name stored for the stereo variant, or null. ``text`` keeps a row
when it occurs (ignoring case) in the row's ELN number, full ID or name, or
equals its internal experiment key. ``total`` counts the rows that match,
``registry_total`` every batch.

Reactions
---------
A reaction belongs to one experiment (``experiment_ref``, the ELN's internal
key) and is what its page shows: reactants, reagents, solvents, temperature
and the reference amount. Like a yield it is an annotation mirrored from the
ELN: not part of any key, not in the issued-ID journal, replaced as a whole by
every ``set_reaction`` whose content differs (``changed`` false when the same
content was stored already), every change recorded in the event log
(``reaction_set``). The extension sends it right after "Create ID" and
whenever an experiment with a registered product is shown in its saved
(read-only) state and differs from what it last sent.

``reaction`` is an object with exactly these keys::

    {"reactants": [Component, ...],     # the reactants grid, in page order
     "reagents":  [Component, ...],     # the reagents grid, in page order
     "solvents":  [{"name": text, "volume": number|null, "unit": text|null}, ...],
     "temperature": text|null,          # the "T (°C)" field as shown, e.g. "40", "rt"
     "reference_amount": {"value": number, "unit": text}|null,   # e.g. 0.701 mmol
     "amounts_shown": "actual"|"planned"|null}   # which amounts the page displayed

    Component = {
     "row": 1..999,                     # position in its grid
     "name": text|null,                 # substance name as shown (no container text);
                                        # "ABC-001" when the row is a product of that experiment
     "formula": text|null, "cas": text|null,
     "source_experiment_ref": text|null,  # internal key of the experiment this row was
     "source_experiment_code": text|null, # taken from (the ELN's own link), e.g. "4110" / "ABC-001"
     "molfile": text|null,              # the row's structure; null when none or unreadable
     "actual":  Amounts|null,           # the "Actual amounts" values
     "planned": Amounts|null}           # the "Planned amounts" values

    Amounts = {"eq": number|null,
               "n": number|null, "n_unit": text|null,     # amount of substance, e.g. 0.701 "mmol"
               "m": number|null, "m_unit": text|null,     # mass, e.g. 100 "mg"
               "v": number|null, "v_unit": text|null}     # volume, e.g. 1.31 "ml"

Every number is finite and not negative. Texts are one line (no control
characters), at most :data:`MAX_REACTION_TEXT` characters; a molfile at most
:data:`MAX_STRUCTURE_CHARS`; a list at most :data:`MAX_REACTION_ROWS` items.
Unknown keys anywhere are refused (``bad_request``).

Synthesis routes
----------------
``route`` draws how a stereo variant was made, as a ChemDraw document
(CDXML): every step that led to it, recursively, from the reactions stored
with ``set_reaction``.

* The steps making a compound C are the stored reactions of the experiments
  in which a batch of C was made (retired batches excluded), except an
  experiment where C is also one of the reactants (recovered starting
  material).
* A run (one experiment) is drawn only when its yield for C is above zero
  (isolated, else analytical). A compound without such a run ends the route.
* Runs whose reactants are the same compounds form ONE step: one arrow,
  reactants drawn left of it joined by "+", reagents written above it,
  solvents (and temperature) below it, then one line per run:
  ``"ABC-001, SM = 2.31 g, 52.4 %"`` (SM = mass of the limiting reactant).
  Reagents / solvents that differ between the runs are written on each run's
  own line instead of at the arrow.
* A reactant is a registered compound when the ELN links it to an
  experiment whose registered product has the same structure, or else when
  its structure is a registered stereo variant. Its own steps are then drawn
  before, and it is labelled with its ``CORE_STEREO``.
* The limiting reactant ("SM") of a run is the one the user chose for that
  experiment with ``set_limiting``, when that choice still applies (the
  stored reaction still has a reactant in that row with the name recorded at
  the time of the choice; otherwise a note says the choice was ignored).
  Automatic choice: among the reactants with 1 equivalent, the one whose
  amount is closest to the reference amount (within 0.5 %; a tie goes to the
  first row), else the only one with 1 equivalent, else none (then ``SM`` is
  left out). Actual amounts are used, planned ones when no actual value is
  recorded. A choice is stored per experiment beside its reaction, survives
  every later ``set_reaction``, and is recorded in the event log
  (``limiting_set``).
* Layout: a step whose reactant is the product just drawn continues the row
  from it; its OTHER reactants are then drawn ABOVE its arrow (below the
  reagent lines). ChemDraw re-reads every row by geometry; it keeps them as
  objects above that arrow (agents, the middle part of its reaction SMILES
  "A>B>C"), never as products of the previous arrow. Known limitation: a
  continued step exported from ChemDraw as SMILES/RXN lists those
  co-reactants as reagents. (The file itself also names them in the step's
  ``ReactionStepReactants``.) A step that starts a row has its reactants left of the arrow,
  joined by "+".

The result is ``{"core_stereo_id", "file", "opened", "open_message",
"steps", "experiments", "missing", "notes", "choices", "cdxml",
"warnings"}``:

``file``         ``{"path", "name"}`` of the file written (a new name each
                 time: ``<CORE_STEREO>_route_<YYYY-MM-DD_HHMMSS>.cdxml``), or
                 null when nothing was drawn (no step) or it could not be
                 written (``warnings`` says why).
``opened``       true when Windows was asked to open the file;
                 ``open_message`` says why not otherwise (or null).
``steps``        the number of reaction arrows drawn.
``experiments``  ELN numbers of the runs drawn, in drawing order.
``missing``      experiments of the route whose reaction is not stored yet:
                 ``[{"experiment_ref", "experiment_code", "experiment_url",
                 "compound"}]`` (``compound`` = the ``CORE_STEREO`` made
                 there). Opening each once in the ELN stores it.
``notes``        plain-language remarks (runs left out and why, links that
                 did not match, a route cut because it loops).
``choices``      one entry per run drawn, in drawing order, for switching
                 the starting material: ``{"experiment_ref",
                 "experiment_code", "compound", "row", "by", "auto_row",
                 "ambiguous", "candidates"}``. ``row`` = the reactant row used
                 as SM (null: none); ``by`` = ``"user"``, ``"auto"`` or null;
                 ``auto_row`` = what the automatic choice gives (null: none);
                 ``ambiguous`` = true when two or more reactants have 1
                 equivalent; ``candidates`` = every reactant of the run:
                 ``[{"row", "name", "eq", "mass"}]`` (``name`` as stored,
                 else its formula, else null; ``eq`` a number or null;
                 ``mass`` the formatted mass as on a run line, e.g. "1.90 g",
                 or null).
``cdxml``        the document text when it is at most
                 :data:`MAX_ROUTE_CDXML_CHARS` characters (for "Save a copy"),
                 else null.

Compound folders and spectra
----------------------------
Every stereo variant gets a folder on the registry's PC:
``<files folder>/<CORE_STEREO>`` (``files.folder`` in ``config.toml``,
default ``compounds`` inside the registry home). ``register`` makes it when an
ID is created (also for an ID that already existed) and puts the structure in
it as ``<CORE_STEREO>.mol``. The registry never changes, moves or removes
anything in a folder afterwards, and never overwrites a file. Not being able
to make the folder is never an error: the ID is issued all the same and
``warnings`` says what went wrong. Other programs (the MestReNova add-on) put
spectra into the folder; what they measured is recorded per batch in the
registry (``RegistryService.record_spectra``).

``compound_files`` answers ``{"core_stereo_id", "folder", "spectra", "files",
"files_truncated", "warnings"}``:

``folder``   ``{"path", "exists"}``, or null when folders cannot be used for
             this ID (its text is not usable as a folder name).
``spectra``  the spectra recorded for the batches of this stereo variant, by
             ID and spectrum key. Each is ``{"full_id", "spectrum_key",
             "updated_at", "experiment_code", "title", "comment", "sample",
             "nucleus", "solvent", "experiment", "pulse_sequence",
             "frequency_mhz", "dimensions", "report", "parameters",
             "mnova_file", "pdf_file", "pdf_available", "crude"}``. ``crude``
             is true for a spectrum saved with the add-on's "Crude" box: of
             such a spectrum only ``mnova_file`` is sent (every other detail
             is null or empty, ``pdf_available`` false) and the page shows
             just that path. Text fields are
             text or null; ``frequency_mhz`` a number or null; ``dimensions``
             a whole number or null; ``report`` the multiplet report as ONE
             LINE OF PLAIN TEXT or null (the HTML report is never sent);
             ``parameters`` a list of ``{"name", "value"}`` (acquisition
             parameters, texts); ``mnova_file`` / ``pdf_file`` the recorded
             paths as text; ``pdf_available`` true when ``pdf_file`` can be
             read with ``read_pdf`` right now.
``files``    the regular files lying directly in the folder (no subfolders),
             by name, at most :data:`MAX_FOLDER_FILES` (``files_truncated``
             says whether more exist): ``{"name", "size", "modified",
             "is_pdf", "recorded"}``; ``modified`` is ISO 8601 UTC;
             ``recorded`` is true when the file is the ``mnova_file`` or
             ``pdf_file`` of one of ``spectra``.

``read_pdf`` only ever serves a PDF: a regular file whose name ends in
``.pdf``, that starts with ``%PDF-`` and is at most :data:`MAX_PDF_BYTES`
long. With ``name`` the file must lie directly in the compound's folder
(``name`` is a plain file name, never a path); with ``full_id`` and
``spectrum_key`` it is the ``pdf_file`` recorded for that spectrum. The page
never sends a path. A file is read piece by piece (Firefox limits a message
to 1 MB): ask again with ``offset`` advanced by the bytes received until
``eof`` is true; if ``size`` differs from the first answer the file changed
meanwhile and the read must start again.

Compound search
---------------
A CORE is listed when it matches the ``text`` (if given) AND the query
structure (if given); without either, every CORE is listed.

``text`` matches a CORE when ANY of these holds (``matched_by`` names which):

``id``         the text is an ID of this registry (full ID, ``CORE_STEREO``,
               ``CORE`` or its plain number) naming this CORE;
``inchikey``   the text is an InChIKey (or the first block of one) equal to
               the InChIKey of one of its stereo variants or of the CORE;
``structure``  the text starts with ``InChI=`` and is a readable InChI, or is
               a readable SMILES, and that structure has the same
               constitution as the CORE (same core key; stereo is ignored);
``name``       the text occurs (ignoring case) in a stored name of one of its
               stereo variants;
``formula``    the text equals the CORE's formula or its sum formula;
``experiment`` the text occurs (ignoring case) in the ELN number of an
               experiment in which the CORE was made, or equals that
               experiment's internal key.

The query structure matches a CORE (``matched_by`` then contains ``drawn``):

``substructure``  when the stored structure of one of its stereo variants
                  contains the query as a substructure (stereo, and explicit
                  hydrogens of the query, are ignored);
``exact``         when the query has the same constitution as the CORE.

A text or query structure that cannot be used as asked is never an error by
itself: the result's ``notes`` say in plain words how the search was read
(for example "Read as SMILES" or "The drawn structure could not be read").
An unreadable or empty query structure matches nothing.

Which registry
--------------
The host NEVER creates a registry: a new, empty one would issue IDs that
already exist in the user's notebooks. If the configured home folder, its
``config.toml`` or the database is missing, every request is answered with
``registry_missing``. Registries are created only by an explicit installer
step. The host decides its registry once at start-up; if the configuration
says something else at a later request, it answers that request with
``registry_changed`` and exits, so the next connection starts from the new
configuration and the panel can show the registry actually in use.

Why ``confirm``: an issued ID is permanent, and the user confirms a drawing
and a stereo mode in the panel. If what the host computes at registration time
is not what the user saw (the text changed, the configured default mode
changed, the chemistry software changed), no ID is issued.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .errors import RegistryError

__all__ = [
    "PROTOCOL_VERSION", "HOST_NAME", "EXTENSION_ID", "CHROME_EXTENSION_ID", "CHROME_ORIGIN", "MAX_REQUEST_BYTES", "MAX_RESPONSE_BYTES",
    "MAX_STRUCTURE_CHARS", "OPS", "HOST_ERROR_CODES", "HOME_SOURCES", "PING_RESULT_KEYS",
    "MATCH_MODES", "ORDERS", "MATCH_REASONS", "MAX_SEARCH_TEXT", "MAX_NAME_TEXT",
    "NAME_SOURCES", "NAME_GENERATION_TIMEOUT_S",
    "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "MAX_OFFSET", "MAX_ITEM_EXPERIMENTS",
    "MAX_YIELD_PERCENT", "DEFAULT_YIELD_PAGE_SIZE", "MAX_YIELD_PAGE_SIZE", "YIELD_KINDS",
    "PDF_CHUNK_BYTES", "MAX_PDF_BYTES", "MAX_FOLDER_FILES", "MAX_FILE_NAME", "MAX_SPECTRUM_KEY",
    "MAX_REACTION_ROWS", "MAX_REACTION_TEXT", "MAX_REACTION_NUMBER", "AMOUNTS_SHOWN",
    "REACTION_KEYS", "COMPONENT_KEYS", "AMOUNT_KEYS", "SOLVENT_KEYS", "MAX_ROUTE_CDXML_CHARS",
    "LIMITING_BY",
    "CONFIRM_KEYS", "SOURCE", "ProtocolError", "Request", "parse_request", "ok_response", "error_response",
    "is_safe_url",
]

PROTOCOL_VERSION = 1

#: Name of the native messaging host (manifest ``name``, registry key, and the
#: argument of ``browser.runtime.connectNative``).
HOST_NAME = "compound_registry"

#: Add-on ID of the extension (``browser_specific_settings.gecko.id``). It is
#: the only entry of the host manifest's ``allowed_extensions``, and the host
#: also checks it against the caller ID Firefox passes on the command line.
EXTENSION_ID = "compound-registry@registry.local"

#: The Chrome build of the same extension (folder ``chrome-extension/``,
#: loaded unpacked). Chrome derives its ID from the public key in the
#: manifest (``extension/static/chrome-key.json``). Chrome starts the host with
#: this origin as first argument (and ``--parent-window=...`` on Windows); it is
#: the only entry of the Chrome host manifest's ``allowed_origins`` and the
#: host refuses (``forbidden_caller``) any other ``chrome-extension://``
#: caller. Both browsers use the SAME registry (one shared host configuration),
#: so an ID means the same compound whichever browser issued it.
CHROME_EXTENSION_ID = "imkbdobpgmdgbpfeencchphiegjledoj"
CHROME_ORIGIN = f"chrome-extension://{CHROME_EXTENSION_ID}/"

#: ``source`` stored with batches registered through the host.
SOURCE = "extension"

MAX_REQUEST_BYTES = 1024 * 1024      # larger frames are refused (frame_too_large)
MAX_RESPONSE_BYTES = 900_000         # Firefox's limit is 1 MB; keep a margin
MAX_STRUCTURE_CHARS = 500_000        # a molfile or SMILES

MAX_ID_TEXT = 64
MAX_REF_TEXT = 200                   # experiment_ref, experiment_code, a full ID
MAX_URL_TEXT = 2000
MAX_REASON_TEXT = 2000
MAX_PRODUCT_INDEX = 999
MAX_IGNORED_ITEMS = 50
MAX_IGNORED_ITEM_TEXT = 1000

OPS = ("ping", "analyze", "register", "retire", "find", "compounds", "set_name",
       "generate_name", "set_yield", "yields", "compound_files", "read_pdf",
       "set_reaction", "route", "set_limiting")

#: ``set_reaction``: limits of the ``reaction`` object (see "Reactions").
MAX_REACTION_ROWS = 50               # items per list (reactants, reagents, solvents)
MAX_REACTION_TEXT = 500              # any text in it
MAX_REACTION_NUMBER = 1e12           # any number in it
AMOUNTS_SHOWN = ("actual", "planned")
REACTION_KEYS = ("reactants", "reagents", "solvents", "temperature", "reference_amount",
                 "amounts_shown")
COMPONENT_KEYS = ("row", "name", "formula", "cas", "source_experiment_ref",
                  "source_experiment_code", "molfile", "actual", "planned")
AMOUNT_KEYS = ("eq", "n", "n_unit", "m", "m_unit", "v", "v_unit")
SOLVENT_KEYS = ("name", "volume", "unit")

#: ``route``: the document text is returned only up to this size.
MAX_ROUTE_CDXML_CHARS = 700_000

#: ``route`` choices: who chose a run's starting material.
LIMITING_BY = ("user", "auto")

#: ``read_pdf`` sends at most this many bytes of the file per answer (as
#: base64 they stay well below MAX_RESPONSE_BYTES).
PDF_CHUNK_BYTES = 600_000
#: Larger PDFs are not served.
MAX_PDF_BYTES = 100 * 1024 * 1024
#: ``compound_files`` lists at most this many files of a folder.
MAX_FOLDER_FILES = 200
MAX_FILE_NAME = 255                  # ``name`` of ``read_pdf``
MAX_SPECTRUM_KEY = 300               # ``spectrum_key`` of ``read_pdf``

#: The two yields of a batch (keys of the stored yield and of ``set_yield``).
YIELD_KINDS = ("isolated", "analytical")
#: A yield is a number of percent from 0 to this (impure material can give
#: more than 100).
MAX_YIELD_PERCENT = 1000
DEFAULT_YIELD_PAGE_SIZE = 200        # ``limit`` of ``yields``
MAX_YIELD_PAGE_SIZE = 500

#: ``source`` of a stored name: typed by the user, or generated by the local
#: ChemDraw.
NAME_SOURCES = ("user", "chemdraw")
#: The host gives the naming program this long for one name (plus a few
#: seconds to stop it after a time-out); the extension must wait longer than
#: this for ``generate_name``.
NAME_GENERATION_TIMEOUT_S = 20

#: ``match`` of ``compounds``: how a drawn query structure is compared.
MATCH_MODES = ("substructure", "exact")
#: ``order`` of ``compounds``: by CORE number.
ORDERS = ("asc", "desc")
#: ``matched_by`` values of a ``compounds`` entry.
MATCH_REASONS = ("id", "inchikey", "structure", "name", "formula", "experiment", "drawn")

MAX_SEARCH_TEXT = 2000               # ``text`` of ``compounds``
MAX_NAME_TEXT = 1000                 # a stored name
DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 50
MAX_OFFSET = 10_000_000
#: At most this many experiments are listed per ``compounds`` entry
#: (``experiment_count`` is always the full number).
MAX_ITEM_EXPERIMENTS = 10

#: Error codes the host itself produces (besides the registry's own codes).
HOST_ERROR_CODES = (
    "bad_request",            # not an object, wrong types, unknown or missing params
    "unknown_op",
    "protocol_mismatch",      # request "v" differs from PROTOCOL_VERSION
    "frame_too_large",        # request frame over MAX_REQUEST_BYTES; the host then exits
    "response_too_large",     # result does not fit MAX_RESPONSE_BYTES even without thumbnails
    "confirmation_mismatch",  # register: the structure is not what the user confirmed
    "forbidden_caller",       # started by an add-on other than EXTENSION_ID; the host then exits
    "registry_missing",       # no registry at the configured place; the host never creates one
    "registry_changed",       # the configured registry changed since the host started; it then exits
    "internal_error",         # unexpected exception; details are in the host log only
)

#: Keys of ``confirm`` in ``register``; each is the same-named key of
#: ``structure`` in the analysis the user confirmed.
CONFIRM_KEYS = ("inchikey", "stereo_mode", "key_digest")

#: Where the host took the registry home folder from.
HOME_SOURCES = ("host-config", "env", "default")

#: Keys of ``RegistryService.doctor()`` that ``ping`` returns.
PING_RESULT_KEYS = (
    "home", "db_path", "db_exists", "id_format", "default_stereo_mode", "stats", "integrity",
    "rdkit_version", "inchi_version", "chemistry_fingerprint", "python", "warnings",
    "name_generator", "files_folder",
)


class ProtocolError(RegistryError):
    """A request that breaks the protocol. ``request_id`` is the request's id
    when it could be read (so the error can be matched to its request)."""

    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None,
                 request_id: str | int | None = None):
        super().__init__(message, code=code)
        self.request_id = request_id


@dataclass(frozen=True)
class Request:
    """A validated request. ``params`` holds exactly the documented keys of the
    op; optional ones that were absent are present with value ``None`` (or
    ``[]`` for ``accept_ignored_stereo``)."""

    id: str | int
    op: str
    params: dict[str, Any]


# --------------------------------------------------------------------------- helpers

def is_safe_url(value: object) -> bool:
    """True for an absolute ``http(s)`` URL without whitespace or control
    characters. Stored experiment URLs are later shown as links, so anything
    else (``javascript:``, ``data:``, relative paths) is refused."""
    if not isinstance(value, str) or not value or len(value) > MAX_URL_TEXT:
        return False
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _has_control(text: str, allow_newlines: bool) -> bool:
    for ch in text:
        code = ord(ch)
        if allow_newlines and ch in "\n\r\t":
            continue
        if code < 0x20 or code == 0x7F:
            return True
    return False


class _Params:
    """Reads documented keys from the params object and rejects the rest."""

    def __init__(self, raw: dict[str, Any], op: str, request_id: str | int):
        self.raw = raw
        self.op = op
        self.request_id = request_id
        self.seen: set[str] = set()

    def fail(self, message: str) -> ProtocolError:
        return ProtocolError(f"{self.op}: {message}", request_id=self.request_id)

    def text(self, name: str, *, required: bool, max_len: int, multiline: bool = False,
             blank_ok: bool = False) -> str | None:
        self.seen.add(name)
        value = self.raw.get(name)
        if value is None:
            if required:
                raise self.fail(f"'{name}' is required.")
            return None
        if not isinstance(value, str):
            raise self.fail(f"'{name}' must be text.")
        if len(value) > max_len:
            raise self.fail(f"'{name}' is too long (limit {max_len} characters).")
        if _has_control(value, allow_newlines=multiline):
            raise self.fail(f"'{name}' contains control characters.")
        if not value.strip():
            if blank_ok and not required:
                return None
            raise self.fail(f"'{name}' is blank.")
        return value

    def structure(self) -> dict[str, str | None]:
        molfile = self.text("molfile", required=False, max_len=MAX_STRUCTURE_CHARS, multiline=True)
        smiles = self.text("smiles", required=False, max_len=MAX_STRUCTURE_CHARS)
        if (molfile is None) == (smiles is None):
            raise self.fail("give exactly one structure: 'molfile' or 'smiles'.")
        return {"molfile": molfile, "smiles": smiles.strip() if smiles is not None else None}

    def product_index(self, *, required: bool) -> int | None:
        self.seen.add("product_index")
        value = self.raw.get("product_index")
        if value is None:
            if required:
                raise self.fail("'product_index' is required.")
            return None
        if isinstance(value, bool) or not isinstance(value, int) \
                or not 1 <= value <= MAX_PRODUCT_INDEX:
            raise self.fail(f"'product_index' must be a whole number from 1 to {MAX_PRODUCT_INDEX}.")
        return value

    def whole_number(self, name: str, *, default: int, minimum: int, maximum: int) -> int:
        self.seen.add(name)
        value = self.raw.get(name)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) \
                or not minimum <= value <= maximum:
            raise self.fail(f"'{name}' must be a whole number from {minimum} to {maximum}.")
        return value

    def flag(self, name: str, *, default: bool) -> bool:
        self.seen.add(name)
        value = self.raw.get(name)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise self.fail(f"'{name}' must be true or false.")
        return value

    def percent(self, name: str) -> float | None:
        """A required key holding a yield in percent, or null."""
        self.seen.add(name)
        if name not in self.raw:
            raise self.fail(f"'{name}' is required (a number of percent, or null for none).")
        value = self.raw[name]
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise self.fail(f"'{name}' must be a number of percent or null.")
        number = float(value)
        if number != number or not 0 <= number <= MAX_YIELD_PERCENT:   # NaN, infinity, range
            raise self.fail(f"'{name}' must be from 0 to {MAX_YIELD_PERCENT} percent.")
        return round(number, 2)

    def choice(self, name: str, allowed: tuple[str, ...], *, default: str) -> str:
        value = self.text(name, required=False, max_len=MAX_ID_TEXT)
        if value is None:
            return default
        if value not in allowed:
            raise self.fail(f"'{name}' must be one of {', '.join(allowed)}.")
        return value

    def confirm(self) -> dict[str, str]:
        self.seen.add("confirm")
        value = self.raw.get("confirm")
        if not isinstance(value, dict) or set(value) != set(CONFIRM_KEYS):
            raise self.fail("'confirm' must be {\"inchikey\": ..., \"stereo_mode\": ..., "
                            "\"key_digest\": ...} from the analysis the user confirmed.")
        for key in CONFIRM_KEYS:
            item = value[key]
            if not isinstance(item, str) or not item or len(item) > MAX_REF_TEXT:
                raise self.fail(f"'confirm.{key}' must be non-empty text.")
        return {key: value[key] for key in CONFIRM_KEYS}

    def ignored_list(self) -> list[str]:
        self.seen.add("accept_ignored_stereo")
        value = self.raw.get("accept_ignored_stereo")
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > MAX_IGNORED_ITEMS:
            raise self.fail("'accept_ignored_stereo' must be a list of texts.")
        for item in value:
            if not isinstance(item, str) or len(item) > MAX_IGNORED_ITEM_TEXT:
                raise self.fail("'accept_ignored_stereo' must be a list of texts.")
        return list(value)

    def done(self) -> None:
        unknown = sorted(set(self.raw) - self.seen)
        if unknown:
            raise self.fail(f"unknown parameter(s): {', '.join(unknown)}.")


def _params_ping(p: _Params) -> dict[str, Any]:
    return {}


def _params_analyze(p: _Params) -> dict[str, Any]:
    out: dict[str, Any] = p.structure()
    out["mode"] = p.text("mode", required=False, max_len=MAX_ID_TEXT)
    out["experiment_ref"] = p.text("experiment_ref", required=False, max_len=MAX_REF_TEXT,
                                   blank_ok=True)
    out["product_index"] = p.product_index(required=False)
    return out


def _params_register(p: _Params) -> dict[str, Any]:
    out: dict[str, Any] = p.structure()
    out["mode"] = p.text("mode", required=False, max_len=MAX_ID_TEXT)
    out["experiment_ref"] = p.text("experiment_ref", required=True, max_len=MAX_REF_TEXT)
    out["experiment_code"] = p.text("experiment_code", required=False, max_len=MAX_REF_TEXT,
                                    blank_ok=True)
    url = p.text("experiment_url", required=False, max_len=MAX_URL_TEXT, blank_ok=True)
    if url is not None and not is_safe_url(url):
        raise p.fail("'experiment_url' must be an absolute http(s) URL.")
    out["experiment_url"] = url
    out["product_index"] = p.product_index(required=True)
    out["confirm"] = p.confirm()
    out["accept_ignored_stereo"] = p.ignored_list()
    return out


def _params_find(p: _Params) -> dict[str, Any]:
    return {"id": p.text("id", required=True, max_len=MAX_REF_TEXT)}


def _params_compounds(p: _Params) -> dict[str, Any]:
    text = p.text("text", required=False, max_len=MAX_SEARCH_TEXT, blank_ok=True)
    molfile = p.text("molfile", required=False, max_len=MAX_STRUCTURE_CHARS, multiline=True,
                     blank_ok=True)
    smiles = p.text("smiles", required=False, max_len=MAX_STRUCTURE_CHARS, blank_ok=True)
    if molfile is not None and smiles is not None:
        raise p.fail("give at most one query structure: 'molfile' or 'smiles'.")
    return {
        "text": text.strip() if text is not None else None,
        "molfile": molfile,
        "smiles": smiles.strip() if smiles is not None else None,
        "match": p.choice("match", MATCH_MODES, default="substructure"),
        "order": p.choice("order", ORDERS, default="asc"),
        "offset": p.whole_number("offset", default=0, minimum=0, maximum=MAX_OFFSET),
        "limit": p.whole_number("limit", default=DEFAULT_PAGE_SIZE, minimum=1,
                                maximum=MAX_PAGE_SIZE),
    }


def _params_set_name(p: _Params) -> dict[str, Any]:
    core_stereo_id = p.text("core_stereo_id", required=True, max_len=MAX_REF_TEXT)
    if "name" not in p.raw:
        raise p.fail("'name' is required (text, or null to remove the stored name).")
    # Blank text is refused rather than read as "remove": removing needs an explicit null.
    name = p.text("name", required=False, max_len=MAX_NAME_TEXT)
    return {"core_stereo_id": core_stereo_id.strip(),
            "name": name.strip() if name is not None else None}


def _params_generate_name(p: _Params) -> dict[str, Any]:
    core_stereo_id = p.text("core_stereo_id", required=True, max_len=MAX_REF_TEXT)
    return {"core_stereo_id": core_stereo_id.strip(),
            "replace": p.flag("replace", default=False)}


def _params_set_yield(p: _Params) -> dict[str, Any]:
    return {
        "id": p.text("id", required=True, max_len=MAX_REF_TEXT).strip(),
        "experiment_ref": p.text("experiment_ref", required=True, max_len=MAX_REF_TEXT).strip(),
        "product_index": p.product_index(required=True),
        "isolated": p.percent("isolated"),
        "analytical": p.percent("analytical"),
    }


def _params_yields(p: _Params) -> dict[str, Any]:
    text = p.text("text", required=False, max_len=MAX_SEARCH_TEXT, blank_ok=True)
    return {
        "text": text.strip() if text is not None else None,
        "only_recorded": p.flag("only_recorded", default=False),
        "order": p.choice("order", ORDERS, default="asc"),
        "offset": p.whole_number("offset", default=0, minimum=0, maximum=MAX_OFFSET),
        "limit": p.whole_number("limit", default=DEFAULT_YIELD_PAGE_SIZE, minimum=1,
                                maximum=MAX_YIELD_PAGE_SIZE),
    }


def _params_compound_files(p: _Params) -> dict[str, Any]:
    return {"core_stereo_id": p.text("core_stereo_id", required=True, max_len=MAX_REF_TEXT).strip()}


def _params_read_pdf(p: _Params) -> dict[str, Any]:
    core_stereo_id = p.text("core_stereo_id", required=True, max_len=MAX_REF_TEXT).strip()
    name = p.text("name", required=False, max_len=MAX_FILE_NAME)
    full_id = p.text("full_id", required=False, max_len=MAX_REF_TEXT)
    spectrum_key = p.text("spectrum_key", required=False, max_len=MAX_SPECTRUM_KEY)
    if (full_id is None) != (spectrum_key is None):
        raise p.fail("'full_id' and 'spectrum_key' go together.")
    if (name is None) == (full_id is None):
        raise p.fail("give exactly one of 'name' or 'full_id' with 'spectrum_key'.")
    return {
        "core_stereo_id": core_stereo_id,
        "name": name.strip() if name is not None else None,
        "full_id": full_id.strip() if full_id is not None else None,
        "spectrum_key": spectrum_key.strip() if spectrum_key is not None else None,
        "offset": p.whole_number("offset", default=0, minimum=0, maximum=MAX_PDF_BYTES),
    }


class _ReactionReader:
    """Validates the ``reaction`` object of ``set_reaction`` (see "Reactions")
    and returns a clean copy with every documented key present."""

    def __init__(self, p: _Params):
        self.p = p

    def fail(self, where: str, message: str) -> ProtocolError:
        return self.p.fail(f"'reaction{where}' {message}")

    def obj(self, value: object, where: str, keys: tuple[str, ...]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise self.fail(where, "must be an object.")
        unknown = sorted(str(k) for k in set(value) - set(keys))
        if unknown:
            raise self.fail(where, f"has unknown key(s): {', '.join(unknown)}.")
        return value

    def text(self, value: object, where: str, *, multiline: bool = False,
             max_len: int = MAX_REACTION_TEXT) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise self.fail(where, "must be text or null.")
        if len(value) > max_len:
            raise self.fail(where, f"is too long (limit {max_len} characters).")
        if _has_control(value, allow_newlines=multiline):
            raise self.fail(where, "contains control characters.")
        if not value.strip():
            return None
        return value if multiline else value.strip()

    def number(self, value: object, where: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise self.fail(where, "must be a number or null.")
        number = float(value)
        if number != number or not 0 <= number <= MAX_REACTION_NUMBER:
            raise self.fail(where, "must be a finite number that is not negative.")
        return number

    def items(self, value: object, where: str) -> list[Any]:
        if not isinstance(value, list):
            raise self.fail(where, "must be a list.")
        if len(value) > MAX_REACTION_ROWS:
            raise self.fail(where, f"has more than {MAX_REACTION_ROWS} items.")
        return value

    def amounts(self, value: object, where: str) -> dict[str, Any] | None:
        if value is None:
            return None
        raw = self.obj(value, where, AMOUNT_KEYS)
        out: dict[str, Any] = {}
        for key in AMOUNT_KEYS:
            item = raw.get(key)
            out[key] = (self.text(item, f"{where}.{key}") if key.endswith("_unit")
                        else self.number(item, f"{where}.{key}"))
        return out

    def component(self, value: object, where: str) -> dict[str, Any]:
        raw = self.obj(value, where, COMPONENT_KEYS)
        row = raw.get("row")
        if isinstance(row, bool) or not isinstance(row, int) or not 1 <= row <= MAX_PRODUCT_INDEX:
            raise self.fail(f"{where}.row", f"must be a whole number from 1 to {MAX_PRODUCT_INDEX}.")
        out: dict[str, Any] = {"row": row}
        for key in ("name", "formula", "cas", "source_experiment_ref", "source_experiment_code"):
            out[key] = self.text(raw.get(key), f"{where}.{key}")
        out["molfile"] = self.text(raw.get("molfile"), f"{where}.molfile", multiline=True,
                                   max_len=MAX_STRUCTURE_CHARS)
        out["actual"] = self.amounts(raw.get("actual"), f"{where}.actual")
        out["planned"] = self.amounts(raw.get("planned"), f"{where}.planned")
        return out

    def solvent(self, value: object, where: str) -> dict[str, Any]:
        raw = self.obj(value, where, SOLVENT_KEYS)
        name = self.text(raw.get("name"), f"{where}.name")
        if name is None:
            raise self.fail(f"{where}.name", "is required.")
        return {"name": name, "volume": self.number(raw.get("volume"), f"{where}.volume"),
                "unit": self.text(raw.get("unit"), f"{where}.unit")}

    def read(self, value: object) -> dict[str, Any]:
        raw = self.obj(value, "", REACTION_KEYS)
        for key in ("reactants", "reagents", "solvents"):
            if key not in raw:
                raise self.fail(f".{key}", "is required (a list, possibly empty).")
        out: dict[str, Any] = {
            "reactants": [self.component(v, f".reactants[{i}]")
                          for i, v in enumerate(self.items(raw["reactants"], ".reactants"))],
            "reagents": [self.component(v, f".reagents[{i}]")
                         for i, v in enumerate(self.items(raw["reagents"], ".reagents"))],
            "solvents": [self.solvent(v, f".solvents[{i}]")
                         for i, v in enumerate(self.items(raw["solvents"], ".solvents"))],
            "temperature": self.text(raw.get("temperature"), ".temperature"),
        }
        ref = raw.get("reference_amount")
        if ref is None:
            out["reference_amount"] = None
        else:
            r = self.obj(ref, ".reference_amount", ("value", "unit"))
            amount = self.number(r.get("value"), ".reference_amount.value")
            out["reference_amount"] = (None if amount is None else
                                       {"value": amount,
                                        "unit": self.text(r.get("unit"), ".reference_amount.unit")})
        shown = raw.get("amounts_shown")
        if shown is not None and shown not in AMOUNTS_SHOWN:
            raise self.fail(".amounts_shown", f"must be one of {', '.join(AMOUNTS_SHOWN)} or null.")
        out["amounts_shown"] = shown
        return out


def _params_set_reaction(p: _Params) -> dict[str, Any]:
    out: dict[str, Any] = {
        "experiment_ref": p.text("experiment_ref", required=True, max_len=MAX_REF_TEXT).strip(),
    }
    code = p.text("experiment_code", required=False, max_len=MAX_REF_TEXT, blank_ok=True)
    out["experiment_code"] = code.strip() if code is not None else None
    url = p.text("experiment_url", required=False, max_len=MAX_URL_TEXT, blank_ok=True)
    if url is not None and not is_safe_url(url):
        raise p.fail("'experiment_url' must be an absolute http(s) URL.")
    out["experiment_url"] = url
    p.seen.add("reaction")
    if "reaction" not in p.raw:
        raise p.fail("'reaction' is required.")
    out["reaction"] = _ReactionReader(p).read(p.raw["reaction"])
    return out


def _params_set_limiting(p: _Params) -> dict[str, Any]:
    experiment_ref = p.text("experiment_ref", required=True, max_len=MAX_REF_TEXT).strip()
    p.seen.add("row")
    if "row" not in p.raw:
        raise p.fail("'row' is required (a reactant row number, or null for automatic).")
    row = p.raw["row"]
    if row is not None and (isinstance(row, bool) or not isinstance(row, int)
                            or not 1 <= row <= MAX_PRODUCT_INDEX):
        raise p.fail(f"'row' must be a whole number from 1 to {MAX_PRODUCT_INDEX}, or null.")
    return {"experiment_ref": experiment_ref, "row": row}


def _params_route(p: _Params) -> dict[str, Any]:
    return {"core_stereo_id": p.text("core_stereo_id", required=True, max_len=MAX_REF_TEXT).strip(),
            "open": p.flag("open", default=True)}


def _params_retire(p: _Params) -> dict[str, Any]:
    return {
        "id": p.text("id", required=True, max_len=MAX_REF_TEXT),
        "reason": p.text("reason", required=True, max_len=MAX_REASON_TEXT, multiline=True),
    }


_PARAM_READERS = {
    "ping": _params_ping,
    "analyze": _params_analyze,
    "register": _params_register,
    "retire": _params_retire,
    "find": _params_find,
    "compounds": _params_compounds,
    "set_name": _params_set_name,
    "generate_name": _params_generate_name,
    "set_yield": _params_set_yield,
    "yields": _params_yields,
    "compound_files": _params_compound_files,
    "read_pdf": _params_read_pdf,
    "set_reaction": _params_set_reaction,
    "route": _params_route,
    "set_limiting": _params_set_limiting,
}


def _request_id(message: object) -> str | int | None:
    """The request id if it is usable, else None (never raises)."""
    if not isinstance(message, dict):
        return None
    value = message.get("id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 2**53:
        return value
    if isinstance(value, str) and 1 <= len(value) <= MAX_ID_TEXT \
            and not _has_control(value, allow_newlines=False):
        return value
    return None


def parse_request(message: object) -> Request:
    """Validate a decoded JSON message. Raises :class:`ProtocolError` (with
    ``request_id`` set whenever the id was readable)."""
    request_id = _request_id(message)
    if not isinstance(message, dict):
        raise ProtocolError("A request must be a JSON object.")
    if request_id is None:
        raise ProtocolError(f"'id' must be text of 1..{MAX_ID_TEXT} characters or a "
                            "non-negative whole number.")
    unknown = sorted(set(message) - {"id", "v", "op", "params"})
    if unknown:
        raise ProtocolError(f"Unknown request field(s): {', '.join(map(str, unknown))}.",
                            request_id=request_id)
    op = message.get("op")
    if not isinstance(op, str) or op not in OPS:
        shown = op if isinstance(op, str) and len(op) <= MAX_ID_TEXT else "?"
        raise ProtocolError(f"Unknown operation {shown!r}; known: {', '.join(OPS)}.",
                            code="unknown_op", request_id=request_id)
    version = message.get("v")
    if op != "ping" and (isinstance(version, bool) or version != PROTOCOL_VERSION):
        raise ProtocolError(
            f"The extension speaks protocol version {version!r}, the host version "
            f"{PROTOCOL_VERSION}. Rebuild and reload the extension, or re-run the installer, "
            "so that both come from the same version of the project.",
            code="protocol_mismatch", request_id=request_id)
    raw = message.get("params", {})
    if not isinstance(raw, dict):
        raise ProtocolError(f"{op}: 'params' must be an object.", request_id=request_id)
    reader = _Params(raw, op, request_id)
    params = _PARAM_READERS[op](reader)
    reader.done()
    return Request(id=request_id, op=op, params=params)


def ok_response(request_id: str | int | None, result: dict[str, Any]) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result}


def error_response(request_id: str | int | None, error: dict[str, Any]) -> dict[str, Any]:
    """``error`` is ``RegistryError.to_dict()`` or ``{"code", "message"}``."""
    return {"id": request_id, "ok": False, "error": error}
