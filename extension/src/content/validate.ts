/**
 * Shape checks for what comes back from the background. Rendering and the
 * create gating rely on these fields (e.g. `ignored_stereo` must really be a
 * list), so an answer that does not have them is treated as `host_bad_response`.
 */

import {
  MAX_FILE_NAME,
  MAX_FOLDER_FILES,
  MAX_PDF_BYTES,
  MAX_PRODUCT_INDEX,
  MAX_ROUTE_CDXML_CHARS,
  MAX_YIELD_PERCENT,
  PDF_CHUNK_BYTES,
  type AnalysisStatus,
  type AnalyzeResult,
  type BackgroundReply,
  type Batch,
  type CompoundFilesResult,
  type CompoundExperiment,
  type CompoundItem,
  type CompoundsResult,
  type CompoundVariant,
  type FindResult,
  type FolderFile,
  type FoundExperiment,
  type FoundExperimentBatch,
  type FoundVariant,
  type GenerateNameResult,
  type HostError,
  type LimitingCandidate,
  type NameGeneratorInfo,
  type NameInfo,
  type NameSource,
  type Op,
  type PingResult,
  type Properties,
  type ReadPdfResult,
  type RegisterResult,
  type RouteChoice,
  type RouteMissing,
  type RouteResult,
  type ResultByOp,
  type SetNameResult,
  type SetLimitingResult,
  type SetReactionResult,
  type SetYieldResult,
  type SpectrumInfo,
  type SpectrumParameter,
  type StereoVariant,
  type StructureInfo,
  type YieldInfo,
  type YieldRow,
  type YieldsResult,
} from "../shared/protocol";

type Rec = Record<string, unknown>;

