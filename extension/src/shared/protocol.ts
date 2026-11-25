/**
 * Native-messaging protocol between the extension and the Python host.
 *
 * CONTRACT: this file mirrors `src/compound_registry/protocol.py`. Change both
 * together. Field names are the host's (snake_case) and are used unchanged.
 *
 * Request:  {id, v, op, params}
 * Response: {id, ok: true, result} | {id, ok: false, error: {code, message, ...}}
 */

/**
 * Changes only when a host and an extension of different protocol versions can
 * no longer work together (an operation, field or meaning removed or changed).
 * Additions keep it: a new operation or a new optional field does not change
 * the number. Keep in step with protocol.py.
 */
export const PROTOCOL_VERSION = 1;

/** Argument of `browser.runtime.connectNative`. */
export const HOST_NAME = "compound_registry";

/** `browser_specific_settings.gecko.id`; the host only accepts this add-on. */
export const EXTENSION_ID = "compound-registry@registry.local";

/**
 * The Chrome build (folder chrome-extension/, loaded unpacked): Chrome derives
 * this ID from the manifest "key" (static/chrome-key.json). The host serves
 * only this chrome-extension:// origin from Chrome, and the same registry as
 * from Firefox.
 */
export const CHROME_EXTENSION_ID = "imkbdobpgmdgbpfeencchphiegjledoj";

/** The only site the extension runs on: a build-time setting, see site.ts. */
export { SITE_ORIGIN } from "./site";

/** Keep in step with protocol.py. The host refuses larger structures. */
export const MAX_STRUCTURE_CHARS = 500_000;
export const MAX_REF_TEXT = 200;
/** `reason` of `retire`. */
export const MAX_REASON_TEXT = 2000;
export const MAX_PRODUCT_INDEX = 999;

/** Keep in step with protocol.py. */
export const MAX_SEARCH_TEXT = 2000;
export const MAX_NAME_TEXT = 1000;
export const DEFAULT_PAGE_SIZE = 24;
export const MAX_PAGE_SIZE = 50;
/**
 * The host gives the local naming program NAME_GENERATION_TIMEOUT_S (20 s in
 * protocol.py) for one name, plus a few seconds to stop it after a time-out;
 * wait this long for `generate_name`.
 */
export const GENERATE_NAME_TIMEOUT_MS = 60_000;
/** Keep in step with protocol.py: a yield is a number of percent from 0 to this. */
export const MAX_YIELD_PERCENT = 1000;
export const DEFAULT_YIELD_PAGE_SIZE = 200;
export const MAX_YIELD_PAGE_SIZE = 500;
/** Keep in step with protocol.py: `read_pdf` sends at most this many bytes per answer. */
export const PDF_CHUNK_BYTES = 600_000;
/** Larger PDFs are not served by the host. */
export const MAX_PDF_BYTES = 100 * 1024 * 1024;
export const MAX_FOLDER_FILES = 200;
export const MAX_FILE_NAME = 255;
/** Keep in step with protocol.py: limits of `set_reaction`'s `reaction`. */
export const MAX_REACTION_ROWS = 50;
export const MAX_REACTION_TEXT = 500;
export const MAX_REACTION_NUMBER = 1e12;
/** `route`: the document text is returned only up to this size. */
export const MAX_ROUTE_CDXML_CHARS = 700_000;

/**
 * Keep in step with protocol.py ("Capture reaction"). `recognize`: `image` is
 * base64 of a PNG of at most this many characters, so that the whole request
 * stays under the host's 1 MiB frame limit.
 */
export const MAX_IMAGE_BASE64_CHARS = 900_000;
/** `recognize`: a picture must be at least / at most this many pixels wide and high. */
export const MIN_IMAGE_SIDE = 16;
export const MAX_IMAGE_SIDE = 8000;
/** `recognize` returns at most this many molecules. */
export const MAX_RECOGNITION_MOLECULES = 60;
/** `describe_structures`: at most this many structures per request. */
export const MAX_DESCRIBE_STRUCTURES = 50;
/** `reaction_to_chemdraw`: limits of `steps`. */
export const MAX_CHEMDRAW_STEPS = 12;
/** Per list of a step: reactants, agents, products. */
export const MAX_CHEMDRAW_STEP_MOLECULES = 10;
/** Per step; each line at most MAX_REACTION_TEXT characters. */
export const MAX_CHEMDRAW_CONDITION_LINES = 20;
/**
 * The host answers `recognize` within RECOGNIZE_TIMEOUT_S (270 s in
 * protocol.py; loading the models can take minutes the first time); wait this
 * long for `recognize`.
 */
export const RECOGNIZE_TIMEOUT_MS = 300_000;

export type Op =
  | "ping"
  | "analyze"
  | "register"
  /** Served by the host, but no part of the extension may send it: IDs are retired with the command-line tool only. */
  | "retire"
  | "find"
  | "compounds"
  | "set_name"
  | "generate_name"
  | "set_yield"
  | "yields"
  | "compound_files"
  | "read_pdf"
  | "set_reaction"
  | "route"
  | "set_limiting"
  | "recognition_status"
  | "recognize"
  | "describe_structures"
  | "reaction_to_chemdraw";

/** Modes the user may choose. */
export type ModeOverride = "abs" | "rac" | "rel" | "undefined";
export const MODE_OVERRIDES: readonly ModeOverride[] = ["abs", "rac", "rel", "undefined"];

/** Modes the registry can store / report. */
export type StereoMode = "none" | "abs" | "rac" | "rel" | "undefined" | "partial" | "mixed";

