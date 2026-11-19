/**
 * State machine of one product card: lookup, staleness, stereo mode choice,
 * ignored-stereo acceptance, the confirmation step and registration.
 *
 * No DOM. The controller talks to the host only through `HostClient`, and all
 * gating rules live in `deriveView`, which is a pure function of the state.
 *
 * Rules that protect what gets registered:
 * - `register` sends EXACTLY the structure and mode of the analysis on screen
 *   (kept in `analysis.snapshot`), never what the form holds now.
 * - Any change of structure, mode, experiment key or product no. makes the
 *   analysis stale; a stale analysis cannot be used to create an ID.
 * - Only the latest analyze response is used; older ones are dropped.
 * - At most one request runs at a time per card, so a double click cannot
 *   send two `register` requests. A register answer is never dropped.
 *
 * Yields (`set_yield`, see `yieldToSave`): the yields the page shows for a
 * product that has an ID here are stored with that ID, in the background,
 * whenever they differ from what the registry holds. One request per change,
 * never a retry loop, never from a source without yields (the manual form).
 */

import type { ExperimentContext, PageYields, ProductInput } from "../shared/adapter";
import {
  MODE_OVERRIDES,
  REGISTRY_CHANGED,
  type AnalysisStatus,
  type AnalyzeParams,
  type AnalyzeResult,
  type BackgroundReply,
  type Batch,
  type HostError,
  type ModeOverride,
  type NameInfo,
  type Op,
  type RegisterParams,
  type RegisterResult,
  type SetYieldParams,
  type StereoMode,
  type StructureInput,
  type YieldInfo,
} from "../shared/protocol";
import type { HostClient } from "./host-client";
import { retireHint } from "./errors";
import { CREATE_LABELS, MODE_OVERRIDE_LABELS, stereoModeLabel } from "./labels";
import { experimentLink } from "./safe-values";
import { sameStructure } from "./structure-kind";
import { roundedYields, sameValues, sameYields } from "./yields";

export interface CardInput {
  product: ProductInput;
  experiment: ExperimentContext | null;
}

/** What an analysis was computed for. */
export interface AnalysisSnapshot {
  structure: StructureInput;
  mode: ModeOverride | null;
  experimentRef: string | null;
  productIndex: number;
  changeToken: string;
}

export interface Analysis {
  seq: number;
  snapshot: AnalysisSnapshot;
  result: AnalyzeResult;
}

/** Everything the confirmation names and exactly what `register` will send. */
export interface RegisterPlan {
  analysisSeq: number;
  params: RegisterParams;
  status: Exclude<AnalysisStatus, "registered_here">;
  stereoModeLabel: string;
  coreStereoId: string | null;
  experimentRef: string;
  experimentCode: string | null;
  productIndex: number;
  acceptedIgnored: string[];
}

export interface Registration {
  analysisSeq: number;
  plan: RegisterPlan;
  result: RegisterResult;
}

export interface CardState {
  input: CardInput | null;
  /** The mode the user picked; null = as detected (the host's default). */
  selectedMode: ModeOverride | null;
  analysis: Analysis | null;
  /**
   * Set after a failed register or when the registry configuration changed:
   * the shown analysis may be out of date, look up again.
   */
  needsRefresh: boolean;
  /** Answers to requests up to this sequence number were computed before an invalidation. */
  refreshAfterSeq: number;
  /** One flag per `analysis.result.structure.ignored_stereo` item. */
  ticked: boolean[];
  /** `phase: "recheck"`: the structure is being re-read from its origin before `register`. */
  pending: { op: "analyze" | "register"; seq: number; phase?: "recheck" } | null;
  latestSeq: number;
  confirming: RegisterPlan | null;
  registration: Registration | null;
  error: { op: Op; error: HostError } | null;
  /** Why the last Create sent nothing after the re-check. */
  recheckNotice: RecheckNotice | null;
  /**
   * A name stored from this card (`set_name`) after the shown analysis was
   * requested. Display only: it never makes the analysis stale and never
   * touches `pending`, `confirming` or `registration`. It applies to analyses
   * with `seq <= afterSeq` (requested before the name was sent); a later
   * analysis carries the stored name itself.
   */
  nameOverride: { coreStereoId: string; name: NameInfo | null; afterSeq: number } | null;
  /** Storing the page's yields with the card's ID (`set_yield`). */
  yieldSync: YieldSync;
}

/**
 * The card's `set_yield` bookkeeping. `afterSeq` values are the card's
 * `latestSeq` when an answer arrived: analyses requested up to then may have
 * been computed before it, later ones were computed after it.
 */