export function isRecord(value: unknown): value is Rec {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
const isString = (v: unknown): v is string => typeof v === "string";
const isNullableString = (v: unknown): v is string | null => v === null || typeof v === "string";
const isNumber = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isStringArray = (v: unknown): v is string[] => Array.isArray(v) && v.every(isString);

const STATUSES: readonly AnalysisStatus[] = ["registered_here", "known_compound", "known_core", "new"];

export function isHostError(value: unknown): value is HostError {
  return isRecord(value) && isString(value.code) && isString(value.message);
}

/** Display-only values (sum formula, masses, composition). */
export function isProperties(v: unknown): v is Properties {
  return (
    isRecord(v) &&
    isString(v.sum_formula) &&
    isNumber(v.mol_weight) &&
    isNumber(v.exact_mass) &&
    Array.isArray(v.composition) &&
    v.composition.every((c) => isRecord(c) && isString(c.element) && isNumber(c.percent))
  );
}

/** `source` is not checked here: read it with `nameSource`. */
export function isNameInfo(v: unknown): v is NameInfo {
  return isRecord(v) && isString(v.text) && isString(v.updated_at);
}

/** Where a stored name came from. Absent (older host) or a value this extension does not know: "user". */
export function nameSource(name: NameInfo | null | undefined): NameSource {
  return isRecord(name) && name.source === "chemdraw" ? "chemdraw" : "user";
}

export const NO_NAME_GENERATOR: Readonly<NameGeneratorInfo> = Object.freeze({ available: false, label: null });

/**
 * `ping.name_generator`: whether the host can get names from a naming program
 * on this PC. Absent (older host) or malformed: not available. A blank label is
 * read as null (the UI then says "ChemDraw").
 */
export function readNameGenerator(ping: unknown): NameGeneratorInfo {
  if (!isRecord(ping)) return NO_NAME_GENERATOR;
  const g = ping.name_generator;
  if (!isRecord(g) || typeof g.available !== "boolean" || !isNullableString(g.label ?? null)) return NO_NAME_GENERATOR;
  if (!g.available) return NO_NAME_GENERATOR;
  const label = isString(g.label) && g.label.trim() !== "" ? g.label.trim() : null;
  return { available: true, label };
}

/**
 * Additive, display-only fields: absent in answers of an older host (then the
 * UI simply shows less), but when present they must have their shape.
 */
const optional = (check: (x: unknown) => boolean) => (v: unknown): boolean => v === undefined || v === null || check(v);
const isOptionalProperties = optional(isProperties);
const isOptionalName = optional(isNameInfo);

export function isStructureInfo(v: unknown): v is StructureInfo {
  return (
    isRecord(v) &&
    isOptionalProperties(v.properties) &&
    isString(v.inchikey) &&
    v.inchikey.length > 0 &&
    isString(v.key_digest) &&
    v.key_digest.length > 0 &&
    isString(v.formula) &&
    isString(v.smiles) &&
    isString(v.stereo_mode) &&
    v.stereo_mode.length > 0 &&
    isString(v.detected_mode) &&
    typeof v.mode_overridden === "boolean" &&
    isStringArray(v.warnings) &&
    isStringArray(v.ignored_stereo) &&
    (v.svg === undefined || isNullableString(v.svg))
  );
}

/** A yield in percent as the registry stores it (0..MAX_YIELD_PERCENT), or null. */
const isPercentOrNull = (v: unknown): v is number | null => v === null || (isNumber(v) && v >= 0 && v <= MAX_YIELD_PERCENT);

/** A stored yield `{isolated, analytical, updated_at}`. */
export function isYieldInfo(v: unknown): v is YieldInfo {
  return isRecord(v) && isPercentOrNull(v.isolated) && isPercentOrNull(v.analytical) && isString(v.updated_at);
}

/**
 * The `yield` field of a batch (display and `set_yield` input): absent in
 * answers of an older host (tolerated, and then no yield is ever stored),
 * null for none, else it must have its shape.
 */
export function readYield(v: unknown): v is YieldInfo | null | undefined {
  return v === undefined || v === null || isYieldInfo(v);
}

export function isBatch(v: unknown): v is Batch {
  return (
    isRecord(v) &&
    isString(v.full_id) &&
    isString(v.core_stereo_id) &&
    isString(v.experiment_ref) &&
    isNullableString(v.experiment_code) &&
    isNullableString(v.experiment_url) &&
    isNumber(v.product_index) &&
    isString(v.stereo_mode) &&
    isString(v.created_at) &&
    isNullableString(v.retired_reason) &&
    typeof v.retired === "boolean" &&
    readYield(v.yield)
  );
}

export function isStereoVariant(v: unknown): v is StereoVariant {
  return (
    isRecord(v) &&
    isString(v.core_stereo_id) &&
    isString(v.stereo_mode) &&
    isNumber(v.batch_count) &&
    isNullableString(v.svg ?? null)
  );
}

export function isAnalyzeResult(v: unknown): v is AnalyzeResult {
  return (
    isRecord(v) &&
    isStructureInfo(v.structure) &&
    isString(v.status) &&
    (STATUSES as readonly string[]).includes(v.status) &&
    isNullableString(v.id) &&
    (v.status !== "registered_here" || isString(v.id)) &&
    isNullableString(v.core_stereo_id) &&
    isOptionalName(v.name) &&
    Array.isArray(v.batches) &&
    v.batches.every(isBatch) &&
    Array.isArray(v.other_stereos) &&
    v.other_stereos.every(isStereoVariant) &&
    (v.slot_conflict === null || isBatch(v.slot_conflict)) &&
    isStringArray(v.mode_options) &&
    isStringArray(v.warnings)
  );
}

export function isRegisterResult(v: unknown): v is RegisterResult {
  return (
    isRecord(v) &&
    isString(v.id) &&
    v.id.length > 0 &&
    typeof v.created === "boolean" &&
    isBatch(v.batch) &&
    isStringArray(v.warnings) &&
    isRecord(v.backup) &&
    typeof v.backup.performed === "boolean" &&
    isNullableString(v.backup.path) &&
    isNullableString(v.backup.warning)
  );
}

/** Enough to explain a protocol mismatch, even if the rest of the shape differs. */
export function hasProtocolVersion(v: unknown): v is { protocol_version: number } {
  return isRecord(v) && isNumber(v.protocol_version);
}

export function isPingResult(v: unknown): v is PingResult {
  return (
    isRecord(v) &&
    isNumber(v.protocol_version) &&
    isString(v.home_source) &&
    isString(v.home) &&
    isString(v.db_path) &&
    isStringArray(v.warnings)
  );
}

const LEVELS = ["batch", "stereo", "core"];

export function isFoundVariant(v: unknown, full: boolean): v is FoundVariant {
  return (
    isRecord(v) &&
    isString(v.core_stereo_id) &&
    isString(v.stereo_mode) &&
    isNumber(v.batch_count) &&
    (v.svg === undefined || isNullableString(v.svg)) &&
    isOptionalName(v.name) &&
    (v.inchi === undefined || isString(v.inchi)) &&
    isOptionalProperties(v.properties) &&
    (full ? Array.isArray(v.batches) && v.batches.every(isBatch) : v.batches === undefined || (Array.isArray(v.batches) && v.batches.every(isBatch)))
  );
}

export function isFoundExperimentBatch(v: unknown): v is FoundExperimentBatch {
  return (
    isRecord(v) &&
    isString(v.full_id) &&
    isString(v.core_stereo_id) &&
    isNumber(v.product_index) &&
    isString(v.created_at) &&
    typeof v.retired === "boolean" &&
    readYield(v.yield)
  );
}

export function isFoundExperiment(v: unknown): v is FoundExperiment {
  return (
    isRecord(v) &&
    isString(v.experiment_ref) &&
    isNullableString(v.experiment_code) &&
    isNullableString(v.experiment_url) &&
    Array.isArray(v.batches) &&
    v.batches.every(isFoundExperimentBatch)
  );
}

export function isFindResult(v: unknown): v is FindResult {
  return (
    isRecord(v) &&
    isString(v.query) &&
    isString(v.level) &&
    LEVELS.includes(v.level) &&
    isNumber(v.core_no) &&
    isNullableString(v.matched_id) &&
    (v.level !== "batch" || isString(v.matched_id)) &&
    Array.isArray(v.variants) &&
    v.variants.every((x) => isFoundVariant(x, true)) &&
    Array.isArray(v.other_variants) &&
    v.other_variants.every((x) => isFoundVariant(x, false)) &&
    Array.isArray(v.experiments) &&
    v.experiments.every(isFoundExperiment) &&
    isStringArray(v.warnings)
  );
}

export function isCompoundVariant(v: unknown): v is CompoundVariant {
  return (
    isRecord(v) &&
    isString(v.core_stereo_id) &&
    isNumber(v.stereo_no) &&
    isString(v.stereo_mode) &&
    isString(v.smiles) &&
    isString(v.inchi) &&
    isString(v.inchikey) &&
    (v.name === null || isNameInfo(v.name)) &&
    isNumber(v.batch_count) &&
    typeof v.matched === "boolean"
  );
}

export function isCompoundExperiment(v: unknown): v is CompoundExperiment {
  return (
    isRecord(v) &&
    isString(v.experiment_ref) &&
    isNullableString(v.experiment_code) &&
    isNullableString(v.experiment_url)
  );
}

export function isCompoundItem(v: unknown): v is CompoundItem {
  return (
    isRecord(v) &&
    isNumber(v.core_no) &&
    isString(v.core_id) &&
    v.core_id.length > 0 &&
    isString(v.formula) &&
    (v.properties === null || isProperties(v.properties)) &&
    isNullableString(v.svg ?? null) &&
    Array.isArray(v.variants) &&
    v.variants.every(isCompoundVariant) &&
    isNumber(v.batch_count) &&
    isNumber(v.experiment_count) &&
    Array.isArray(v.experiments) &&
    v.experiments.every(isCompoundExperiment) &&
    isString(v.first_created_at) &&
    isString(v.last_created_at) &&
    // Reasons this extension does not know yet are shown as they are.
    isStringArray(v.matched_by)
  );
}

export function isCompoundsResult(v: unknown): v is CompoundsResult {
  return (
    isRecord(v) &&
    isNumber(v.total) &&
    isNumber(v.registry_total) &&
    isNumber(v.offset) &&
    isNumber(v.limit) &&
    Array.isArray(v.items) &&
    v.items.every(isCompoundItem) &&
    isStringArray(v.notes) &&
    isStringArray(v.warnings)
  );
}

export function isSetNameResult(v: unknown): v is SetNameResult {
  return (
    isRecord(v) &&
    isString(v.core_stereo_id) &&
    v.core_stereo_id.length > 0 &&
    (v.name === null || isNameInfo(v.name)) &&
    typeof v.changed === "boolean" &&
    isStringArray(v.warnings)
  );
}

/** Not getting a name is an ok answer (`generated: false`); a generated name must be there. */
export function isGenerateNameResult(v: unknown): v is GenerateNameResult {
  return (
    isRecord(v) &&
    isString(v.core_stereo_id) &&
    v.core_stereo_id.length > 0 &&
    (v.name === null || isNameInfo(v.name)) &&
    typeof v.generated === "boolean" &&
    (!v.generated || v.name !== null) &&
    typeof v.changed === "boolean" &&
    isNullableString(v.message) &&
    isStringArray(v.warnings)
  );
}

export function isSetYieldResult(v: unknown): v is SetYieldResult {
  return (
    isRecord(v) &&
    isString(v.id) &&
    v.id.length > 0 &&
    (v.yield === null || isYieldInfo(v.yield)) &&
    typeof v.changed === "boolean" &&
    isStringArray(v.warnings)
  );
}

export function isYieldRow(v: unknown): v is YieldRow {
  return (
    isRecord(v) &&
    isString(v.experiment_ref) &&
    isNullableString(v.experiment_code) &&
    isNullableString(v.experiment_url) &&
    isNumber(v.product_index) &&
    isString(v.full_id) &&
    v.full_id.length > 0 &&
    isString(v.core_stereo_id) &&
    isNullableString(v.name) &&
    (v.yield === null || isYieldInfo(v.yield)) &&
    isString(v.created_at) &&
    typeof v.retired === "boolean"
  );
}

export function isYieldsResult(v: unknown): v is YieldsResult {
  return (
    isRecord(v) &&
    isNumber(v.total) &&
    isNumber(v.registry_total) &&
    isNumber(v.offset) &&
    isNumber(v.limit) &&
    Array.isArray(v.items) &&
    v.items.every(isYieldRow) &&
    isStringArray(v.warnings)
  );
}

// ------------------------------------------------------------------ compound folders and spectra

/** A path on the registry's PC is shown as text only; longer ones are not plausible. */
export const MAX_PATH_TEXT = 4096;
/** Sanity caps of a `compound_files` answer (the host has no fixed limit for these). */
export const MAX_SPECTRA = 500;
export const MAX_SPECTRUM_PARAMETERS = 500;
/** Base64 of PDF_CHUNK_BYTES bytes. */
export const MAX_PDF_CHUNK_TEXT = Math.ceil(PDF_CHUNK_BYTES / 3) * 4;

const isWhole = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const isNullableNumber = (v: unknown): v is number | null => v === null || isNumber(v);
const isNullableWhole = (v: unknown): v is number | null => v === null || isWhole(v);

function hasControlChars(text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c < 0x20 || c === 0x7f) return true;
  }
  return false;
}

