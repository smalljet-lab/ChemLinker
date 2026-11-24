/**
 * "Spectra and files" of the stereo variants shown on the compounds page:
 * the variant's folder on the registry's PC, the spectra recorded for its
 * batches (by the MestReNova add-on) and the files lying in the folder
 * (`compound_files`, read-only). Loaded when the details are opened, one
 * request per variant; only the answer to the latest request of a variant is
 * used. Plus the texts the section shows. No DOM.
 */

import type { CompoundFilesResult, FolderFile, HostError, SpectrumInfo } from "../shared/protocol";
import type { HostClient } from "../content/host-client";
import { shortDate } from "../content/labels";
import type { CompoundsHooks } from "./state";

export interface FilesEntry {
  /** True while a request runs (the previous answer, if any, stays shown). */
  loading: boolean;
  result: CompoundFilesResult | null;
  error: HostError | null;
}

export const NO_SPECTRA_TEXT =
  "No spectra are recorded for this compound yet. Spectra saved from MestReNova with the ChemLinker add-on appear here.";
export const FOLDER_MISSING_TEXT = "This folder does not exist yet.";

export class FilesController {
  private entries = new Map<string, FilesEntry>();
  private seq = new Map<string, number>();
  private counter = 0;

  constructor(
    private readonly client: HostClient,
    private readonly hooks: CompoundsHooks,
  ) {}

  get(coreStereoId: string): FilesEntry | undefined {
    return this.entries.get(coreStereoId);
  }

  has(coreStereoId: string): boolean {
    return this.entries.has(coreStereoId);
  }

  /** Loads (or reloads) the given variants; one request each. */
  load(ids: readonly string[]): void {
    if (ids.length === 0) return;
    const next = new Map(this.entries);
    const started: [string, number][] = [];
    for (const id of ids) {
      const seq = ++this.counter;
      this.seq.set(id, seq);
      const previous = next.get(id);
      next.set(id, { loading: true, result: previous?.result ?? null, error: null });
      started.push([id, seq]);
    }
    this.entries = next;
    this.hooks.onChange();
    for (const [id, seq] of started) void this.request(id, seq);
  }

  private async request(id: string, seq: number): Promise<void> {
    const reply = await this.client.request("compound_files", { core_stereo_id: id });
    if (this.seq.get(id) !== seq) return; // a newer request, or the details were closed
    let entry: FilesEntry;
    if (reply.ok && reply.result.core_stereo_id === id) {
      entry = { loading: false, result: reply.result, error: null };
    } else {
      const error: HostError = reply.ok
        ? { code: "host_bad_response", message: `The answer was about ${reply.result.core_stereo_id}, not ${id}.` }
        : reply.error;
      entry = { loading: false, result: this.entries.get(id)?.result ?? null, error };
      this.hooks.announce?.(`The spectra and files of ${id} could not be loaded: ${error.message}`);
    }
    const next = new Map(this.entries);
    next.set(id, entry);
    this.entries = next;
    this.hooks.onChange();
  }

  /** Forgets every variant not in `ids` (details closed, list reloaded); their late answers are ignored. */
  retain(ids: ReadonlySet<string>): void {
    let changed = false;
    for (const id of [...this.entries.keys()]) {
      if (ids.has(id)) continue;
      this.entries.delete(id);
      this.seq.delete(id);
      changed = true;
    }
    if (changed) this.entries = new Map(this.entries);
  }
}

// ------------------------------------------------------------------ texts

/**
 * "500 MHz" for 500.13, "400 MHz" for 400.13 (from 100 MHz on, rounded to the
 * whole number spectrometers are named by); below that two decimals at most
 * ("60 MHz", "43.52 MHz").
 */
export function formatFrequency(mhz: number): string {
  if (mhz >= 99.5) return `${Math.round(mhz)} MHz`;
  const rounded = Math.round(mhz * 100) / 100;
  return `${rounded} MHz`;
}

