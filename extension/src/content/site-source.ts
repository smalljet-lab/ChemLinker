/**
 * The normal product source: reads the open experiment from the Sciformation
 * page itself (see `extract.ts`) and gets each product's molfile through the
 * page bridge.
 *
 * The server's `smiles` attribute is NEVER used as a structure, and it is not
 * a sufficient change detector either (it cannot express everything the
 * registry keys contain). A row's molfile is read again whenever the row's
 * handle (ZK replaces the row nodes after every structure edit) or its
 * `smiles` attribute is new, and `changeToken` depends on the molfile text
 * itself. Before an ID is created, `recheck` reads the molfile once more.
 *
 * Each product also carries the yields its row shows (`ProductInput.yields`,
 * read with every pass over the page); they never change `changeToken`.
 */

import type { ProductInput, ProductSource, SkippedProduct, SourceState } from "../shared/adapter";
import { BRIDGE_MOLFILE_TIMEOUT_MS } from "../shared/bridge";
import { experimentPageUrl } from "../shared/import";
import type { BridgeClient, MolfileResult } from "./bridge-client";
import {
  defaultIsVisible,
  extractExperiment,
  rowHasStructure,
  type ExtractedRow,
  type Extraction,
  type IsVisible,
} from "./extract";

export const NO_STRUCTURE = "No structure is drawn for this product.";
export const READING = "Reading the structure…";
export const LAYOUT_CHANGED = "The page layout changed or the page is not ready";

export type RecheckResult = { result: "unchanged" | "changed" } | { result: "unavailable"; message: string };

export interface SiteSourceOptions {
  doc: Document;
  bridge: BridgeClient;
  isVisible?: IsVisible;
  /** Watches the document; calls `onChange` after mutations; returns a stop function. */
  observe?: (onChange: () => void) => () => void;
  /** Timer; returns a cancel function. */
  schedule?: (fn: () => void, ms: number) => () => void;
  debounceMs?: number;
  helloTimeoutMs?: number;
  helloAttempts?: number;
  molfileTimeoutMs?: number;
  /** How often a row is re-asked after "no_widget" for the same handle and smiles. */
  maxNoWidgetRetries?: number;
}

type HelloState = { kind: "unknown" } | { kind: "pending" } | { kind: "ok" } | { kind: "failed"; what: string };
type ExperimentRows = Extract<Extraction, { kind: "experiment" }>;

/** What is known about one product row (by product number) of the current experiment. */
interface RowState {
  /** Last molfile read for this row; kept while the row is read again. */
  molfile: string | null;
  /** The (handle, smiles) signature the last answer belongs to. */
  readFor: string | null;
  /** Message when the read for `readFor` failed. */
  failed: string | null;
  /** "no_widget" answers for the current signature. */
  noWidget: number;
}

/** Depends on the molfile TEXT: the same molfile read again changes nothing. */
export function productToken(key: string, index: number, molfile: string): string {
  return JSON.stringify([key, index, molfile]);
}

/** [key, index] of a token made by `productToken`, or null. */
export function parseProductToken(token: string): { key: string; index: number } | null {
  try {
    const v = JSON.parse(token) as unknown;
    if (Array.isArray(v) && typeof v[0] === "string" && typeof v[1] === "number") return { key: v[0], index: v[1] };
  } catch {
    // not ours
  }
  return null;
}

function signature(row: ExtractedRow & { handle: string; smiles: string }): string {
  return JSON.stringify([row.handle, row.smiles]);
}

function defaultObserve(doc: Document): (onChange: () => void) => () => void {
  return (onChange) => {
    const View = doc.defaultView?.MutationObserver ?? MutationObserver;
    const observer = new View(() => onChange());
    // `readonly` and `class` change when the experiment switches between edit
    // and view mode; `value` when the ELN sets a field's attribute. Rows are
    // re-rendered (childList) after a save.
    observer.observe(doc, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["smiles", "href", "title", "style", "class", "hidden", "id", "readonly", "value"],
      characterData: true,
    });
    return () => observer.disconnect();
  };
}

function defaultSchedule(fn: () => void, ms: number): () => void {
  const timer = setTimeout(fn, ms);
  return () => clearTimeout(timer);
}

export class SiteSource implements ProductSource {
  readonly name = "Sciformation page";
  readonly autoLookup = true;
  readonly confirmBeforeCreate = false;

  private readonly doc: Document;
  private readonly bridge: BridgeClient;
  private readonly isVisible: IsVisible;
  private readonly observe: (onChange: () => void) => () => void;
  private readonly schedule: (fn: () => void, ms: number) => () => void;
  private readonly debounceMs: number;
  private readonly helloTimeoutMs: number;
  private readonly helloAttempts: number;
  private readonly molfileTimeoutMs: number;
  private readonly maxNoWidgetRetries: number;