export interface YieldSync {
  /** What the registry stores for `id` according to this card's last successful `set_yield`. */
  stored: { id: string; yield: YieldInfo | null; afterSeq: number } | null;
  /** The request in flight (at most one per card). */
  sending: { seq: number; params: SetYieldParams } | null;
  /**
   * The last `set_yield` that failed: the same values are not sent again for
   * the same ID until the page shows other values or a later analysis exists.
   */
  failed: { id: string; values: PageYields; afterSeq: number; message: string } | null;
  seq: number;
}

export const NO_YIELD_SYNC: YieldSync = Object.freeze({ stored: null, sending: null, failed: null, seq: 0 }) as YieldSync;

export type RecheckNotice =
  | { kind: "changed"; message: string }
  | { kind: "unavailable"; heading: string; message: string };

export type RecheckOutcome = { result: "unchanged" | "changed" } | { result: "unavailable"; message: string };

export const RECHECK_CHANGED_MESSAGE =
  "The structure on the page changed, so no ID was created. It was looked up again: check the result and click Create ID again.";
export const RECHECK_UNAVAILABLE_HEADING = "The structure could not be re-checked";

export function initialCardState(): CardState {
  return {
    input: null,
    selectedMode: null,
    analysis: null,
    needsRefresh: false,
    refreshAfterSeq: 0,
    ticked: [],
    pending: null,
    latestSeq: 0,
    confirming: null,
    registration: null,
    error: null,
    recheckNotice: null,
    nameOverride: null,
    yieldSync: NO_YIELD_SYNC,
  };
}

// ------------------------------------------------------------------ yields

/**
 * The batch whose yield this card shows and stores: the batch of the latest
 * analysis when it says `registered_here` (the batch with that `id`), else the
 * batch of an ID created on that analysis. Null otherwise.
 */
export function yieldBatch(state: CardState): Batch | null {
  const analysis = state.analysis;
  if (!analysis) return null;
  const r = analysis.result;
  if (r.status === "registered_here" && r.id) return r.batches.find((b) => b.full_id === r.id) ?? null;
  const reg = state.registration;
  if (reg && reg.analysisSeq === analysis.seq && reg.result.batch.full_id === reg.result.id) return reg.result.batch;
  return null;
}

/**
 * What the registry stores for the card's batch: the answer of this card's
 * own `set_yield` when it is newer than the analysis, else the batch's `yield`.
 * Undefined when there is no such batch or the host does not report yields
 * (an older host).
 */
export function storedYield(state: CardState): YieldInfo | null | undefined {
  const batch = yieldBatch(state);
  if (!batch || batch.yield === undefined) return undefined;
  const own = state.yieldSync.stored;
  if (own && own.id === batch.full_id && state.analysis!.seq <= own.afterSeq) return own.yield;
  return batch.yield;
}

/**
 * The `set_yield` to send now, or null. Sent only when ALL hold: the card has
 * an experiment and the page's yields (an object: read in view mode); the
 * analysis on screen is current and nothing else runs on the card; it names a
 * batch (`yieldBatch`) that is not retired and was registered for exactly this
 * experiment and product no.; the host reports yields (`batch.yield` present);
 * the page's values, rounded to two decimals, differ from the stored ones; no
 * `set_yield` is in flight; and these values did not just fail for this ID.
 */
export function yieldToSave(state: CardState): SetYieldParams | null {
  const { input, analysis } = state;
  if (!input || !analysis || state.pending !== null) return null;
  const experiment = input.experiment;
  const page = input.product.yields;
  if (!experiment || page === null || page === undefined || typeof page !== "object") return null;
  if (staleReason(state) !== null) return null;
  const batch = yieldBatch(state);
  if (!batch || batch.retired) return null;
  if (batch.experiment_ref !== experiment.ref || batch.product_index !== input.product.productIndex) return null;
  if (batch.yield === undefined) return null;
  const sync = state.yieldSync;
  if (sync.sending !== null) return null;
  const stored = storedYield(state);
  if (stored === undefined || sameYields(page, stored)) return null;
  const values = roundedYields(page);
  const failed = sync.failed;
  if (failed && failed.id === batch.full_id && sameValues(failed.values, values) && analysis.seq <= failed.afterSeq) return null;
  return {
    id: batch.full_id,
    experiment_ref: experiment.ref,
    product_index: input.product.productIndex,
    isolated: values.isolated,
    analytical: values.analytical,
  };
}

/** The yield shown on the card of a product that has an ID here; null/undefined: nothing to show. */
export function cardYield(state: CardState): YieldInfo | null | undefined {
  if (deriveView(state).status !== "registered_here") return undefined;
  return storedYield(state);
}