/** "1H NMR", "13C NMR", "2D 1H NMR" from the nucleus and the number of dimensions. */
function nucleusText(nucleus: string | null, dimensions: number | null): string | null {
  const n = nucleus?.trim() ?? "";
  if (n === "") return null;
  return dimensions !== null && dimensions > 1 ? `${dimensions}D ${n} NMR` : `${n} NMR`;
}

/** "1H NMR · 500 MHz · CDCl3", from what exists; the title (or the spectrum key) when none of it does. */
export function spectrumHeading(s: Pick<SpectrumInfo, "nucleus" | "dimensions" | "frequency_mhz" | "solvent" | "title" | "spectrum_key">): string {
  const parts: string[] = [];
  const nucleus = nucleusText(s.nucleus, s.dimensions);
  if (nucleus) parts.push(nucleus);
  if (s.frequency_mhz !== null && s.frequency_mhz > 0) parts.push(formatFrequency(s.frequency_mhz));
  const solvent = s.solvent?.trim() ?? "";
  if (solvent !== "") parts.push(solvent);
  if (parts.length > 0) return parts.join(" · ");
  const title = s.title?.trim() ?? "";
  return title !== "" ? title : s.spectrum_key;
}

/** "980 bytes", "12.3 KB", "4.5 MB" (1 KB = 1024 bytes). */
export function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} ${bytes === 1 ? "byte" : "bytes"}`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const shown = value >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${shown} ${units[unit]}`;
}

/** "EX0001_01_001_FC2.pdf · 1.2 MB · 2026-11-17 14:05". */
export function folderFileLine(file: Pick<FolderFile, "name" | "size" | "modified">): string {
  return [file.name, humanSize(file.size), shortDate(file.modified)].filter((x) => x !== "").join(" · ");
}

/**
 * The spectra shown with their details: every recorded spectrum that is
 * neither a crude one nor one of an undesired side product.
 */
export function fullSpectra(result: CompoundFilesResult): SpectrumInfo[] {
  return result.spectra.filter((s) => s.crude !== true && s.side_product !== true);
}

/** One saved document of crude or side-product spectra: of these only the path is shown. */
export interface CrudeDocument {
  full_id: string;
  experiment_code: string | null;
  /** The recorded path of the MestReNova document, as text. */
  path: string;
  /** True when the spectrum was also marked as crude (always true in `crudeDocuments`). */
  crude: boolean;
}

function documentsOf(result: CompoundFilesResult, wanted: (s: SpectrumInfo) => boolean): CrudeDocument[] {
  const seen = new Set<string>();
  const out: CrudeDocument[] = [];
  for (const s of result.spectra) {
    const path = s.mnova_file?.trim() ?? "";
    if (!wanted(s) || path === "") continue;
    const key = `${s.full_id}\n${path.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ full_id: s.full_id, experiment_code: s.experiment_code, path, crude: s.crude === true });
  }
  return out;
}

/**
 * The crude spectra as one entry per batch and saved document (a document
 * with a 1H and a 13C spectrum is recorded twice but listed once), in the
 * order recorded. A crude record without a path is left out: there is nothing
 * to show. A crude spectrum of an undesired side product is not listed here
 * but by `sideProductDocuments`.
 */
export function crudeDocuments(result: CompoundFilesResult): CrudeDocument[] {
  return documentsOf(result, (s) => s.crude === true && s.side_product !== true);
}

/**
 * The spectra of undesired side products, listed like the crude ones: one
 * entry per batch and saved document, only the path.
 */
export function sideProductDocuments(result: CompoundFilesResult): CrudeDocument[] {
  return documentsOf(result, (s) => s.side_product === true);
}

/** The files of the folder that no recorded spectrum names. */
export function otherFiles(result: CompoundFilesResult): FolderFile[] {
  return result.files.filter((f) => !f.recorded);
}