/** A path as text: one line, not blank, not absurdly long. */
export function isPathText(v: unknown): v is string {
  return isString(v) && v.trim() !== "" && v.length <= MAX_PATH_TEXT && !hasControlChars(v);
}

/** A plain file name of the folder (never a path): sent back unchanged in `read_pdf`. */
export function isPlainFileName(v: unknown): v is string {
  return (
    isString(v) &&
    v.trim() !== "" &&
    v.length <= MAX_FILE_NAME &&
    !hasControlChars(v) &&
    !/[\\/]/.test(v) &&
    v !== "." &&
    v !== ".."
  );
}

/** Base64 text (standard alphabet, padded); empty for an empty piece. */
export function isBase64Text(v: unknown): v is string {
  return isString(v) && v.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(v);
}

export function isSpectrumParameter(v: unknown): v is SpectrumParameter {
  return isRecord(v) && isString(v.name) && isString(v.value);
}

export function isSpectrumInfo(v: unknown): v is SpectrumInfo {
  return (
    isRecord(v) &&
    isString(v.full_id) &&
    v.full_id.length > 0 &&
    isString(v.spectrum_key) &&
    v.spectrum_key.length > 0 &&
    isString(v.updated_at) &&
    isNullableString(v.experiment_code) &&
    isNullableString(v.title) &&
    isNullableString(v.comment) &&
    isNullableString(v.sample) &&
    isNullableString(v.nucleus) &&
    isNullableString(v.solvent) &&
    isNullableString(v.experiment) &&
    isNullableString(v.pulse_sequence) &&
    isNullableNumber(v.frequency_mhz) &&
    (v.frequency_mhz === null || v.frequency_mhz >= 0) &&
    isNullableWhole(v.dimensions) &&
    isNullableString(v.report) &&
    Array.isArray(v.parameters) &&
    v.parameters.length <= MAX_SPECTRUM_PARAMETERS &&
    v.parameters.every(isSpectrumParameter) &&
    (v.mnova_file === null || isPathText(v.mnova_file)) &&
    (v.pdf_file === null || isPathText(v.pdf_file)) &&
    typeof v.pdf_available === "boolean" &&
    (v.crude === undefined || typeof v.crude === "boolean")
  );
}