/** The short note after a failed `set_yield` for the card's current ID; null otherwise. */
export function yieldNote(state: CardState): string | null {
  const failed = state.yieldSync.failed;
  const batch = yieldBatch(state);
  if (!failed || !batch || failed.id !== batch.full_id) return null;
  return `The yield could not be saved: ${failed.message}`;
}

/** The name row of a card: which CORE_STEREO it names and what is stored. */
export interface CardName {
  /** The CORE_STEREO of the compound shown; null while it has no ID yet. */
  target: string | null;
  name: NameInfo | null;
}

/**
 * The name shown on the card, or null when there is no analysis or the host
 * does not report names (an older host: the card then shows no name row).
 * After an ID was created on this analysis, the name belongs to its CORE_STEREO.
 */
export function cardName(state: CardState): CardName | null {
  const analysis = state.analysis;
  if (!analysis) return null;
  const reg = state.registration;
  const registeredHere = reg !== null && reg.analysisSeq === analysis.seq;
  const target = registeredHere ? reg.result.batch.core_stereo_id : analysis.result.core_stereo_id;
  const override = state.nameOverride;
  if (override && target !== null && override.coreStereoId === target && analysis.seq <= override.afterSeq) {
    return { target, name: override.name };
  }
  if (analysis.result.name === undefined) return null;
  const name = target !== null && target === analysis.result.core_stereo_id ? (analysis.result.name ?? null) : null;
  return { target, name };
}

/**
 * The CORE_STEREO to name after a register: set when the ID shown was issued
 * on the current analysis and its compound has no stored name. Null when a
 * name is stored, when there was no register on this analysis, or when the
 * host does not report names (an older host).
 */
export function autoNameTarget(state: CardState): string | null {
  const reg = state.registration;
  const analysis = state.analysis;
  if (!reg || !analysis || reg.analysisSeq !== analysis.seq) return null;
  const name = cardName(state);
  if (!name || name.target === null || name.name !== null) return null;
  return name.target;
}

// ------------------------------------------------------------------ derived view

export interface ModeOption {
  value: string;
  label: string;
  disabled: boolean;
}

export interface ModeControl {
  disabled: boolean;
  value: string;
  options: ModeOption[];
}

export interface CardView {
  hasInput: boolean;
  busy: "analyze" | "recheck" | "register" | null;
  stale: boolean;
  staleReason: string | null;
  canLookUp: boolean;
  lookUpLabel: string;
  /** Status after taking a registration made on this analysis into account. */
  status: AnalysisStatus | null;
  existingId: string | null;
  modeControl: ModeControl | null;
  ignored: { text: string; ticked: boolean }[];
  createLabel: string | null;
  canCreate: boolean;
  createBlockers: string[];
  confirming: RegisterPlan | null;
}

const isOverride = (mode: string): mode is ModeOverride => (MODE_OVERRIDES as readonly string[]).includes(mode);

function currentRef(input: CardInput | null): string | null {
  return input?.experiment?.ref ?? null;
}

export function staleReason(state: CardState): string | null {
  const { analysis, input } = state;
  if (!analysis) return null;
  if (!input) return "The input is incomplete.";
  const snap = analysis.snapshot;
  if (snap.changeToken !== input.product.changeToken || !sameStructure(snap.structure, input.product.structure)) {
    return "The structure changed since this lookup.";
  }
  if (snap.mode !== state.selectedMode) return "The stereo mode changed since this lookup.";
  if (snap.experimentRef !== currentRef(input)) return "The experiment key changed since this lookup.";
  if (snap.productIndex !== input.product.productIndex) return "The product no. changed since this lookup.";
  if (state.needsRefresh) return "The registry may have changed since this lookup.";
  return null;
}

function modeControl(state: CardState, stale: boolean): ModeControl | null {
  const analysis = state.analysis;
  if (!analysis) return null;
  const shown = analysis.result.structure.stereo_mode as StereoMode;
  const choices = analysis.result.mode_options.filter(isOverride);
  const offered = (MODE_OVERRIDES as readonly ModeOverride[]).filter((m) => choices.includes(m));
  if (shown === "none") {
    return { disabled: true, value: "none", options: [{ value: "none", label: "No stereo", disabled: true }] };
  }
  // While a different mode is being looked up (or failed), show the mode that was asked for;
  // the analysis below is then greyed out as stale.
  const value = stale && state.selectedMode !== null ? state.selectedMode : shown;
  const options: ModeOption[] = [];
  if (!isOverride(shown)) {
    options.push({ value: shown, label: `${stereoModeLabel(shown)} (as detected)`, disabled: true });
  }
  for (const mode of offered) options.push({ value: mode, label: MODE_OVERRIDE_LABELS[mode], disabled: false });
  if (!options.some((o) => o.value === value)) {
    options.unshift({ value, label: stereoModeLabel(value), disabled: true });
  }
  return { disabled: state.pending !== null || state.input === null, value, options };
}

