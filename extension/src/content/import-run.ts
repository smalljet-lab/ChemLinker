/**
 * The content script's half of the import of many experiments (the contract
 * is `shared/import.ts`): answers the compounds page's "import-status" and
 * "import-apply" messages for the experiment this frame shows.
 *
 * - Only the compounds page of this extension is served (`isImportSender`);
 *   every other sender gets no answer at all.
 * - A frame that shows no experiment (source state "idle") never answers, so
 *   that the experiment frame's answer is the one delivered.
 * - An apply looks every product up (`analyze`) and creates IDs (`register`)
 *   exactly as the panel's "Create ID" does: the molfile the page source read,
 *   the stereo mode as detected, `confirm` copied from that analysis, and a
 *   re-read of the structure from the page (`recheck`) right before `register`.
 *   Anything that needs a human decision is left to the panel ("attention").
 * - One apply at a time per frame; nothing is ever retried by itself.
 *
 * No DOM; the host is reached only through `HostClient`.
 */

import type { ExperimentContext, PageYields, ProductInput, SourceState } from "../shared/adapter";
import {
  MAX_IMPORT_PRODUCTS,
  type ImportApplyReply,
  type ImportApplyRequest,
  type ImportOutcome,
  type ImportPageProduct,
  type ImportReactionResult,
  type ImportStatusReply,
  type ImportStatusRequest,
  type ImportYieldResult,
} from "../shared/import";
import {
  MAX_PRODUCT_INDEX,
  MAX_REF_TEXT,
  type AnalyzeParams,
  type AnalyzeResult,
  type BackgroundReply,
  type Batch,
  type Op,
  type ParamsByOp,
  type RegisterParams,
  type StructureInput,
} from "../shared/protocol";
import type { HostClient } from "./host-client";
import type { ReactionStoreOutcome } from "./reaction-capture";
import { experimentLink } from "./safe-values";
import type { RecheckResult } from "./site-source";
import { roundedYields, sameYields } from "./yields";

// ------------------------------------------------------------------ senders and messages

/** The fields of `runtime.MessageSender` looked at. */
export interface ImportSender {
  id?: string | undefined;
  url?: string | undefined;
}