/** Exactly one of the two. */
export type StructureInput =
  | { molfile: string; smiles?: undefined }
  | { smiles: string; molfile?: undefined };

// ------------------------------------------------------------------ params

export type PingParams = Record<string, never>;

export type AnalyzeParams = StructureInput & {
  /** null / absent = as detected from the file (or the configured default). */
  mode?: ModeOverride | null;
  experiment_ref?: string | null;
  product_index?: number | null;
};

export type RegisterParams = StructureInput & {
  mode?: ModeOverride | null;
  experiment_ref: string;
  experiment_code?: string | null;
  experiment_url?: string | null;
  product_index: number;
  /**
   * Copied from `structure` of the analysis the user looked at. The host
   * re-analyses and issues nothing if any value differs. `key_digest` is the
   * decisive one (it covers exactly the keys that select the CORE_STEREO).
   */
  confirm: { inchikey: string; stereo_mode: string; key_digest: string };
  /** The exact `structure.ignored_stereo` texts the user accepted. */
  accept_ignored_stereo?: string[];
};

export interface RetireParams {
  id: string;
  reason: string;
}

/**
 * Search by ID (read-only): every batch, and so every experiment, of the
 * compound the ID names. `id` is a full ID ("000042_02_003": the compound that
 * batch belongs to), a CORE_STEREO ("000042_02") or a CORE number ("000042" or
 * "42": every stereo variant of that constitution).
 */
export interface FindParams {
  id: string;
}

/** How a drawn query structure is compared. */
export type MatchMode = "substructure" | "exact";

/**
 * The registered compounds, one entry per CORE, optionally filtered, one page
 * at a time (read-only). A CORE is listed when it matches `text` (if given)
 * AND the query structure (if given). The exact matching rules are in
 * protocol.py under "Compound search".
 */
export interface CompoundsParams {
  /** One line: an ID, a SMILES, an InChI, an InChIKey, part of a name, a formula or an ELN number. */
  text?: string | null;
  /** The drawn query structure: at most one of `molfile` / `smiles`. */
  molfile?: string | null;
  smiles?: string | null;
  /** Default "substructure". Only used with a query structure. */
  match?: MatchMode | null;
  /** By CORE number. Default "asc". */
  order?: "asc" | "desc" | null;
  /** Default 0. */
  offset?: number | null;
  /** 1..MAX_PAGE_SIZE, default DEFAULT_PAGE_SIZE. */
  limit?: number | null;
}

/** Store (or, with `name: null`, remove) the name of one stereo variant. */
export interface SetNameParams {
  core_stereo_id: string;
  /** One line of text, at most MAX_NAME_TEXT characters; null removes the stored name. */
  name: string | null;
}

/**
 * Ask the host to get the name of one stereo variant from the naming program
 * installed on the same PC (ChemDraw) and store it. Nothing leaves the PC.
 */
export interface GenerateNameParams {
  core_stereo_id: string;
  /** Default false: an already stored name is kept and nothing is generated. */
  replace?: boolean | null;
}

/**
 * Store the yields the ELN shows for one batch (one product of one
 * experiment). The registry mirrors the ELN: both values are always sent, null
 * meaning "none shown"; both null removes the stored yield. `experiment_ref`
 * and `product_index` say where the values were read; the host refuses
 * (`yield_mismatch`) unless the batch was registered for exactly that place.
 */
export interface SetYieldParams {
  /** The full ID. */
  id: string;
  experiment_ref: string;
  product_index: number;
  /** Percent, 0..MAX_YIELD_PERCENT, or null. */
  isolated: number | null;
  analytical: number | null;
}

/**
 * The batches of the registry, one row per batch, with experiment and yield
 * (read-only), ordered by ELN number, then product number.
 */
export interface YieldsParams {
  /** Keeps rows whose ELN number, full ID or name contains it (ignoring case). */
  text?: string | null;
  /** Default false; true lists only batches that have a yield. */
  only_recorded?: boolean | null;
  /** Default "asc". */
  order?: "asc" | "desc" | null;
  /** Default 0. */
  offset?: number | null;
  /** 1..MAX_YIELD_PAGE_SIZE, default DEFAULT_YIELD_PAGE_SIZE. */
  limit?: number | null;
}

/**
 * The folder of one stereo variant on the registry's PC, the spectra recorded
 * for its batches and the files in the folder (read-only).
 */
export interface CompoundFilesParams {
  core_stereo_id: string;
}

/**
 * One piece of a PDF that belongs to a stereo variant (read-only): either a
 * file lying directly in its folder (`name`: a plain file name, never a path)
 * or the PDF recorded for one spectrum (`full_id` + `spectrum_key`). The page
 * never sends a path. Ask again with `offset` advanced by the bytes received
 * until `eof`; if `size` changes between answers, start again.
 */
export type ReadPdfParams = {
  core_stereo_id: string;
  /** Default 0. */
  offset?: number | null;
} & (
  | { name: string; full_id?: undefined; spectrum_key?: undefined }
  | { full_id: string; spectrum_key: string; name?: undefined }
);

/** Amounts of one reactant or reagent row, as numbers (units as the grid shows them). */
export interface ReactionAmounts {
  eq: number | null;
  /** Amount of substance, e.g. 0.701 with n_unit "mmol". */
  n: number | null;
  n_unit: string | null;
  /** Mass, e.g. 100 with m_unit "mg". */
  m: number | null;
  m_unit: string | null;
  /** Volume, e.g. 1.31 with v_unit "ml". */
  v: number | null;
  v_unit: string | null;
}