export function deriveView(state: CardState): CardView {
  const { analysis, input, pending } = state;
  const reason = staleReason(state);
  const stale = analysis !== null && reason !== null;
  const registeredHere =
    state.registration !== null && analysis !== null && state.registration.analysisSeq === analysis.seq;
  const status: AnalysisStatus | null = analysis ? (registeredHere ? "registered_here" : analysis.result.status) : null;
  const existingId = registeredHere ? state.registration!.result.id : (analysis?.result.id ?? null);
  const ignoredTexts = analysis?.result.structure.ignored_stereo ?? [];
  const ignored = ignoredTexts.map((text, i) => ({ text, ticked: state.ticked[i] === true }));

  const blockers: string[] = [];
  if (!analysis) blockers.push("Look up the structure first.");
  if (pending) blockers.push("Wait for the current request to finish.");
  if (analysis && stale) blockers.push(`${reason} Look up again.`);
  if (status === "registered_here") blockers.push("This compound already has an ID in this experiment.");
  if (analysis && currentRef(input) === null) blockers.push("Enter the experiment key to create an ID.");
  const conflict = analysis?.result.slot_conflict ?? null;
  if (conflict) {
    blockers.push(
      `Product no. ${conflict.product_index} of this experiment already holds ${conflict.full_id}. ` +
        `It must be retired before another compound can take this slot. ${retireHint(conflict.full_id)}`,
    );
  }
  if (ignored.some((item) => !item.ticked)) blockers.push("Tick every stereo feature listed above to accept it.");

  let lookUpLabel = analysis ? "Look up again" : "Look up";
  if (pending?.op === "analyze") lookUpLabel = "Looking up…";

  return {
    hasInput: input !== null,
    busy: pending?.phase === "recheck" ? "recheck" : (pending?.op ?? null),
    stale,
    staleReason: stale ? reason : null,
    canLookUp: input !== null && pending === null,
    lookUpLabel,
    status,
    existingId,
    modeControl: modeControl(state, stale),
    ignored,
    createLabel: status && status !== "registered_here" ? CREATE_LABELS[status] : null,
    canCreate: blockers.length === 0,
    createBlockers: blockers,
    confirming: state.confirming,
  };
}

// ------------------------------------------------------------------ params

function structureParams(structure: StructureInput): StructureInput {
  return structure.molfile !== undefined ? { molfile: structure.molfile } : { smiles: structure.smiles };
}

export function analyzeParams(snapshot: AnalysisSnapshot): AnalyzeParams {
  const params: AnalyzeParams = { ...structureParams(snapshot.structure), mode: snapshot.mode };
  if (snapshot.experimentRef !== null) {
    params.experiment_ref = snapshot.experimentRef;
    params.product_index = snapshot.productIndex;
  }
  return params;
}

/** Builds the register request from the analysis on screen. Null when creating is not allowed. */
export function buildPlan(state: CardState): RegisterPlan | null {
  const view = deriveView(state);
  const analysis = state.analysis;
  const experiment = state.input?.experiment ?? null;
  if (!view.canCreate || !analysis || !experiment || !view.status || view.status === "registered_here") return null;
  const snap = analysis.snapshot;
  const structure = analysis.result.structure;
  if (snap.experimentRef === null || snap.experimentRef !== experiment.ref) return null;
  const accepted = [...structure.ignored_stereo];
  const params: RegisterParams = {
    ...structureParams(snap.structure),
    mode: snap.mode,
    experiment_ref: snap.experimentRef,
    experiment_code: experiment.code,
    experiment_url: experimentLink(experiment.url),
    product_index: snap.productIndex,
    confirm: { inchikey: structure.inchikey, stereo_mode: structure.stereo_mode, key_digest: structure.key_digest },
  };
  if (accepted.length > 0) params.accept_ignored_stereo = accepted;
  return {
    analysisSeq: analysis.seq,
    params,
    status: view.status,
    stereoModeLabel: stereoModeLabel(structure.stereo_mode),
    coreStereoId: analysis.result.core_stereo_id,
    experimentRef: snap.experimentRef,
    experimentCode: experiment.code,
    productIndex: snap.productIndex,
    acceptedIgnored: accepted,
  };
}

function yieldsKey(yields: PageYields | null | undefined): string {
  return yields === undefined ? "absent" : JSON.stringify(yields);
}

function inputsEqual(a: CardInput | null, b: CardInput | null): boolean {
  return sameExceptYields(a, b) && (a === null || yieldsKey(a.product.yields) === yieldsKey(b!.product.yields));
}