export function isFolderFile(v: unknown): v is FolderFile {
  return (
    isRecord(v) &&
    isPlainFileName(v.name) &&
    isWhole(v.size) &&
    isString(v.modified) &&
    typeof v.is_pdf === "boolean" &&
    typeof v.recorded === "boolean"
  );
}

export function isCompoundFilesResult(v: unknown): v is CompoundFilesResult {
  return (
    isRecord(v) &&
    isString(v.core_stereo_id) &&
    v.core_stereo_id.length > 0 &&
    (v.folder === null || (isRecord(v.folder) && isPathText(v.folder.path) && typeof v.folder.exists === "boolean")) &&
    Array.isArray(v.spectra) &&
    v.spectra.length <= MAX_SPECTRA &&
    v.spectra.every(isSpectrumInfo) &&
    Array.isArray(v.files) &&
    v.files.length <= MAX_FOLDER_FILES &&
    v.files.every(isFolderFile) &&
    typeof v.files_truncated === "boolean" &&
    isStringArray(v.warnings)
  );
}

export function isReadPdfResult(v: unknown): v is ReadPdfResult {
  return (
    isRecord(v) &&
    isPlainFileName(v.name) &&
    isWhole(v.size) &&
    v.size <= MAX_PDF_BYTES &&
    isWhole(v.offset) &&
    isBase64Text(v.data) &&
    v.data.length <= MAX_PDF_CHUNK_TEXT &&
    typeof v.eof === "boolean"
  );
}