/** One row of the reactants or reagents grid. */
export interface ReactionComponent {
  /** 1-based position in its grid. */
  row: number;
  /** Substance name as shown, without container text; "ABC-001" when the row is a product of that experiment. */
  name: string | null;
  formula: string | null;
  cas: string | null;
  /** The experiment the ELN says this row was taken from (its own link): internal key and ELN number. */
  source_experiment_ref: string | null;
  source_experiment_code: string | null;
  /** The row's structure; null when none is drawn or it could not be read. */
  molfile: string | null;
  /** "Actual amounts" values. */
  actual: ReactionAmounts | null;
  /** "Planned amounts" values. */
  planned: ReactionAmounts | null;
}

export interface ReactionSolvent {
  name: string;
  volume: number | null;
  unit: string | null;
}

/** What an experiment page shows about its reaction (see protocol.py "Reactions"). */
export interface ReactionData {
  reactants: ReactionComponent[];
  reagents: ReactionComponent[];
  solvents: ReactionSolvent[];
  /** The "T (°C)" field as shown, e.g. "40", "rt"; null when empty. */
  temperature: string | null;
  /** E.g. { value: 0.701, unit: "mmol" }. */
  reference_amount: { value: number; unit: string | null } | null;
  /** Which amounts the page displayed. */
  amounts_shown: "actual" | "planned" | null;
}

/**
 * Store what the experiment page shows about its reaction. Sent right after
 * "Create ID" and whenever an experiment with a registered product is shown
 * saved (read-only) and differs from what was last sent. `not_found` when
 * the registry has no batch made in that experiment.
 */
export interface SetReactionParams {
  experiment_ref: string;
  experiment_code?: string | null;
  experiment_url?: string | null;
  reaction: ReactionData;
}

/**
 * Draw how a stereo variant was made as a ChemDraw file in its folder, and
 * (default) open it with ChemDraw.
 */
export interface RouteParams {
  core_stereo_id: string;
  /** Default true. */
  open?: boolean | null;
}

/**
 * Choose the reactant row that is the starting material ("SM") of an
 * experiment's run line in routes; null = automatic again. `not_found` when no
 * reaction is stored for the experiment; `invalid_argument` when it has no
 * reactant in that row.
 */
export interface SetLimitingParams {
  experiment_ref: string;
  row: number | null;
}

// "Capture reaction" (see protocol.py, "Capture reaction"). Keep the lists in step with protocol.py.

/** `mode` of `recognize`: a reaction scheme, or a picture of one molecule. */
export const RECOGNITION_MODES = ["reaction", "molecule"] as const;
export type RecognitionMode = (typeof RECOGNITION_MODES)[number];

/**
 * `state` of `recognition_status`: not_installed; stopped (installed, no worker
 * running); loading (worker started, models loading); ready; failed (the
 * worker could not be started or died while loading).
 */
export const RECOGNITION_STATES = ["not_installed", "stopped", "loading", "ready", "failed"] as const;
export type RecognitionState = (typeof RECOGNITION_STATES)[number];

/**
 * `warnings` of a molecule, computed by the host with the registry's RDKit:
 *   unparsable             the structure could not be read (`smiles` null)
 *   placeholder            contains a dummy atom / R group
 *   isotope                contains an isotope label (often a misread)
 *   unusual_element        contains an element that is rare in organic synthesis (often a misread)
 *   stereo                 has a stereocentre (with or without a wedge) or a double bond that is or could be E/Z: check the wedges and the geometry
 *   charged_or_salt        has charges or several fragments: check the counter-ion
 *   low_confidence         the model's confidence is low (`recognize` only)
 *   duplicate              the same structure (the same `smiles`) appears more than once in the picture (`recognize` only)
 *   smiles_molfile_differ  the model's SMILES and molfile describe different structures; an abbreviation the SMILES has expanded does not count (`recognize` only)
 */
export const MOLECULE_WARNING_CODES = [
  "unparsable", "placeholder", "isotope", "unusual_element", "stereo", "charged_or_salt", "low_confidence", "duplicate",
  "smiles_molfile_differ",
] as const;
export type MoleculeWarning = (typeof MOLECULE_WARNING_CODES)[number];

/** The molecule warnings `describe_structures` can report: those that can be told from one structure alone. */
export const STRUCTURE_WARNING_CODES = [
  "unparsable", "placeholder", "isotope", "unusual_element", "stereo", "charged_or_salt",
] as const satisfies readonly MoleculeWarning[];
export type StructureWarning = (typeof STRUCTURE_WARNING_CODES)[number];

/**
 * `warnings` of a `recognize` result:
 *   no_molecules          no molecule was found in the picture
 *   no_reaction           mode "reaction": molecules were found but no reaction
 *   unassigned_molecules  mode "reaction": a reaction was found, and some molecules are in none (a step may have been missed)
 *   truncated             more than MAX_RECOGNITION_MOLECULES molecules were found; the rest was left out
 */
export const RECOGNITION_WARNING_CODES = ["no_molecules", "no_reaction", "unassigned_molecules", "truncated"] as const;
export type RecognitionWarning = (typeof RECOGNITION_WARNING_CODES)[number];

/**
 * Whether the optional recognition part is installed and whether its worker
 * is running. With `start: true` the host starts the worker when it is
 * installed and answers at once; the models load in the background. Needs no
 * registry, like every operation of CAPTURE_OPS.
 */