/** Equal in everything but the yields (which never make an analysis stale). */
function sameExceptYields(a: CardInput | null, b: CardInput | null): boolean {
  if (a === null || b === null) return a === b;
  return (
    a.product.changeToken === b.product.changeToken &&
    sameStructure(a.product.structure, b.product.structure) &&
    a.product.productIndex === b.product.productIndex &&
    a.product.label === b.product.label &&
    (a.experiment?.ref ?? null) === (b.experiment?.ref ?? null) &&
    (a.experiment?.code ?? null) === (b.experiment?.code ?? null) &&
    (a.experiment?.url ?? null) === (b.experiment?.url ?? null)
  );
}

// ------------------------------------------------------------------ controller

export interface CardHooks {
  onChange(): void;
  announce?(message: string): void;
}

export interface CardOptions {
  /**
   * Look the product up by itself when it appears and whenever its token,
   * the experiment key or the product no. changes, and again after every
   * register (lookups are read-only; nothing is ever created automatically).
   */
  autoLookup?: boolean;
  /** "Create ID" first shows a confirmation (default true). */
  confirmBeforeCreate?: boolean;
  /** Debounce of automatic lookups, so a burst of page changes gives one request. */
  debounceMs?: number;
  /** Timer used for the debounce; returns a cancel function. */
  schedule?: (fn: () => void, ms: number) => () => void;
  /**
   * The source's `recheck`: called right before `register`; nothing is sent
   * unless it answers "unchanged". Absent for the manual source.
   */
  recheck?: ((product: ProductInput) => Promise<RecheckOutcome>) | undefined;
  /**
   * Called once after each successful `register` whose compound has no stored
   * name (`autoNameTarget`), never after a lookup. `sentAfterSeq` covers the
   * lookup started after the register, for `applyName`. The panel uses it to
   * get the name from ChemDraw when that is available.
   */
  onRegisteredWithoutName?: ((coreStereoId: string, sentAfterSeq: number) => void) | undefined;
  /**
   * Called once after each successful `register` (also when the ID already
   * existed), with the experiment it was registered for. The panel uses it to
   * store the experiment's reaction (`set_reaction`).
   */
  onRegistered?: ((experiment: ExperimentContext, result: RegisterResult) => void) | undefined;
  /**
   * Store the page's yields with the product's ID (`set_yield`, see
   * `yieldToSave`), in the background. Default false; the panel sets it for
   * the page source only, never for the manual form.
   */
  saveYields?: boolean;
}

export const AUTO_LOOKUP_DEBOUNCE_MS = 300;

function defaultSchedule(fn: () => void, ms: number): () => void {
  const timer = setTimeout(fn, ms);
  return () => clearTimeout(timer);
}

/** What an automatic lookup depends on (the stereo mode is handled by selectMode). */
function autoKeyOf(input: CardInput | null): string | null {
  if (!input) return null;
  return JSON.stringify([
    input.product.changeToken,
    input.product.structure.molfile ?? null,
    input.product.structure.smiles ?? null,
    input.experiment?.ref ?? null,
    input.product.productIndex,
  ]);
}

export class CardController {
  private s: CardState = initialCardState();
  private readonly autoLookup: boolean;
  private readonly confirmBeforeCreate: boolean;
  private readonly debounceMs: number;
  private readonly schedule: (fn: () => void, ms: number) => () => void;
  private readonly recheck: ((product: ProductInput) => Promise<RecheckOutcome>) | null;
  private readonly onRegisteredWithoutName: ((coreStereoId: string, sentAfterSeq: number) => void) | null;
  private readonly onRegistered: ((experiment: ExperimentContext, result: RegisterResult) => void) | null;
  private readonly saveYields: boolean;
  /** The input the last automatic lookup was scheduled for. */
  private autoKey: string | null = null;
  private cancelAuto: (() => void) | null = null;
  /** One automatic lookup per invalidation, until a lookup succeeds (no loops). */
  private autoAfterInvalidate = true;
  private disposed = false;

  constructor(
    private readonly client: HostClient,
    private readonly hooks: CardHooks,
    options: CardOptions = {},
  ) {
    this.autoLookup = options.autoLookup ?? false;
    this.confirmBeforeCreate = options.confirmBeforeCreate ?? true;
    this.debounceMs = options.debounceMs ?? AUTO_LOOKUP_DEBOUNCE_MS;
    this.schedule = options.schedule ?? defaultSchedule;
    this.recheck = options.recheck ?? null;
    this.onRegisteredWithoutName = options.onRegisteredWithoutName ?? null;
    this.onRegistered = options.onRegistered ?? null;
    this.saveYields = options.saveYields ?? false;
  }