  private extraction: Extraction = { kind: "none" };
  private hello: HelloState = { kind: "unknown" };
  private rows = new Map<number, RowState>();
  private rowsKey: string | null = null;
  private queue: number[] = [];
  private inFlight: { index: number; sig: string } | null = null;
  /** Every molfile request goes through this chain: one at a time, also on this side. */
  private bridgeChain: Promise<unknown> = Promise.resolve();
  private state: SourceState = { kind: "idle", message: "No experiment is shown here." };
  private stateJson = JSON.stringify(this.state);
  private readonly listeners = new Set<() => void>();
  private stopObserving: (() => void) | null = null;
  private cancelRefresh: (() => void) | null = null;
  private stopped = false;

  constructor(options: SiteSourceOptions) {
    this.doc = options.doc;
    this.bridge = options.bridge;
    this.isVisible = options.isVisible ?? defaultIsVisible;
    this.observe = options.observe ?? defaultObserve(options.doc);
    this.schedule = options.schedule ?? defaultSchedule;
    this.debounceMs = options.debounceMs ?? 250;
    this.helloTimeoutMs = options.helloTimeoutMs ?? 2000;
    this.helloAttempts = options.helloAttempts ?? 3;
    this.molfileTimeoutMs = options.molfileTimeoutMs ?? BRIDGE_MOLFILE_TIMEOUT_MS + 5000;
    this.maxNoWidgetRetries = options.maxNoWidgetRetries ?? 3;
  }

  /** Starts watching the page and reads it once. */
  start(): void {
    if (this.stopObserving || this.stopped) return;
    this.stopObserving = this.observe(() => this.scheduleRefresh());
    this.refresh();
  }

  stop(): void {
    this.stopped = true;
    this.stopObserving?.();
    this.stopObserving = null;
    this.cancelRefresh?.();
    this.cancelRefresh = null;
    this.queue = [];
  }

