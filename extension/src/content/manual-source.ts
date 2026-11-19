/**
 * Fallback product source: a structure pasted or dropped by hand plus the
 * experiment fields typed by the user. No DOM here; `ui/manual-form.ts` binds
 * the form to it.
 */

import type { ExperimentContext, ProductSource, SourceState } from "../shared/adapter";
import { MAX_PRODUCT_INDEX, MAX_REF_TEXT, MAX_STRUCTURE_CHARS } from "../shared/protocol";
import { detectStructure, type DetectedStructure } from "./structure-kind";

export const EXPERIMENT_KEY_NEEDED = "An experiment key is needed to create an ID (lookups work without one).";
export const EXPERIMENT_KEY_NOT_A_NUMBER =
  "The experiment key is the number in the experiment's link (pk=...), not the ELN number. " +
  "Without it no ID can be created (lookups work).";

/** The ELN's internal experiment key, as the page source reads it from the experiment's links. */
const EXPERIMENT_KEY = /^\d{1,18}$/;

/** What `loadFile` needs from a `File`. */
export interface FileLike {
  readonly name: string;
  readonly size: number;
  text(): Promise<string>;
}

export type FileResult = { ok: true } | { ok: false; message: string };

/** Parses the "Product no." field: a whole number 1..MAX_PRODUCT_INDEX, or null. */
export function parseProductIndex(text: string): number | null {
  const t = text.trim();
  if (!/^\d{1,4}$/.test(t)) return null;
  const n = Number(t);
  return n >= 1 && n <= MAX_PRODUCT_INDEX ? n : null;
}

export class ManualSource implements ProductSource {
  readonly name = "Manual input";
  /** The user presses "Look up"; nothing is analysed while typing. */
  readonly autoLookup = false;
  /** The experiment is typed by hand, so "Create ID" first asks for confirmation. */
  readonly confirmBeforeCreate = true;

  private text = "";
  private fileName: string | null = null;
  private experimentKey = "";
  private experimentCode = "";
  private productIndexText = "1";
  private structureVersion = 0;
  private state: SourceState;
  private readonly listeners = new Set<() => void>();

  constructor() {
    this.state = this.compute();
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

  /** The structure text exactly as it will be sent (molfiles verbatim). */
  get structureText(): string {
    return this.text;
  }

  get loadedFileName(): string | null {
    return this.fileName;
  }

  detected(): DetectedStructure {
    return detectStructure(this.text);
  }

  /** Text typed or pasted into the textarea. */
  setStructureText(text: string): void {
    if (text === this.text && this.fileName === null) return;
    this.text = text;
    this.fileName = null;
    this.structureVersion += 1;
    this.changed();
  }

  /** Reads one `.mol` file; its text is kept byte-for-byte as decoded. */
  async loadFile(file: FileLike): Promise<FileResult> {
    if (!/\.mol$/i.test(file.name)) {
      return { ok: false, message: `"${file.name}" is not a .mol file.` };
    }
    if (file.size > MAX_STRUCTURE_CHARS) {
      return { ok: false, message: `"${file.name}" is larger than ${MAX_STRUCTURE_CHARS} characters.` };
    }
    let text: string;
    try {
      text = await file.text();
    } catch (error) {
      return { ok: false, message: `"${file.name}" could not be read: ${String(error)}` };
    }
    if (text.length > MAX_STRUCTURE_CHARS) {
      return { ok: false, message: `"${file.name}" is larger than ${MAX_STRUCTURE_CHARS} characters.` };
    }
    if (text.trim() === "") {
      return { ok: false, message: `"${file.name}" is empty.` };
    }
    this.text = text;
    this.fileName = file.name;
    this.structureVersion += 1;
    this.changed();
    return { ok: true };
  }

  setExperimentKey(value: string): void {
    if (value === this.experimentKey) return;
    this.experimentKey = value;
    this.changed();
  }

  setExperimentCode(value: string): void {
    if (value === this.experimentCode) return;
    this.experimentCode = value;
    this.changed();
  }

  setProductIndex(value: string): void {
    if (value === this.productIndexText) return;
    this.productIndexText = value;
    this.changed();
  }

  private changed(): void {
    this.state = this.compute();
    for (const listener of [...this.listeners]) listener();
  }

  private compute(): SourceState {
    const detected = detectStructure(this.text);
    if (detected.kind === "empty") {
      return { kind: "idle", message: "Paste a molfile or SMILES, or drop a .mol file." };
    }
    if (detected.kind === "too_large") {
      return {
        kind: "error",
        message: `The structure has ${detected.length} characters; the limit is ${MAX_STRUCTURE_CHARS}.`,
      };
    }
    const productIndex = parseProductIndex(this.productIndexText);
    if (productIndex === null) {
      return { kind: "error", message: `Product no. must be a whole number from 1 to ${MAX_PRODUCT_INDEX}.` };
    }
    const product = {
      productIndex,
      structure: detected.structure,
      label: this.fileName ?? (detected.kind === "molfile" ? "Pasted molfile" : "Pasted SMILES"),
      changeToken: `manual-${this.structureVersion}`,
    };

    const key = this.experimentKey.trim();
    const code = this.experimentCode.trim();
    let experiment: ExperimentContext | null = null;
    let notice: string | undefined;
    if (key === "") {
      notice = EXPERIMENT_KEY_NEEDED;
    } else if (!EXPERIMENT_KEY.test(key)) {
      // The key is what makes a second "Create ID" for the same product find the first:
      // anything else than the ELN's own key (e.g. the ELN number) would register it apart.
      notice = EXPERIMENT_KEY_NOT_A_NUMBER;
    } else if (code.length > MAX_REF_TEXT) {
      notice = `The experiment code is longer than ${MAX_REF_TEXT} characters.`;
    } else {
      experiment = { ref: key, code: code === "" ? null : code, url: null };
    }
    const state: SourceState = { kind: "ready", experiment, products: [product] };
    if (notice !== undefined) state.notice = notice;
    return state;
  }
}