  /** Stops automatic lookups (card removed). */
  dispose(): void {
    this.disposed = true;
    this.cancelAuto?.();
    this.cancelAuto = null;
  }

  private scheduleAutoLookup(): void {
    if (!this.autoLookup || this.disposed) return;
    this.cancelAuto?.();
    this.cancelAuto = this.schedule(() => {
      this.cancelAuto = null;
      // A running register looks up by itself when it ends.
      if (this.disposed || this.s.pending?.op === "register") return;
      void this.lookUp();
    }, this.debounceMs);
  }

  private maybeAutoLookup(): void {
    if (!this.autoLookup) return;
    const key = autoKeyOf(this.s.input);
    if (key === null) {
      this.cancelAuto?.();
      this.cancelAuto = null;
      this.autoKey = null;
      return;
    }
    if (key === this.autoKey) return;
    this.autoKey = key;
    this.scheduleAutoLookup();
  }

  get state(): Readonly<CardState> {
    return this.s;
  }

  view(): CardView {
    return deriveView(this.s);
  }

  private update(patch: Partial<CardState>): void {
    this.s = { ...this.s, ...patch };
    this.hooks.onChange();
  }

  /** New input from the source (null: the source has nothing usable right now). */
  setInput(input: CardInput | null): void {
    const prev = this.s.input;
    if (inputsEqual(prev, input)) return;
    // A change of the yields alone keeps everything else as it is (and may store them).
    const patch: Partial<CardState> = sameExceptYields(prev, input) ? { input } : { input, confirming: null };
    const structureChanged =
      prev !== null &&
      input !== null &&
      (prev.product.changeToken !== input.product.changeToken ||
        !sameStructure(prev.product.structure, input.product.structure));
    // A mode chosen for one drawing is not carried over to another.
    if (structureChanged) patch.selectedMode = null;
    this.update(patch);
    this.maybeAutoLookup();
    this.maybeSaveYield();
  }

  /** Sends `analyze` for the current input and mode. Supersedes a running analyze, never a register. */
  async lookUp(): Promise<void> {
    const input = this.s.input;
    if (!input || this.s.pending?.op === "register") return;
    const snapshot: AnalysisSnapshot = {
      structure: structureParams(input.product.structure),
      mode: this.s.selectedMode,
      experimentRef: input.experiment?.ref ?? null,
      productIndex: input.product.productIndex,
      changeToken: input.product.changeToken,
    };
    const seq = this.s.latestSeq + 1;
    this.update({ latestSeq: seq, pending: { op: "analyze", seq }, error: null, confirming: null });
    this.hooks.announce?.("Looking up…");
    let reply: BackgroundReply<"analyze">;
    try {
      reply = await this.client.request("analyze", analyzeParams(snapshot));
    } catch (error) {
      reply = { ok: false, error: { code: "internal_error", message: String(error) } };
    }
    if (seq !== this.s.latestSeq) return; // an older request: discard
    if (reply.ok) {
      // Keep showing "ID created" when the lookup confirms that same ID.
      const reg = this.s.registration;
      const keep = reg !== null && reply.result.status === "registered_here" && reply.result.id === reg.result.id;
      const needsRefresh = seq <= this.s.refreshAfterSeq;
      if (!needsRefresh) this.autoAfterInvalidate = true;
      this.update({
        pending: null,
        analysis: { seq, snapshot, result: reply.result },
        ticked: reply.result.structure.ignored_stereo.map(() => false),
        // An answer to a request sent before an invalidation still needs a fresh lookup.
        needsRefresh,
        registration: keep && reg ? { ...reg, analysisSeq: seq } : null,
        error: null,
      });
      if (needsRefresh && this.autoLookup && this.autoAfterInvalidate) {
        this.autoAfterInvalidate = false;
        this.scheduleAutoLookup();
      }
      this.hooks.announce?.("Lookup finished.");
      this.maybeSaveYield();
    } else {
      const patch: Partial<CardState> = { pending: null, error: { op: "analyze", error: reply.error } };
      const changed = reply.error.code === REGISTRY_CHANGED;
      if (changed) {
        patch.needsRefresh = true;
        patch.refreshAfterSeq = this.s.latestSeq;
      }
      this.update(patch);
      if (changed && this.autoLookup && this.autoAfterInvalidate) {
        this.autoAfterInvalidate = false;
        this.scheduleAutoLookup();
      }
      this.hooks.announce?.(`Lookup failed: ${reply.error.message}`);
      // The analysis shown before may still name the ID (nothing else runs now).
      this.maybeSaveYield();
    }
  }