/** The URL without its query and fragment. */
function withoutQuery(url: string): string {
  const cut = url.search(/[?#]/);
  return cut < 0 ? url : url.slice(0, cut);
}

/**
 * True only for this extension's compounds page: `sender.id` is `runtimeId`
 * and `sender.url` without query and fragment is `pageUrl`
 * (`browser.runtime.getURL(COMPOUNDS_PAGE)`).
 */
export function isImportSender(sender: ImportSender | undefined | null, runtimeId: string, pageUrl: string): boolean {
  if (!sender || typeof sender.id !== "string" || sender.id === "" || sender.id !== runtimeId) return false;
  if (typeof sender.url !== "string" || pageUrl === "") return false;
  return withoutQuery(sender.url) === pageUrl;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isRef(value: unknown): value is string {
  return typeof value === "string" && value !== "" && value.length <= MAX_REF_TEXT;
}

/** The status request when `message` is a well-formed one; null otherwise. */
export function readStatusRequest(message: unknown): ImportStatusRequest | null {
  if (!isRecord(message) || message.type !== "import-status" || !isRef(message.ref)) return null;
  return { type: "import-status", ref: message.ref };
}

export const BAD_REF_MESSAGE = `The import request names no experiment (or a key longer than ${MAX_REF_TEXT} characters).`;
export const BAD_PRODUCTS_MESSAGE =
  `The import request must list at most ${MAX_IMPORT_PRODUCTS} different product numbers, ` +
  `each a whole number from 1 to ${MAX_PRODUCT_INDEX}.`;
export const BAD_FLAGS_MESSAGE = "The import request's options (dry_run, allow_warnings, names) must be true or false.";

/** A clean copy of a well-formed apply request, or why it is not one. */
export function readApplyRequest(message: unknown): ImportApplyRequest | { error: string } {
  if (!isRecord(message) || message.type !== "import-apply") return { error: "Unknown message type." };
  if (!isRef(message.ref)) return { error: BAD_REF_MESSAGE };
  const products = message.products;
  if (
    !Array.isArray(products) ||
    products.length > MAX_IMPORT_PRODUCTS ||
    !products.every((n) => typeof n === "number" && Number.isSafeInteger(n) && n >= 1 && n <= MAX_PRODUCT_INDEX) ||
    new Set(products).size !== products.length
  ) {
    return { error: BAD_PRODUCTS_MESSAGE };
  }
  const { dry_run, allow_warnings, names } = message;
  if (typeof dry_run !== "boolean" || typeof allow_warnings !== "boolean" || typeof names !== "boolean") {
    return { error: BAD_FLAGS_MESSAGE };
  }
  return { type: "import-apply", ref: message.ref, products: [...(products as number[])], dry_run, allow_warnings, names };
}

// ------------------------------------------------------------------ status

/**
 * What the frame answers to "import-status" for `ref`; null: no answer (the
 * frame shows no experiment).
 */
export function importStatus(state: SourceState, ref: string): ImportStatusReply | null {
  if (state.kind === "idle") return null;
  if (state.kind === "error") return { state: "error", message: state.message };
  const experiment = state.experiment;
  if (experiment === null) {
    // The site source always knows the experiment's key; without it nothing could be created.
    return { state: "error", message: state.notice ?? "The experiment shown has no key." };
  }
  if (experiment.ref !== ref) return { state: "other", ref: experiment.ref };
  const skipped = state.skipped ?? [];
  if (skipped.some((s) => s.pending === true)) return { state: "reading" };
  const rows: ImportPageProduct[] = [
    ...state.products.map((p) => ({
      index: p.productIndex,
      structure: true,
      problem: null,
      yields: copyYields(p.yields),
    })),
    ...skipped.map((s) => ({ index: s.productIndex, structure: false, problem: s.reason, yields: null })),
  ];
  rows.sort((a, b) => a.index - b.index);
  return { state: "ready", experiment: { ref: experiment.ref, code: experiment.code }, products: rows };
}

function copyYields(yields: PageYields | null | undefined): PageYields | null {
  return yields === null || yields === undefined ? null : { isolated: yields.isolated, analytical: yields.analytical };
}

// ------------------------------------------------------------------ apply

/** What the import needs of the page source (`SiteSource`). */
export interface ImportSource {
  getState(): SourceState;
  recheck(product: ProductInput): Promise<RecheckResult>;
}

/** Stores the reaction the page shows for `experiment` once and says what happened (`storeReactionOnce`). */
export type StoreReaction = (experiment: ExperimentContext) => Promise<ReactionStoreOutcome>;

export interface ImportResponderOptions {
  source: ImportSource;
  client: HostClient;
  storeReaction: StoreReaction;
  /** `browser.runtime.id`. */
  runtimeId: string;
  /** `browser.runtime.getURL(COMPOUNDS_PAGE)`. */
  pageUrl: string;
}

export const BUSY_MESSAGE = "An import is already running for this experiment.";
export const READING_MESSAGE = "The products of the experiment are still being read from the page.";
export const NOT_SHOWN_MESSAGE = "The experiment is no longer shown in this tab.";
export const RECHECK_CHANGED_IMPORT =
  "The structure on the page changed while it was being handled, so no ID was created. " +
  "Open the experiment and check it in the panel.";

function quoteList(items: readonly string[]): string {
  return items.join("; ");
}

/** Why the analysis needs a human decision (contract step 3), in plain words; null when it does not. */
export function attentionMessage(result: AnalyzeResult, allowWarnings: boolean): string | null {
  const conflict = result.slot_conflict;
  if (conflict) {
    return (
      `This product's row already holds ${conflict.full_id} for another structure. ` +
      "Open the experiment and decide in the panel."
    );
  }
  const ignored = result.structure.ignored_stereo;
  if (ignored.length > 0) {
    return (
      `The drawing contains stereo information the registry does not use (${quoteList(ignored)}). ` +
      "Open the experiment and accept it in the panel."
    );
  }
  const warnings = result.structure.warnings;
  if (warnings.length > 0 && !allowWarnings) {
    return `The structure has warnings (${quoteList(warnings)}). Open the experiment and check it in the panel.`;
  }
  return null;
}

function blankOutcome(index: number, yields: PageYields | null): ImportOutcome {
  return {
    index,
    kind: "failed",
    id: null,
    core_stereo_id: null,
    status: null,
    compound_key: null,
    inchikey: null,
    formula: null,
    stereo_mode: null,
    core_created: false,
    stereo_created: false,
    message: null,
    notes: [],
    yields,
    yield_result: "none",
    name: null,
  };
}

function structureOf(input: StructureInput): StructureInput {
  return input.molfile !== undefined ? { molfile: input.molfile } : { smiles: input.smiles };
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function addNote(notes: string[], note: string | null | undefined): void {
  if (typeof note === "string" && note.trim() !== "" && !notes.includes(note)) notes.push(note);
}

/** The shown experiment when the state is "ready" for `ref` with every product read; else why not. */
function readyExperiment(state: SourceState, ref: string): { experiment: ExperimentContext } | { message: string } {
  const status = importStatus(state, ref);
  if (status === null) return { message: "No experiment is shown in this tab." };
  switch (status.state) {
    case "error":
      return { message: status.message };
    case "other":
      return { message: `This tab shows another experiment (${status.ref}), not ${ref}.` };
    case "reading":
      return { message: READING_MESSAGE };
    case "ready":
      return { experiment: (state as Extract<SourceState, { kind: "ready" }>).experiment! };
  }
}

export class ImportResponder {
  private readonly source: ImportSource;
  private readonly client: HostClient;
  private readonly storeReaction: StoreReaction;
  private readonly runtimeId: string;
  private readonly pageUrl: string;
  private busy = false;

  constructor(options: ImportResponderOptions) {
    this.source = options.source;
    this.client = options.client;
    this.storeReaction = options.storeReaction;
    this.runtimeId = options.runtimeId;
    this.pageUrl = options.pageUrl;
  }

  /** True while an apply runs in this frame. */
  isRunning(): boolean {
    return this.busy;
  }

  /**
   * Decides SYNCHRONOUSLY whether this frame answers `message`: null = no
   * answer at all; otherwise the answer, which never rejects.
   */
  answer(message: unknown, sender: ImportSender | undefined | null): Promise<ImportStatusReply | ImportApplyReply> | null {
    if (!isImportSender(sender, this.runtimeId, this.pageUrl) || !isRecord(message)) return null;
    if (message.type === "import-status") {
      const request = readStatusRequest(message);
      if (request === null) return null;
      const reply = importStatus(this.source.getState(), request.ref);
      return reply === null ? null : Promise.resolve(reply);
    }
    if (message.type === "import-apply") {
      if (this.busy) return Promise.resolve({ ok: false, message: BUSY_MESSAGE });
      // A frame without an experiment does not answer, whatever the request holds.
      if (this.source.getState().kind === "idle") return null;
      const request = readApplyRequest(message);
      if ("error" in request) return Promise.resolve({ ok: false, message: request.error });
      return this.apply(request);
    }
    return null;
  }

  /** Runs one apply (contract steps 1 to 7 and the reaction). Never rejects. */
  async apply(request: ImportApplyRequest): Promise<ImportApplyReply> {
    if (this.busy) return { ok: false, message: BUSY_MESSAGE };
    const shown = readyExperiment(this.source.getState(), request.ref);
    if ("message" in shown) return { ok: false, message: shown.message };
    this.busy = true;
    try {
      const experiment = shown.experiment;
      const outcomes: ImportOutcome[] = [];
      for (const index of [...request.products].sort((a, b) => a - b)) {
        let outcome: ImportOutcome;
        try {
          outcome = await this.handleProduct(index, request);
        } catch (error) {
          outcome = { ...blankOutcome(index, null), message: `Unexpected error in the extension: ${errorText(error)}` };
        }
        outcomes.push(outcome);
      }
      const reaction = await this.reactionStep(request, outcomes);
      return {
        ok: true,
        experiment: { ref: experiment.ref, code: experiment.code },
        products: outcomes,
        reaction: reaction.result,
        reaction_message: reaction.message,
      };
    } catch (error) {
      return { ok: false, message: `Unexpected error in the extension: ${errorText(error)}` };
    } finally {
      this.busy = false;
    }
  }

  private async call<K extends Op>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>> {
    try {
      return await this.client.request(op, params);
    } catch (error) {
      return { ok: false, error: { code: "internal_error", message: errorText(error) } };
    }
  }

  private async handleProduct(index: number, request: ImportApplyRequest): Promise<ImportOutcome> {
    // The page as it is now (the yields are read with every pass over the page).
    const state = this.source.getState();
    if (state.kind !== "ready" || state.experiment === null || state.experiment.ref !== request.ref) {
      return { ...blankOutcome(index, null), message: NOT_SHOWN_MESSAGE };
    }
    const experiment = state.experiment;
    const product = state.products.find((p) => p.productIndex === index);
    if (!product) {
      const skipped = state.skipped?.find((s) => s.productIndex === index);
      return {
        ...blankOutcome(index, null),
        message: skipped ? skipped.reason : `The experiment shows no product ${index}.`,
      };
    }
    const yields = copyYields(product.yields);
    const out = blankOutcome(index, yields);
    const structure = structureOf(product.structure);

    // 1. Look it up: the stereo mode as detected, this experiment and product number.
    const analyzeParams: AnalyzeParams = { ...structure, mode: null, experiment_ref: experiment.ref, product_index: index };
    const analyzed = await this.call("analyze", analyzeParams);
    if (!analyzed.ok) return { ...out, message: analyzed.error.message };
    const result = analyzed.result;
    const info = result.structure;
    out.status = result.status;
    out.compound_key = info.key_digest;
    out.inchikey = info.inchikey;
    // Everything drawn (a salt with its counter-ion), as the ELN's list shows it.
    out.formula = info.properties?.sum_formula ?? info.formula;
    out.stereo_mode = info.stereo_mode;
    out.core_stereo_id = result.core_stereo_id;
    out.name = result.name?.text ?? null;
    for (const warning of info.warnings) addNote(out.notes, warning);

    // 2. Already registered here.
    if (result.status === "registered_here") {
      out.kind = "exists";
      out.id = result.id;
      if (!request.dry_run && result.id !== null) {
        const batch = result.batches.find((b) => b.full_id === result.id) ?? null;
        out.yield_result = await this.syncYield(batch, yields, experiment.ref, index, out.notes);
      }
      return out;
    }

    // 3. A human decision is needed.
    const attention = attentionMessage(result, request.allow_warnings);
    if (attention !== null) return { ...out, kind: "attention", message: attention };

    // 4. Look up only.
    if (request.dry_run) return { ...out, kind: "would_create" };

    // 5. The structure on the page must still be exactly what was analysed.
    let recheck: RecheckResult;
    try {
      recheck = await this.source.recheck({
        productIndex: index,
        label: product.label,
        structure,
        changeToken: product.changeToken,
      });
    } catch (error) {
      recheck = { result: "unavailable", message: errorText(error) };
    }
    if (recheck.result === "changed") return { ...out, message: RECHECK_CHANGED_IMPORT };
    if (recheck.result === "unavailable") {
      return { ...out, message: `The structure could not be re-checked on the page, so no ID was created: ${recheck.message}` };
    }
    const registerParams: RegisterParams = {
      ...structure,
      mode: null,
      experiment_ref: experiment.ref,
      experiment_code: experiment.code,
      experiment_url: experimentLink(experiment.url),
      product_index: index,
      confirm: { inchikey: info.inchikey, stereo_mode: info.stereo_mode, key_digest: info.key_digest },
    };
    const registered = await this.call("register", registerParams);
    if (!registered.ok) return { ...out, message: registered.error.message };
    const reg = registered.result;
    out.kind = "created";
    out.id = reg.id;
    out.core_stereo_id = reg.batch.core_stereo_id;
    out.core_created = reg.core_created;
    out.stereo_created = reg.stereo_created;
    // The name read with the analysis belongs to the analysis' CORE_STEREO only.
    out.name = reg.batch.core_stereo_id === result.core_stereo_id ? (result.name?.text ?? null) : null;
    for (const warning of reg.warnings) addNote(out.notes, warning);
    addNote(out.notes, reg.backup.warning);

    // 6. The page's yields with the new ID.
    const batch = reg.batch.full_id === reg.id ? reg.batch : null;
    out.yield_result = await this.syncYield(batch, yields, experiment.ref, index, out.notes);

    // 7. A name for a new stereo variant.
    if (request.names && reg.stereo_created) {
      const named = await this.call("generate_name", { core_stereo_id: reg.batch.core_stereo_id, replace: false });
      if (named.ok) {
        if (named.result.core_stereo_id === reg.batch.core_stereo_id) out.name = named.result.name?.text ?? out.name;
      } else {
        addNote(out.notes, `No name could be got from the naming program: ${named.error.message}`);
      }
    }
    return out;
  }

  /**
   * Step 6: `set_yield` when the row's yields are usable and differ from what
   * the registry holds for `batch` (both empty on the page while the registry
   * holds a yield counts as different: the registry mirrors the page). The
   * same conditions as the panel's `yieldToSave`.
   */
  private async syncYield(
    batch: Batch | null,
    yields: PageYields | null,
    ref: string,
    index: number,
    notes: string[],
  ): Promise<ImportYieldResult> {
    if (yields === null || batch === null || batch.retired) return "none";
    if (batch.experiment_ref !== ref || batch.product_index !== index) return "none";
    // An older host does not report yields: nothing to compare with.
    if (batch.yield === undefined) return "none";
    if (sameYields(yields, batch.yield)) return "unchanged";
    const values = roundedYields(yields);
    const reply = await this.call("set_yield", {
      id: batch.full_id,
      experiment_ref: ref,
      product_index: index,
      isolated: values.isolated,
      analytical: values.analytical,
    });
    let problem: string | null = null;
    if (!reply.ok) problem = reply.error.message;
    else if (reply.result.id !== batch.full_id) problem = "The registry program answered for another ID.";
    else if (!sameYields(values, reply.result.yield)) problem = "The registry program stored other values.";
    if (problem !== null) {
      addNote(notes, `The yield could not be saved: ${problem}`);
      return "failed";
    }
    return reply.ok && reply.result.changed ? "saved" : "unchanged";
  }

  /** After the products: the reaction, once, when a product has an ID (not in a dry run). */
  private async reactionStep(
    request: ImportApplyRequest,
    outcomes: readonly ImportOutcome[],
  ): Promise<{ result: ImportReactionResult; message: string | null }> {
    if (request.dry_run || !outcomes.some((o) => o.kind === "exists" || o.kind === "created")) {
      return { result: "none", message: null };
    }
    const state = this.source.getState();
    if (state.kind !== "ready" || state.experiment === null || state.experiment.ref !== request.ref) {
      return { result: "failed", message: `The reaction was not stored: ${NOT_SHOWN_MESSAGE.toLowerCase()}` };
    }
    let stored: ReactionStoreOutcome;
    try {
      stored = await this.storeReaction(state.experiment);
    } catch (error) {
      stored = { result: "failed", message: `The reaction could not be saved: ${errorText(error)}` };
    }
    return stored.result === "stored" ? { result: "stored", message: stored.message } : { result: "failed", message: stored.message };
  }
}

// ------------------------------------------------------------------ the listener

/**
 * The `runtime.onMessage` listener. It answers through `sendResponse` and
 * returns `true` only when this frame answers; otherwise it returns `false`
 * at once (no answer from this frame). Never returns a Promise: Chrome does
 * not reliably take one from an onMessage listener.
 */
export function importMessageListener(
  responder: Pick<ImportResponder, "answer">,
): (message: unknown, sender: ImportSender | undefined, sendResponse: (reply: unknown) => void) => boolean {
  return (message, sender, sendResponse) => {
    let answer: Promise<unknown> | null;
    try {
      answer = responder.answer(message, sender);
    } catch {
      return false;
    }
    if (answer === null) return false;
    const reply = (value: unknown): void => {
      try {
        sendResponse(value);
      } catch {
        // The page is gone; nobody waits for the answer.
      }
    };
    answer.then(reply, (error: unknown) => reply({ ok: false, message: `Unexpected error in the extension: ${errorText(error)}` }));
    return true;
  };
}