// ------------------------------------------------------------------ reactions and routes

export function isSetReactionResult(v: unknown): v is SetReactionResult {
  return (
    isRecord(v) &&
    isString(v.experiment_ref) &&
    v.experiment_ref.length > 0 &&
    typeof v.changed === "boolean" &&
    isString(v.updated_at) &&
    isStringArray(v.warnings)
  );
}

export function isRouteMissing(v: unknown): v is RouteMissing {
  return (
    isRecord(v) &&
    isString(v.experiment_ref) &&
    isNullableString(v.experiment_code) &&
    isNullableString(v.experiment_url) &&
    isString(v.compound)
  );
}

export function isRouteResult(v: unknown): v is RouteResult {
  return (
    isRecord(v) &&
    isString(v.core_stereo_id) &&
    v.core_stereo_id.length > 0 &&
    (v.file === null || (isRecord(v.file) && isPathText(v.file.path) && isPlainFileName(v.file.name))) &&
    typeof v.opened === "boolean" &&
    isNullableString(v.open_message) &&
    isWhole(v.steps) &&
    isStringArray(v.experiments) &&
    Array.isArray(v.missing) &&
    v.missing.every(isRouteMissing) &&
    isStringArray(v.notes) &&
    Array.isArray(v.choices) &&
    v.choices.every(isRouteChoice) &&
    (v.cdxml === null || (isString(v.cdxml) && v.cdxml.length <= MAX_ROUTE_CDXML_CHARS)) &&
    isStringArray(v.warnings)
  );
}