export interface RecognitionStatusParams {
  /** Default false. */
  start?: boolean | null;
}

/** Read a drawn reaction scheme (or one molecule) from a picture. Everything runs on this PC. */
export interface RecognizeParams {
  /** Base64 of a PNG (no "data:" prefix), at most MAX_IMAGE_BASE64_CHARS characters. */
  image: string;
  mode: RecognitionMode;
}

/**
 * The canonical SMILES and the warnings of structures the user drew or
 * edited, from the registry's RDKit. One answer per structure, in order.
 */
export interface DescribeStructuresParams {
  /** 1 to MAX_DESCRIBE_STRUCTURES. */
  structures: StructureInput[];
}

/** One reaction step to draw. A step needs at least one reactant or product. */
export interface ChemdrawStep {
  /** Molfiles, at most MAX_CHEMDRAW_STEP_MOLECULES per list. */
  reactants: string[];
  /** Drawn over the arrow. Default none. */
  agents?: string[] | null;
  products: string[];
  /** Lines written at the arrow: one line of text each, at most MAX_REACTION_TEXT characters, at most MAX_CHEMDRAW_CONDITION_LINES. Default none. */
  conditions?: string[] | null;
}

/** Write the steps as one ChemDraw file in the user's temporary folder and open it. */
export interface ReactionToChemdrawParams {
  /** 1 to MAX_CHEMDRAW_STEPS, drawn as consecutive steps. */
  steps: ChemdrawStep[];
}

export interface ParamsByOp {
  ping: PingParams;
  analyze: AnalyzeParams;
  register: RegisterParams;
  retire: RetireParams;
  find: FindParams;
  compounds: CompoundsParams;
  set_name: SetNameParams;
  generate_name: GenerateNameParams;
  set_yield: SetYieldParams;
  yields: YieldsParams;
  compound_files: CompoundFilesParams;
  read_pdf: ReadPdfParams;
  set_reaction: SetReactionParams;
  route: RouteParams;
  set_limiting: SetLimitingParams;
  recognition_status: RecognitionStatusParams;
  recognize: RecognizeParams;
  describe_structures: DescribeStructuresParams;
  reaction_to_chemdraw: ReactionToChemdrawParams;
}

// ------------------------------------------------------------------ results

/**
 * Display-only values computed from a structure; never part of any key. All
 * fragments together (a salt counts with its counter-ion).
 */
export interface Properties {
  /** Hill formula of everything drawn, e.g. "C14H16ClN". */
  sum_formula: string;
  /** Average molar mass in g/mol, 3 decimals. */
  mol_weight: number;
  /** Monoisotopic mass, 5 decimals. */
  exact_mass: number;
  /** Mass percentages (2 decimals) in the element order of `sum_formula`. */
  composition: { element: string; percent: number }[];
}

/** Where a stored name came from: typed by the user, or generated by the local ChemDraw. */
export type NameSource = "user" | "chemdraw";

/** The name stored for one CORE_STEREO (meant for the IUPAC name). */
export interface NameInfo {
  text: string;
  /** ISO 8601 UTC. */
  updated_at: string;
  /** Absent in answers of an older host: read as "user". */
  source?: NameSource;
}

/** `ping`: whether a local naming program was found on the host's PC. */
export interface NameGeneratorInfo {
  /** Found does not mean it will work (licence). */
  available: boolean;
  /** What to call it ("ChemDraw"); null when not available or not known. */
  label: string | null;
}

/**
 * The yield stored for one batch, in percent as the ELN shows it. At least one
 * of the two values is a number (a batch without any has `yield: null`).
 */
export interface YieldInfo {
  isolated: number | null;
  analytical: number | null;
  /** ISO 8601 UTC. */
  updated_at: string;
}

export interface StructureInfo {
  /** Null when the values could not be computed. Absent in answers of an older host. */
  properties?: Properties | null;
  inchikey: string;
  /**
   * Digest of the registry keys of this structure in this stereo mode. Opaque;
   * only ever sent back unchanged in `confirm`.
   */
  key_digest: string;
  core_inchikey: string;
  formula: string;
  smiles: string;
  inchi: string;
  stereo_mode: StereoMode;
  detected_mode: StereoMode;
  mode_overridden: boolean;
  /** Chemistry warnings about this drawing. */
  warnings: string[];
  /**
   * Drawn stereo that is NOT part of the registry keys. If non-empty, a new ID
   * may only be created after the user accepted every item explicitly.
   */
  ignored_stereo: string[];
  /** Standalone SVG text (analyze only). Display it ONLY as an <img> data URL. */
  svg?: string | null;
}

export interface Batch {
  full_id: string;
  core_no: number;
  stereo_no: number;
  batch_no: number;
  core_stereo_id: string;
  experiment_ref: string;
  experiment_code: string | null;
  experiment_url: string | null;
  product_index: number;
  smiles: string;
  inchikey: string;
  stereo_mode: StereoMode;
  source: string;
  created_at: string;
  rdkit_version: string;
  inchi_version: string;
  retired_at: string | null;
  retired_reason: string | null;
  retired: boolean;
  /** The stored yield; null when none. Absent in answers of an older host. */
  yield?: YieldInfo | null;
}

export interface StereoVariant {
  core_stereo_id: string;
  stereo_no: number;
  stereo_mode: StereoMode;
  smiles: string;
  inchikey: string;
  batch_count: number;
  svg: string | null;
}