  getState(): SourceState {
    return this.state;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Debounced re-read: a burst of page mutations gives one read. */
  scheduleRefresh(): void {
    if (this.stopped) return;
    this.cancelRefresh?.();
    this.cancelRefresh = this.schedule(() => {
      this.cancelRefresh = null;
      this.refresh();
    }, this.debounceMs);
  }

  /** Reads the page now. */
  refresh(): void {
    if (this.stopped) return;
    const ex = extractExperiment(this.doc, this.isVisible);
    this.extraction = ex;
    if (ex.kind === "experiment") {
      if (ex.key !== this.rowsKey) {
        // Another experiment: forget everything about the previous one.
        this.rowsKey = ex.key;
        this.rows = new Map();
        this.queue = [];
      }
      if (this.hello.kind === "unknown") void this.sayHello();
      if (this.hello.kind === "ok") this.enqueueChanged(ex);
    }
    this.publish();
  }

  /**
   * Reads this product's molfile from the page NOW (no cache) and compares it
   * with `product.structure.molfile` exactly. Called right before an ID is created.
   */
  async recheck(product: ProductInput): Promise<RecheckResult> {
    if (this.stopped) return { result: "unavailable", message: "The page is no longer watched." };
    const wanted = parseProductToken(product.changeToken);
    for (let attempt = 0; attempt < 2; attempt++) {
      this.refresh();
      const ex = this.extraction;
      if (!wanted || ex.kind !== "experiment" || ex.key !== wanted.key) return { result: "changed" };
      const row = ex.rows[wanted.index - 1];
      if (!row || row.index !== wanted.index || !rowHasStructure(row)) return { result: "changed" };
      const sig = signature(row);
      const answer = await this.readMolfile(row.handle);
      if (this.stopped) return { result: "unavailable", message: "The page is no longer watched." };
      if (!answer.ok) {
        // The row was re-rendered meanwhile: read the page again and try once more.
        if (answer.code === "no_widget" && attempt === 0) continue;
        return { result: "unavailable", message: answer.message };
      }
      // Keep what was read (it is the freshest), then compare.
      this.storeRead(ex.key, wanted.index, sig, answer.molfile);
      this.publish();
      return answer.molfile === product.structure.molfile ? { result: "unchanged" } : { result: "changed" };
    }
    return { result: "unavailable", message: "The structure was re-rendered while it was being read." };
  }

  private readMolfile(handle: string): Promise<MolfileResult> {
    const next = this.bridgeChain.then(() => this.bridge.molfile(handle, this.molfileTimeoutMs));
    this.bridgeChain = next.catch(() => undefined);
    return next.catch((error: unknown) => ({ ok: false as const, code: "unexpected" as const, message: String(error) }));
  }

  private storeRead(key: string, index: number, sig: string, molfile: string): void {
    if (this.rowsKey !== key) return;
    this.rows.set(index, { molfile, readFor: sig, failed: null, noWidget: 0 });
  }

  private async sayHello(): Promise<void> {
    this.hello = { kind: "pending" };
    let answer: boolean | null = null;
    for (let i = 0; i < this.helloAttempts && answer === null && !this.stopped; i++) {
      answer = await this.bridge.hello(this.helloTimeoutMs);
    }
    if (answer === true) this.hello = { kind: "ok" };
    else if (answer === false) this.hello = { kind: "failed", what: "the ELN's page scripts (ZK) were not found" };
    else this.hello = { kind: "failed", what: "the extension's page script did not answer" };
    this.refresh();
  }

  /** Queues every row whose handle or smiles attribute is new since its last read. */
  private enqueueChanged(ex: ExperimentRows): void {
    for (const row of ex.rows) {
      if (!rowHasStructure(row)) {
        this.rows.delete(row.index);
        continue;
      }
      const sig = signature(row);
      const known = this.rows.get(row.index);
      if (known?.readFor === sig) continue;
      if (this.inFlight?.index === row.index && this.inFlight.sig === sig) continue;
      if (!this.queue.includes(row.index)) this.queue.push(row.index);
    }
    for (const index of [...this.rows.keys()]) if (index > ex.rows.length) this.rows.delete(index);
    this.pump();
  }

  /** Asks the bridge for one molfile at a time (the bridge's download hook is global). */
  private pump(): void {
    if (this.inFlight !== null || this.stopped) return;
    while (this.queue.length > 0) {
      const index = this.queue.shift()!;
      const ex = this.extraction;
      if (ex.kind !== "experiment") continue;
      // Use the handle the page has NOW (rows are re-rendered with new ids).
      const row = ex.rows[index - 1];
      if (!row || !rowHasStructure(row)) continue;
      const sig = signature(row);
      if (this.rows.get(index)?.readFor === sig) continue;
      const key = ex.key;
      this.inFlight = { index, sig };
      void this.readMolfile(row.handle).then((result) => {
        this.inFlight = null;
        if (this.stopped) return;
        if (this.rowsKey === key) this.applyRead(index, sig, result);
        if (!result.ok && result.code === "no_widget") this.scheduleRefresh();
        else this.refresh();
        this.pump();
      });
      return;
    }
  }

  private applyRead(index: number, sig: string, result: MolfileResult): void {
    // An answer for a row that changed again meanwhile is not used; the row is read again.
    const ex = this.extraction;
    const row = ex.kind === "experiment" ? ex.rows[index - 1] : undefined;
    if (!row || !rowHasStructure(row) || signature(row) !== sig) return;
    const known = this.rows.get(index) ?? { molfile: null, readFor: null, failed: null, noWidget: 0 };
    if (result.ok) {
      this.rows.set(index, { molfile: result.molfile, readFor: sig, failed: null, noWidget: 0 });
    } else if (result.code === "no_widget") {
      const tries = known.noWidget + 1;
      this.rows.set(
        index,
        tries > this.maxNoWidgetRetries
          ? { molfile: null, readFor: sig, failed: result.message, noWidget: tries }
          : { ...known, noWidget: tries },
      );
    } else {
      // The current structure is unknown: do not keep offering the previous one.
      this.rows.set(index, { molfile: null, readFor: sig, failed: result.message, noWidget: 0 });
    }
  }

  private compute(): SourceState {
    const ex = this.extraction;
    if (ex.kind === "none") return { kind: "idle", message: "No experiment is shown here." };
    if (ex.kind === "error") return { kind: "error", message: `${LAYOUT_CHANGED}: ${ex.what}.` };
    if (this.hello.kind === "failed") return { kind: "error", message: `${LAYOUT_CHANGED}: ${this.hello.what}.` };

    const products: ProductInput[] = [];
    const skipped: SkippedProduct[] = [];
    for (const row of ex.rows) {
      const label = `Product ${row.index}`;
      if (!rowHasStructure(row)) {
        skipped.push({ productIndex: row.index, label, reason: NO_STRUCTURE });
        continue;
      }
      const known = this.rows.get(row.index);
      if (known?.failed && known.readFor === signature(row)) {
        skipped.push({
          productIndex: row.index,
          label,
          reason: `The structure could not be read from the page: ${known.failed} The manual input (under Advanced) can be used instead.`,
        });
      } else if (known?.molfile != null) {
        // Also while the row is being read again: keep the previous molfile (no flicker).
        // The yields are read on every pass and do not enter the token: a change
        // of the yields alone publishes a new state, but nothing is read or analysed again.
        products.push({
          productIndex: row.index,
          label,
          structure: { molfile: known.molfile },
          changeToken: productToken(ex.key, row.index, known.molfile),
          yields: row.yields,
        });
      } else {
        skipped.push({ productIndex: row.index, label, reason: READING, pending: true });
      }
    }
    const state: SourceState = {
      kind: "ready",
      experiment: { ref: ex.key, code: ex.code, url: experimentPageUrl(ex.key) },
      products,
    };
    if (skipped.length > 0) state.skipped = skipped;
    if (ex.rows.length === 0) state.notice = "This experiment has no products.";
    return state;
  }

  private publish(): void {
    const next = this.compute();
    const json = JSON.stringify(next);
    if (json === this.stateJson) return;
    this.state = next;
    this.stateJson = json;
    for (const listener of [...this.listeners]) listener();
  }
}