  /** Sends `set_yield` when `yieldToSave` says so (one request at a time; never retried by itself). */
  private maybeSaveYield(): void {
    if (!this.saveYields || this.disposed) return;
    const params = yieldToSave(this.s);
    if (params) void this.sendYield(params);
  }

  private async sendYield(params: SetYieldParams): Promise<void> {
    const seq = this.s.yieldSync.seq + 1;
    this.update({ yieldSync: { ...this.s.yieldSync, seq, sending: { seq, params }, failed: null } });
    let reply: BackgroundReply<"set_yield">;
    try {
      reply = await this.client.request("set_yield", params);
    } catch (error) {
      reply = { ok: false, error: { code: "internal_error", message: String(error) } };
    }
    const sync = this.s.yieldSync;
    if (sync.sending?.seq !== seq) return;
    const done: YieldSync = { ...sync, sending: null };
    if (this.disposed) {
      this.s = { ...this.s, yieldSync: done };
      return;
    }
    // An answer for a place the card no longer shows (another product, experiment
    // or ID, or a structure that changed meanwhile) is not used.
    const input = this.s.input;
    const batch = yieldBatch(this.s);
    const current =
      input !== null &&
      input.experiment?.ref === params.experiment_ref &&
      input.product.productIndex === params.product_index &&
      batch !== null &&
      batch.full_id === params.id &&
      staleReason(this.s) === null;
    if (!current) {
      this.update({ yieldSync: done });
      this.maybeSaveYield();
      return;
    }
    const values: PageYields = { isolated: params.isolated, analytical: params.analytical };
    if (reply.ok && reply.result.id === params.id) {
      const stored = { id: params.id, yield: reply.result.yield, afterSeq: this.s.latestSeq };
      // Stored something else than was sent (should not happen): do not send the same values again and again.
      const failed = sameYields(values, reply.result.yield)
        ? null
        : { id: params.id, values, afterSeq: this.s.latestSeq, message: "The registry program stored other values." };
      this.update({ yieldSync: { ...done, stored, failed } });
    } else {
      const message = reply.ok ? "The registry program answered for another ID." : reply.error.message;
      this.update({ yieldSync: { ...done, failed: { id: params.id, values, afterSeq: this.s.latestSeq, message } } });
      this.hooks.announce?.(`The yield could not be saved: ${message}`);
    }
    // The page may show other values by now: one more request for that change.
    this.maybeSaveYield();
  }

  /**
   * The registry in use may have changed (a `registry_changed` answer to any
   * request): the shown analysis, and every answer to a request already sent,
   * needs a fresh lookup before an ID can be created.
   */
  invalidate(): void {
    this.update({ needsRefresh: true, refreshAfterSeq: this.s.latestSeq, confirming: null });
    if (this.autoLookup && this.autoAfterInvalidate && this.s.pending === null) {
      this.autoAfterInvalidate = false;
      this.scheduleAutoLookup();
    }
  }

  /** The user picked a stereo mode: re-analyse with it immediately. */
  async selectMode(mode: string): Promise<void> {
    if (this.s.pending || !isOverride(mode)) return;
    this.update({ selectedMode: mode });
    await this.lookUp();
  }

  /**
   * A name was stored for `coreStereoId` by a `set_name` sent while
   * `latestSeq` was `sentAfterSeq`. Only the shown name changes: the analysis
   * stays as it is (not stale), a confirmation stays open, a running register
   * keeps running.
   */
  applyName(coreStereoId: string, name: NameInfo | null, sentAfterSeq: number): void {
    this.update({ nameOverride: { coreStereoId, name, afterSeq: sentAfterSeq } });
  }

  setIgnoredTicked(index: number, ticked: boolean): void {
    const count = this.s.analysis?.result.structure.ignored_stereo.length ?? 0;
    if (index < 0 || index >= count) return;
    const next = this.s.ticked.slice();
    next[index] = ticked;
    this.update({ ticked: next, confirming: null });
  }

  /**
   * The "Create ID" button: with confirmBeforeCreate, show the confirmation;
   * otherwise send `register` right away (one click).
   */
  async create(): Promise<void> {
    if (this.confirmBeforeCreate) {
      this.requestCreate();
      return;
    }
    if (this.s.pending || this.s.confirming) return;
    const plan = buildPlan(this.s);
    if (plan) await this.sendRegister(plan);
  }

  /** First click on Create: show the confirmation; nothing is sent. */
  requestCreate(): void {
    if (this.s.pending || this.s.confirming) return;
    const plan = buildPlan(this.s);
    if (plan) this.update({ confirming: plan });
  }

  cancelCreate(): void {
    if (this.s.confirming) this.update({ confirming: null });
  }