/**
 * registered_here  an active batch of this exact compound exists in this experiment (`id`)
 * known_compound   this stereo variant is registered, but not for this experiment
 * known_core       only other stereo variants of the same constitution exist
 * new              nothing with this constitution is registered
 */
export type AnalysisStatus = "registered_here" | "known_compound" | "known_core" | "new";

export interface AnalyzeResult {
  structure: StructureInfo;
  status: AnalysisStatus;
  id: string | null;
  core_stereo_id: string | null;
  core_no: number | null;
  /** The name stored for `core_stereo_id`; null when there is none or the compound is not registered. */
  name?: NameInfo | null;
  /** All batches of this exact stereo variant, oldest first, retired included. */
  batches: Batch[];
  other_stereos: StereoVariant[];
  /** Active batch of a DIFFERENT compound in (experiment_ref, product_index). */
  slot_conflict: Batch | null;
  mode_options: ModeOverride[];
  /** Registry warnings (stale database, chemistry version, sync folder, ...). */
  warnings: string[];
}

export interface BackupOutcome {
  performed: boolean;
  path: string | null;
  warning: string | null;
  removed: string[];
}

export interface RegisterResult {
  id: string;
  /** false: the ID already existed (same compound, same experiment). */
  created: boolean;
  core_created: boolean;
  stereo_created: boolean;
  batch: Batch;
  structure: StructureInfo;
  accepted_ignored_stereo: string[];
  warnings: string[];
  backup: BackupOutcome;
  /**
   * The compound's folder on the registry's PC (`created`: this call made it);
   * null when none could be made (`warnings` says why). Absent in answers of
   * an older host.
   */
  folder?: { path: string; created: boolean } | null;
}

export interface RetireResult {
  id: string;
  batch: Batch;
  warnings: string[];
  backup: BackupOutcome;
}

export type HomeSource = "host-config" | "env" | "default";

export interface PingResult {
  protocol_version: number;
  host_version: string;
  /** Where the host took the registry folder from; anything but "default" is shown to the user. */
  home_source: HomeSource;
  home: string;
  db_path: string;
  db_exists: boolean;
  id_format: string;
  default_stereo_mode: ModeOverride;
  mode_options: ModeOverride[];
  stats: Record<string, number>;
  integrity: string;
  rdkit_version: string;
  inchi_version: string;
  chemistry_fingerprint: { stored: string | null; current: string; changed: boolean };
  python: string;
  warnings: string[];
  /** Absent in answers of an older host: read as not available. */
  name_generator?: NameGeneratorInfo;
  /** The folder that holds the compound folders; null or absent when there is none. */
  files_folder?: string | null;
}

/** One stereo variant in a search result. */
export interface FoundVariant {
  core_stereo_id: string;
  stereo_no: number;
  stereo_mode: StereoMode;
  smiles: string;
  inchikey: string;
  batch_count: number;
  /** The stored name, on `variants` and `other_variants`. */
  name?: NameInfo | null;
  /** Standard InChI as drawn, on `variants` and `other_variants`. */
  inchi?: string;
  /** Only on `variants`. */
  properties?: Properties | null;
  /** Only on `variants` (not on `other_variants`). Display ONLY as an <img> data URL. */
  svg?: string | null;
  /** Only on `variants`: all batches, oldest first, retired included. */
  batches?: Batch[];
}

/** One batch of an experiment in a search result. */
export interface FoundExperimentBatch {
  full_id: string;
  core_stereo_id: string;
  product_index: number;
  created_at: string;
  retired: boolean;
  /** The stored yield; null when none. Absent in answers of an older host. */
  yield?: YieldInfo | null;
}

/** One experiment in which the compound was made. */
export interface FoundExperiment {
  /** The ELN's internal key, e.g. "4180". */
  experiment_ref: string;
  /** The ELN number, e.g. "ABC-008". */
  experiment_code: string | null;
  experiment_url: string | null;
  batches: FoundExperimentBatch[];
}

export interface FindResult {
  /** The ID as understood (trimmed). */
  query: string;
  /** What kind of ID it was. */
  level: "batch" | "stereo" | "core";
  core_no: number;
  /** The full ID that was searched, for level "batch"; else null. */
  matched_id: string | null;
  /** The stereo variants the ID denotes (one, or all of the CORE for level "core"). */
  variants: FoundVariant[];
  /** The other stereo variants of the same CORE; their experiments are NOT in `experiments`. */
  other_variants: FoundVariant[];
  /** The distinct experiments over `variants`, in order of their first batch. */
  experiments: FoundExperiment[];
  warnings: string[];
}

/** Why a CORE is in a `compounds` answer (see protocol.py, "Compound search"). */
export type MatchReason = "id" | "inchikey" | "structure" | "name" | "formula" | "experiment" | "drawn";

/** One stereo variant of a CORE in a `compounds` answer. */
export interface CompoundVariant {
  core_stereo_id: string;
  stereo_no: number;
  stereo_mode: StereoMode;
  smiles: string;
  inchi: string;
  inchikey: string;
  name: NameInfo | null;
  /** Retired batches included. */
  batch_count: number;
  /**
   * True when this variant itself matched the search (its own name, InChIKey,
   * ID, stored structure, experiment), false when only the CORE or another
   * variant did. Always true without a search.
   */
  matched: boolean;
}

export interface CompoundExperiment {
  experiment_ref: string;
  experiment_code: string | null;
  experiment_url: string | null;
}