/** A reactant row: 1..MAX_PRODUCT_INDEX. */
const isReactantRow = (v: unknown): v is number => isWhole(v) && v >= 1 && v <= MAX_PRODUCT_INDEX;
const isNullableReactantRow = (v: unknown): v is number | null => v === null || isReactantRow(v);

export function isLimitingCandidate(v: unknown): v is LimitingCandidate {
  return isRecord(v) && isReactantRow(v.row) && isNullableString(v.name) && isNullableNumber(v.eq) && isNullableString(v.mass);
}

/** One run of a route answer (starting-material switch); its candidates' rows are distinct. */
export function isRouteChoice(v: unknown): v is RouteChoice {
  if (
    !(
      isRecord(v) &&
      isString(v.experiment_ref) &&
      v.experiment_ref.length > 0 &&
      isNullableString(v.experiment_code) &&
      isString(v.compound) &&
      isNullableReactantRow(v.row) &&
      (v.by === null || v.by === "user" || v.by === "auto") &&
      isNullableReactantRow(v.auto_row) &&
      typeof v.ambiguous === "boolean" &&
      Array.isArray(v.candidates) &&
      v.candidates.every(isLimitingCandidate)
    )
  ) {
    return false;
  }
  const rows = (v.candidates as LimitingCandidate[]).map((c) => c.row);
  return new Set(rows).size === rows.length;
}

export function isSetLimitingResult(v: unknown): v is SetLimitingResult {
  return (
    isRecord(v) &&
    isString(v.experiment_ref) &&
    v.experiment_ref.length > 0 &&
    isNullableReactantRow(v.row) &&
    isNullableString(v.name) &&
    typeof v.changed === "boolean" &&
    isStringArray(v.warnings)
  );
}

/**
 * `register.folder`: the compound's folder on the registry's PC. Absent (older
 * host), null (none could be made) or malformed: null, and nothing is shown.
 * A malformed folder never makes the register answer unreadable: an issued ID
 * must always be shown.
 */
export function readRegisterFolder(result: unknown): { path: string; created: boolean } | null {
  if (!isRecord(result)) return null;
  const folder = result.folder;
  if (!isRecord(folder) || !isPathText(folder.path) || typeof folder.created !== "boolean") return null;
  return { path: folder.path, created: folder.created };
}

/** `ping.files_folder`: the folder that holds the compound folders. Absent, null or malformed: null. */
export function readFilesFolder(ping: unknown): string | null {
  if (!isRecord(ping)) return null;
  return isPathText(ping.files_folder) ? ping.files_folder : null;
}

/** Checks one op's result. A ping with another protocol version is accepted as long as the version is readable. */
export function isResultOf<K extends Op>(op: K, value: unknown, protocolVersion: number): value is ResultByOp[K] {
  switch (op) {
    case "ping":
      return hasProtocolVersion(value) && (value.protocol_version !== protocolVersion || isPingResult(value));
    case "analyze":
      return isAnalyzeResult(value);
    case "register":
      return isRegisterResult(value);
    case "retire":
      return isRecord(value) && isString(value.id);
    case "find":
      return isFindResult(value);
    case "compounds":
      return isCompoundsResult(value);
    case "set_name":
      return isSetNameResult(value);
    case "generate_name":
      return isGenerateNameResult(value);
    case "set_yield":
      return isSetYieldResult(value);
    case "yields":
      return isYieldsResult(value);
    case "compound_files":
      return isCompoundFilesResult(value);
    case "read_pdf":
      return isReadPdfResult(value);
    case "set_reaction":
      return isSetReactionResult(value);
    case "route":
      return isRouteResult(value);
    case "set_limiting":
      return isSetLimitingResult(value);
    default:
      return false;
  }
}

/** Envelope check of a background reply. */
export function readReply(value: unknown): BackgroundReply | null {
  if (!isRecord(value)) return null;
  if (value.ok === true && isRecord(value.result)) return { ok: true, result: value.result as never };
  if (value.ok === false && isHostError(value.error)) {
    const error: HostError = { code: value.error.code, message: value.error.message };
    if (isString(value.error.existing_id)) error.existing_id = value.error.existing_id;
    return { ok: false, error };
  }
  return null;
}