  /** "Create" in the confirmation: sends `register` once. */
  async confirmCreate(): Promise<void> {
    const plan = this.s.confirming;
    if (!plan || this.s.pending) return;
    const fresh = buildPlan(this.s);
    if (!fresh || fresh.analysisSeq !== plan.analysisSeq || JSON.stringify(fresh.params) !== JSON.stringify(plan.params)) {
      this.update({ confirming: null });
      return;
    }
    await this.sendRegister(plan);
  }

  /** The product exactly as it was analysed (what `recheck` compares the page with). */
  private analysedProduct(plan: RegisterPlan): ProductInput | null {
    const analysis = this.s.analysis;
    if (!analysis || analysis.seq !== plan.analysisSeq) return null;
    const snap = analysis.snapshot;
    return {
      productIndex: snap.productIndex,
      label: this.s.input?.product.label ?? "",
      structure: structureParams(snap.structure),
      changeToken: snap.changeToken,
    };
  }

  private async sendRegister(plan: RegisterPlan): Promise<void> {
    if (this.s.pending) return;
    const seq = this.s.latestSeq + 1;
    const recheck = this.recheck;
    // Re-check and register count as ONE request: a second click meanwhile does nothing.
    this.update({
      latestSeq: seq,
      pending: recheck ? { op: "register", seq, phase: "recheck" } : { op: "register", seq },
      confirming: null,
      error: null,
      recheckNotice: null,
    });
    if (recheck) {
      this.hooks.announce?.("Checking the structure on the page…");
      const product = this.analysedProduct(plan);
      let outcome: RecheckOutcome;
      try {
        outcome = product ? await recheck(product) : { result: "changed" };
      } catch (error) {
        outcome = { result: "unavailable", message: String(error) };
      }
      if (this.disposed) {
        this.update({ pending: null });
        return;
      }
      if (outcome.result === "unavailable") {
        this.update({
          pending: null,
          recheckNotice: { kind: "unavailable", heading: RECHECK_UNAVAILABLE_HEADING, message: outcome.message },
        });
        this.hooks.announce?.(`${RECHECK_UNAVAILABLE_HEADING}: ${outcome.message}`);
        return;
      }
      // Still exactly the analysis on screen, and nothing else changed meanwhile?
      const fresh = buildPlan({ ...this.s, pending: null });
      const same =
        fresh !== null &&
        fresh.analysisSeq === plan.analysisSeq &&
        JSON.stringify(fresh.params) === JSON.stringify(plan.params);
      if (outcome.result === "changed" || !same) {
        this.update({
          pending: null,
          needsRefresh: true,
          recheckNotice: { kind: "changed", message: RECHECK_CHANGED_MESSAGE },
        });
        this.hooks.announce?.(RECHECK_CHANGED_MESSAGE);
        this.scheduleAutoLookup();
        return;
      }
      this.update({ pending: { op: "register", seq } });
    }
    this.hooks.announce?.("Creating the ID…");
    let reply: BackgroundReply<"register">;
    try {
      reply = await this.client.request("register", plan.params);
    } catch (error) {
      reply = { ok: false, error: { code: "internal_error", message: String(error) } };
    }
    // Never discard a register answer: an issued ID must always be shown.
    if (reply.ok) {
      this.update({
        pending: null,
        registration: { analysisSeq: plan.analysisSeq, plan, result: reply.result },
        error: null,
      });
      this.hooks.announce?.(
        reply.result.created ? `ID ${reply.result.id} created.` : `ID ${reply.result.id} already existed.`,
      );
    } else {
      this.update({ pending: null, needsRefresh: true, error: { op: "register", error: reply.error } });
      this.hooks.announce?.(`Creating the ID failed: ${reply.error.message}`);
    }
    const nameTarget = reply.ok && !this.disposed ? autoNameTarget(this.s) : null;
    // The new ID's batch: store the page's yields with it (before the lookup, so the lookup sees them).
    if (reply.ok) this.maybeSaveYield();
    if (reply.ok && !this.disposed && this.onRegistered) {
      const experiment: ExperimentContext = {
        ref: plan.experimentRef,
        code: plan.experimentCode,
        url: plan.params.experiment_url ?? null,
      };
      try {
        this.onRegistered(experiment, reply.result);
      } catch {
        // a hook never breaks the card
      }
    }
    // Show the registry's state after every register (read-only lookup).
    if (this.autoLookup && !this.disposed) void this.lookUp();
    // After the lookup was sent, so that a name stored from now on also covers its answer.
    if (nameTarget !== null) this.onRegisteredWithoutName?.(nameTarget, this.s.latestSeq);
  }
}