/** One CORE (one constitution) with its stereo variants. */
export interface CompoundItem {
  core_no: number;
  /** The CORE part of the IDs as text, e.g. "000042". */
  core_id: string;
  /** As stored in the registry: per fragment, e.g. "C14H15N.ClH". */
  formula: string;
  /** Of the first stereo variant (the same for every variant of a constitution). */
  properties: Properties | null;
  /** Drawing of the first stereo variant. Display ONLY as an <img> data URL. */
  svg: string | null;
  /** By stereo number. */
  variants: CompoundVariant[];
  /** Over all variants, retired batches included. */
  batch_count: number;
  /** Distinct experiments over all variants. */
  experiment_count: number;
  /** The first experiments (at most 10), in order of their first batch. */
  experiments: CompoundExperiment[];
  /** ISO 8601 UTC of the first and the latest batch. */
  first_created_at: string;
  last_created_at: string;
  /** Empty without a search. */
  matched_by: MatchReason[];
}

export interface CompoundsResult {
  /** COREs matching the search (all COREs without one). */
  total: number;
  /** COREs in the registry. */
  registry_total: number;
  offset: number;
  limit: number;
  /** `limit` entries at most, starting at `offset`. */
  items: CompoundItem[];
  /** How the search was read, in plain words (e.g. "Read as SMILES: ..."). */
  notes: string[];
  warnings: string[];
}

export interface SetNameResult {
  core_stereo_id: string;
  /** The name now stored; null after a removal. */
  name: NameInfo | null;
  /** False when the stored name already was exactly this. */
  changed: boolean;
  warnings: string[];
}

export interface GenerateNameResult {
  core_stereo_id: string;
  /** The name stored after the call (generated now, or the one already there); null when none. */
  name: NameInfo | null;
  /** True when this call got a name from the naming program and stored it. */
  generated: boolean;
  changed: boolean;
  /** When `generated` is false: why, in plain words. Not getting a name is never an error. */
  message: string | null;
  warnings: string[];
}

export interface SetYieldResult {
  id: string;
  /** What is stored after the call; null when the batch has no yield (any more). */
  yield: YieldInfo | null;
  /** False when exactly these values were already stored. */
  changed: boolean;
  warnings: string[];
}

/** One batch in a `yields` answer. */
export interface YieldRow {
  /** The ELN's internal key, e.g. "4180". */
  experiment_ref: string;
  /** The ELN number, e.g. "ABC-006". */
  experiment_code: string | null;
  experiment_url: string | null;
  product_index: number;
  full_id: string;
  core_stereo_id: string;
  /** Text of the name stored for the stereo variant; null when none. */
  name: string | null;
  yield: YieldInfo | null;
  /** ISO 8601 UTC: when the ID was created. */
  created_at: string;
  retired: boolean;
}

export interface YieldsResult {
  /** Rows matching the request. */
  total: number;
  /** Batches in the registry. */
  registry_total: number;
  offset: number;
  limit: number;
  /** `limit` rows at most, starting at `offset`. */
  items: YieldRow[];
  warnings: string[];
}

/** One acquisition parameter of a recorded spectrum. */
export interface SpectrumParameter {
  name: string;
  value: string;
}

/** What was recorded (by the MestReNova add-on) for one spectrum of a batch. */
export interface SpectrumInfo {
  full_id: string;
  /** Names the measurement within the batch (dataset name, nucleus, dimension). */
  spectrum_key: string;
  /** ISO 8601 UTC. */
  updated_at: string;
  /** The ELN number of the batch's experiment, e.g. "ABC-006". */
  experiment_code: string | null;
  title: string | null;
  comment: string | null;
  sample: string | null;
  nucleus: string | null;
  solvent: string | null;
  experiment: string | null;
  pulse_sequence: string | null;
  frequency_mhz: number | null;
  dimensions: number | null;
  /** The multiplet report as one line of plain text. */
  report: string | null;
  parameters: SpectrumParameter[];
  /** Recorded paths on the registry's PC, as text (never a link). */
  mnova_file: string | null;
  pdf_file: string | null;
  /** True when `pdf_file` can be read with `read_pdf` right now. */
  pdf_available: boolean;
  /**
   * True for a spectrum saved with the add-on's "Crude" box: only `mnova_file`
   * is sent and shown (no details, no PDF). Missing from a host older than 0.11.
   */
  crude?: boolean;
  /**
   * True for a spectrum saved with the add-on's "Undesired side product" box:
   * like a crude one, only `mnova_file` is sent and shown. Missing from a host
   * older than 0.12.
   */
  side_product?: boolean;
}

/** One file lying directly in a compound's folder. */
export interface FolderFile {
  name: string;
  /** Bytes. */
  size: number;
  /** ISO 8601 UTC. */
  modified: string;
  is_pdf: boolean;
  /** True when it is the `mnova_file` or `pdf_file` of one of the listed spectra. */
  recorded: boolean;
}

export interface CompoundFilesResult {
  core_stereo_id: string;
  /** Null when folders cannot be used for this ID. */
  folder: { path: string; exists: boolean } | null;
  spectra: SpectrumInfo[];
  /** By name; at most MAX_FOLDER_FILES. */
  files: FolderFile[];
  files_truncated: boolean;
  warnings: string[];
}

export interface ReadPdfResult {
  /** The file's name, e.g. "EX0001_01_001_FC2.pdf". */
  name: string;
  /** Bytes of the whole file. */
  size: number;
  offset: number;
  /** Base64 of the bytes from `offset` (at most PDF_CHUNK_BYTES). */
  data: string;
  eof: boolean;
}

export interface SetReactionResult {
  experiment_ref: string;
  /** False when exactly this content was stored already. */
  changed: boolean;
  /** ISO 8601 UTC of the stored content. */
  updated_at: string;
  warnings: string[];
}

/** An experiment of the route whose reaction is not stored yet (open it once in the ELN). */
export interface RouteMissing {
  experiment_ref: string;
  experiment_code: string | null;
  experiment_url: string | null;
  /** The CORE_STEREO made there. */
  compound: string;
}

/** A reactant that can be chosen as a run's starting material. */
export interface LimitingCandidate {
  row: number;
  /** Name as stored, else its formula, else null. */
  name: string | null;
  eq: number | null;
  /** Formatted like on a run line, e.g. "1.90 g"; null when unknown. */
  mass: string | null;
}

/** One run drawn in a route, for switching its starting material. */
export interface RouteChoice {
  experiment_ref: string;
  experiment_code: string | null;
  /** The CORE_STEREO this run makes. */
  compound: string;
  /** Reactant row used as SM; null: none. */
  row: number | null;
  by: "user" | "auto" | null;
  /** What the automatic rule gives; null: none. */
  auto_row: number | null;
  /** True when two or more reactants have 1 equivalent. */
  ambiguous: boolean;
  candidates: LimitingCandidate[];
}

export interface SetLimitingResult {
  experiment_ref: string;
  row: number | null;
  /** The chosen reactant's name; null for automatic. */
  name: string | null;
  changed: boolean;
  warnings: string[];
}

export interface RouteResult {
  core_stereo_id: string;
  /** The file written (a new name each time), or null when nothing was drawn or it could not be written. */
  file: { path: string; name: string } | null;
  /** True when Windows was asked to open the file. */
  opened: boolean;
  /** Why it was not opened, or null. */
  open_message: string | null;
  /** Reaction arrows drawn. */
  steps: number;
  /** ELN numbers of the runs drawn, in drawing order. */
  experiments: string[];
  missing: RouteMissing[];
  /** Plain-language remarks (runs left out and why, ...). */
  notes: string[];
  /** One per run drawn, in drawing order (starting-material switch). */
  choices: RouteChoice[];
  /** The CDXML text for "Save a copy", or null when too large / nothing drawn. */
  cdxml: string | null;
  warnings: string[];
}

export interface RecognitionStatusResult {
  /** True when the recognition environment and the model files are in place. */
  installed: boolean;
  state: RecognitionState;
  /** Plain text for the user: how to install it, or why it failed. */
  detail?: string;
  /** How long loading took ("ready") or has taken so far ("loading"). */
  load_seconds?: number;
}

/** [x0, y0, x1, y1] in pixels of the picture as sent (whole numbers, x0 < x1, y0 < y1). */
export type PixelBox = [number, number, number, number];

/** One molecule found in the picture. A draft: wrong answers often look valid. */
export interface RecognizedMolecule {
  /** 1, 2, 3, ... in list order. */
  n: number;
  box: PixelBox;
  /** Canonical SMILES from the registry's RDKit; a placeholder (R group) is "*". Null when unreadable. */
  smiles: string | null;
  /**
   * V2000 molfile with 2D coordinates, or null: the structure of `smiles` (stereo included: wedges, and
   * the chiral flag when there is one), with its atoms where the picture has them whenever the drawing
   * could be used.
   */
  molfile: string | null;
  /** The model's own value from 0 to 1, or null. */
  confidence: number | null;
  warnings: MoleculeWarning[];
}

/** One block of text at a reaction arrow. Formulas are often misread. */
export interface RecognizedCondition {
  box: PixelBox;
  /** The text read, one item per line. */
  lines: string[];
}

/** One reaction step. The lists hold molecule numbers (`n`); a molecule may be in several steps. */
export interface RecognizedReaction {
  reactants: number[];
  /** The molecules drawn over or under the arrow. */
  agents: number[];
  products: number[];
  conditions: RecognizedCondition[];
  /**
   * "reactants>agents>products" from the molecules' SMILES, each part joined
   * with "." (agents without SMILES left out); null when a reactant or product
   * has no SMILES or the step has neither reactants nor products.
   */
  reaction_smiles: string | null;
}

export interface RecognizeResult {
  /** Pixels of the picture as received. */
  width: number;
  height: number;
  /** Every molecule found, also those in no reaction; at most MAX_RECOGNITION_MOLECULES. */
  molecules: RecognizedMolecule[];
  /** In reading order. Always empty for mode "molecule". */
  reactions: RecognizedReaction[];
  /** Numbers of the molecules that are in no reaction. */
  unassigned: number[];
  /** How long the recognition took. */
  seconds: number;
  warnings: RecognitionWarning[];
}

export interface DescribeStructuresResult {
  /** One per structure sent, in the same order. Unreadable: `{ smiles: null, warnings: ["unparsable"] }`. */
  structures: { smiles: string | null; warnings: StructureWarning[] }[];
}

export interface ReactionToChemdrawResult {
  /** The file written (in the user's temporary folder). */
  path: string;
  /** True when the PC was asked to open the file. */
  opened: boolean;
  /** Why it was not opened, in plain words. */
  message?: string;
}

export interface ResultByOp {
  ping: PingResult;
  analyze: AnalyzeResult;
  register: RegisterResult;
  retire: RetireResult;
  find: FindResult;
  compounds: CompoundsResult;
  set_name: SetNameResult;
  generate_name: GenerateNameResult;
  set_yield: SetYieldResult;
  yields: YieldsResult;
  compound_files: CompoundFilesResult;
  read_pdf: ReadPdfResult;
  set_reaction: SetReactionResult;
  route: RouteResult;
  set_limiting: SetLimitingResult;
  recognition_status: RecognitionStatusResult;
  recognize: RecognizeResult;
  describe_structures: DescribeStructuresResult;
  reaction_to_chemdraw: ReactionToChemdrawResult;
}

// ------------------------------------------------------------------ envelope

export interface HostRequest<K extends Op = Op> {
  id: string;
  v: number;
  op: K;
  params: ParamsByOp[K];
}

export interface HostError {
  code: string;
  message: string;
  /** slot_conflict only. */
  existing_id?: string;
}

/** `set_yield`: the batch was not registered for the experiment and product row the yields were read from. */
export const YIELD_MISMATCH = "yield_mismatch";
/** `read_pdf`: no such file, not a PDF, or too large. */
export const FILE_UNAVAILABLE = "file_unavailable";
/** `recognize`: the optional recognition part is not installed (the message names the command). */
export const RECOGNITION_NOT_INSTALLED = "recognition_not_installed";
/** `recognize`: the worker could not be started, died, gave no answer in time or reported a model error. */
export const RECOGNITION_FAILED = "recognition_failed";
/** `recognize`: `image` is not base64 of a PNG, or the picture has an unusable size. */
export const IMAGE_INVALID = "image_invalid";

export type HostResponse<K extends Op = Op> =
  | { id: string | number | null; ok: true; result: ResultByOp[K] }
  | { id: string | number | null; ok: false; error: HostError };

/**
 * Host error codes the panel reacts to specially (all others are only shown).
 * registry_changed: the host's registry configuration changed while it was
 * running; it refused the request and exited. Ping again before going on.
 */
export const REGISTRY_CHANGED = "registry_changed";
export const REGISTRY_MISSING = "registry_missing";

/*
 * Error codes produced on the extension side (the host never sends these):
 *   host_unavailable   the native host could not be started or the connection dropped
 *   host_timeout       no answer within the time limit
 *   host_bad_response  an answer that does not follow the protocol
 *   not_allowed        message from a context the background does not serve
 *   capture_failed     "capture-start": the active tab could not be captured
 *   capture_expired    "capture-image": no screenshot is kept under that id (any more)
 * Registry and host codes are listed in errors.py and protocol.py.
 */

// ------------------------------------------------------------------ content <-> background

/** Sent with `browser.runtime.sendMessage` from the content script. */
export interface BackgroundRequest<K extends Op = Op> {
  type: "host-request";
  op: K;
  params: ParamsByOp[K];
}

/**
 * The extension's own page with the list of all compounds (`dist/compounds.html`).
 * It is opened by the background (`browser.tabs.create`), never linked from the site.
 */
export const COMPOUNDS_PAGE = "compounds.html";

/**
 * Sent by the content script (the panel's "All compounds" link): the
 * background opens COMPOUNDS_PAGE in a new tab and answers `{ ok: true }`.
 */
export interface OpenCompoundsRequest {
  type: "open-compounds";
}

/**
 * The operations the background serves to COMPOUNDS_PAGE (the site's content
 * script keeps its own list). The page never analyses or registers.
 */
export const PAGE_OPS: readonly Op[] = [
  "ping", "find", "compounds", "set_name", "generate_name", "yields", "compound_files", "read_pdf", "route", "set_limiting",
];

/**
 * The extension's own page for "Capture reaction" (`dist/capture.html`). The
 * background opens it as `capture.html?id=<capture id>` after it took the
 * screenshot of the active tab.
 */
export const CAPTURE_PAGE = "capture.html";

/** The toolbar button's popup (`dist/popup.html`). */
export const POPUP_PAGE = "popup.html";

/**
 * The operations the background serves to CAPTURE_PAGE, and to nobody else.
 * Keep in step with CAPTURE_OPS in protocol.py: the host serves exactly these
 * without a registry.
 */
export const CAPTURE_OPS = [
  "recognition_status", "recognize", "describe_structures", "reaction_to_chemdraw",
] as const satisfies readonly Op[];
export type CaptureOp = (typeof CAPTURE_OPS)[number];

/**
 * Sent by the popup (and nothing else): the background takes a screenshot of
 * the active tab, keeps it in memory under a random id and opens CAPTURE_PAGE
 * with that id in a new tab. Answer: `{ ok: true }` or `{ ok: false, error }`
 * (code CAPTURE_FAILED when the tab cannot be captured).
 */
export interface CaptureStartRequest {
  type: "capture-start";
}

/**
 * Sent by CAPTURE_PAGE (and nothing else): the screenshot kept under `id`.
 * Answer: CaptureImageReply. The background keeps a screenshot only for a
 * short time; after that the code is CAPTURE_EXPIRED.
 */
export interface CaptureImageRequest {
  type: "capture-image";
  /** The `id` of the page's own address. */
  id: string;
}

export type CaptureImageReply =
  | { ok: true; result: { /** A "data:image/png;base64,..." URL, in device pixels. */ image: string } }
  | { ok: false; error: HostError };

/** Extension-side error codes of the two capture messages (the host never sends them). */
export const CAPTURE_FAILED = "capture_failed";
export const CAPTURE_EXPIRED = "capture_expired";

/** What the background answers: never rejects, errors are values. */
export type BackgroundReply<K extends Op = Op> =
  | { ok: true; result: ResultByOp[K] }
  | { ok: false; error: HostError };
