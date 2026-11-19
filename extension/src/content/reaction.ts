/**
 * Pure helpers about the reaction an experiment page shows: checking the page
 * bridge's `reaction` answer (untrusted text from the page), turning it into
 * the `reaction` object of `set_reaction`, and the signature that tells
 * whether the page shows something else than what was sent. No DOM.
 *
 * Conversion rules (`toReactionData`):
 * - an amount is a number only when its text is a plain number ("2313",
 *   "0,915", "1.000"); anything else (empty, "~5", "1e3") is null;
 * - the units of both the actual and the planned amounts are the units the
 *   grid header shows (`page.units`);
 * - a row's structure is the molfile read for its handle, or null (none
 *   drawn, or it could not be read);
 * - texts are one line, at most MAX_REACTION_TEXT characters; lists at most
 *   MAX_REACTION_ROWS items (the host refuses more).
 */

import type { PageAmounts, PageComponent, PageReaction } from "../shared/bridge";
import {
  MAX_PRODUCT_INDEX,
  MAX_REACTION_NUMBER,
  MAX_REACTION_ROWS,
  MAX_REACTION_TEXT,
  MAX_STRUCTURE_CHARS,
  type ReactionAmounts,
  type ReactionComponent,
  type ReactionData,
  type ReactionSolvent,
} from "../shared/protocol";

/** Rows and texts larger than this in a bridge answer make it unreadable (the bridge itself clips below). */
const MAX_BRIDGE_ROWS = 500;
const MAX_BRIDGE_TEXT = 5000;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const isText = (v: unknown): v is string | null => v === null || (typeof v === "string" && v.length <= MAX_BRIDGE_TEXT);

function isPageAmounts(v: unknown): v is PageAmounts {
  return isRecord(v) && isText(v.eq) && isText(v.n) && isText(v.m) && isText(v.v);
}

function isPageComponent(v: unknown): v is PageComponent {
  return (
    isRecord(v) &&
    typeof v.row === "number" &&
    Number.isSafeInteger(v.row) &&
    v.row >= 1 &&
    isText(v.handle) &&
    isText(v.smiles) &&
    isText(v.name) &&
    isText(v.formula) &&
    isText(v.cas) &&
    isText(v.sourceRef) &&
    isText(v.sourceCode) &&
    isPageAmounts(v.actual) &&
    isPageAmounts(v.planned)
  );
}

function isSolvent(v: unknown): v is PageReaction["solvents"][number] {
  return isRecord(v) && typeof v.name === "string" && v.name.length <= MAX_BRIDGE_TEXT && isText(v.volume) && isText(v.unit);
}

function isList<T>(v: unknown, item: (x: unknown) => x is T): v is T[] {
  return Array.isArray(v) && v.length <= MAX_BRIDGE_ROWS && v.every(item);
}

/** The bridge's `reaction` answer when it has the documented shape; null otherwise. */
export function readPageReaction(v: unknown): PageReaction | null {
  if (!isRecord(v)) return null;
  const units = v.units;
  const ref = v.referenceAmount;
  const ok =
    typeof v.readOnly === "boolean" &&
    (v.amountsShown === null || v.amountsShown === "actual" || v.amountsShown === "planned") &&
    isRecord(units) &&
    isText(units.n) &&
    isText(units.m) &&
    isText(units.v) &&
    isList(v.reactants, isPageComponent) &&
    isList(v.reagents, isPageComponent) &&
    isList(v.solvents, isSolvent) &&
    isText(v.temperature) &&
    (ref === null || (isRecord(ref) && typeof ref.value === "string" && ref.value.length <= MAX_BRIDGE_TEXT && isText(ref.unit)));
  return ok ? (v as unknown as PageReaction) : null;
}

// ------------------------------------------------------------------ conversion

const NUMBER = /^\d+(?:[.,]\d+)?$/;

/** A plain number as the grid shows it ("2313", "0,915"); anything else null. */
export function parseAmount(text: string | null | undefined): number | null {
  if (typeof text !== "string") return null;
  const value = text.trim();
  if (!NUMBER.test(value)) return null;
  const number = Number(value.replace(",", "."));
  return Number.isFinite(number) && number >= 0 && number <= MAX_REACTION_NUMBER ? number : null;
}

/** One line (control characters become spaces), trimmed, at most MAX_REACTION_TEXT characters; empty -> null. */
export function reactionText(text: string | null | undefined): string | null {
  if (typeof text !== "string") return null;
  // eslint-disable-next-line no-control-regex
  const line = text.replace(/[\u0000-\u001f\u007f]+/g, " ").trim().slice(0, MAX_REACTION_TEXT).trim();
  return line === "" ? null : line;
}

/** A molfile the host accepts (newlines and tabs only), else null. */
function usableMolfile(molfile: string | null | undefined): string | null {
  if (typeof molfile !== "string" || molfile.trim() === "" || molfile.length > MAX_STRUCTURE_CHARS) return null;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(molfile)) return null;
  return molfile;
}

export type MolfilesByHandle = ReadonlyMap<string, string | null> | Readonly<Record<string, string | null>>;

function molfileOf(molfiles: MolfilesByHandle, handle: string | null): string | null {
  if (handle === null) return null;
  if (molfiles instanceof Map) return usableMolfile(molfiles.get(handle));
  const record = molfiles as Readonly<Record<string, string | null>>;
  return Object.prototype.hasOwnProperty.call(record, handle) ? usableMolfile(record[handle]) : null;
}

function amounts(page: PageAmounts, units: PageReaction["units"]): ReactionAmounts {
  return {
    eq: parseAmount(page.eq),
    n: parseAmount(page.n),
    n_unit: reactionText(units.n),
    m: parseAmount(page.m),
    m_unit: reactionText(units.m),
    v: parseAmount(page.v),
    v_unit: reactionText(units.v),
  };
}

function components(rows: readonly PageComponent[], page: PageReaction, molfiles: MolfilesByHandle): ReactionComponent[] {
  const out: ReactionComponent[] = [];
  for (const row of rows) {
    if (out.length >= MAX_REACTION_ROWS) break;
    if (!Number.isSafeInteger(row.row) || row.row < 1 || row.row > MAX_PRODUCT_INDEX) continue;
    out.push({
      row: row.row,
      name: reactionText(row.name),
      formula: reactionText(row.formula),
      cas: reactionText(row.cas),
      source_experiment_ref: reactionText(row.sourceRef),
      source_experiment_code: reactionText(row.sourceCode),
      molfile: molfileOf(molfiles, row.handle),
      actual: amounts(row.actual, page.units),
      planned: amounts(row.planned, page.units),
    });
  }
  return out;
}

/** The `reaction` of `set_reaction` for what the page shows (see the module rules). */
export function toReactionData(page: PageReaction, molfiles: MolfilesByHandle): ReactionData {
  const solvents: ReactionSolvent[] = [];
  for (const s of page.solvents) {
    if (solvents.length >= MAX_REACTION_ROWS) break;
    const name = reactionText(s.name);
    if (name === null) continue;
    solvents.push({ name, volume: parseAmount(s.volume), unit: reactionText(s.unit) });
  }
  const refValue = page.referenceAmount ? parseAmount(page.referenceAmount.value) : null;
  return {
    reactants: components(page.reactants, page, molfiles),
    reagents: components(page.reagents, page, molfiles),
    solvents,
    temperature: reactionText(page.temperature),
    reference_amount: refValue === null ? null : { value: refValue, unit: reactionText(page.referenceAmount!.unit) },
    amounts_shown: page.amountsShown,
  };
}

/**
 * Changes whenever what the page shows about the reaction changes: the
 * answer without the row handles (ZK gives rows new DOM ids when it renders
 * them again; that alone changes nothing). The `smiles` attributes stay in:
 * they change with the drawn structures.
 */
export function reactionSignature(page: PageReaction): string {
  const strip = (rows: readonly PageComponent[]) => rows.map(({ handle: _handle, ...rest }) => rest);
  return JSON.stringify({ ...page, reactants: strip(page.reactants), reagents: strip(page.reagents) });
}

/** The handles of the rows whose structure is to be read, in page order (reactants, then reagents), without repeats. */
export function rowHandles(page: PageReaction): string[] {
  const out: string[] = [];
  for (const row of [...page.reactants.slice(0, MAX_REACTION_ROWS), ...page.reagents.slice(0, MAX_REACTION_ROWS)]) {
    if (row.handle !== null && row.handle !== "" && !out.includes(row.handle)) out.push(row.handle);
  }
  return out;
}

/**
 * The largest `set_reaction` params this side sends, in bytes of JSON. The
 * host refuses a request frame over 1 MiB (MAX_REQUEST_BYTES in
 * protocol.py) and stops, which fails every other request waiting on it;
 * 16 KiB stay free for the request envelope (id, version, op).
 */
export const MAX_SET_REACTION_BYTES = 1024 * 1024 - 16 * 1024;

/** The size of `value` as UTF-8 JSON, in bytes (what goes over native messaging). */
export function jsonBytes(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).length;
}

/**
 * `params` made small enough to send: while its JSON is over `maxBytes`,
 * the largest structure (molfile) left is replaced by null. Gives the params
 * to send (a copy when something was dropped) and how many structures were
 * dropped; null when even without any structure it is too large.
 */
export function fitSetReaction<T extends { reaction: ReactionData }>(
  params: T,
  maxBytes: number = MAX_SET_REACTION_BYTES,
): { params: T; dropped: number } | null {
  if (jsonBytes(params) <= maxBytes) return { params, dropped: 0 };
  const reaction: ReactionData = {
    ...params.reaction,
    reactants: params.reaction.reactants.map((c) => ({ ...c })),
    reagents: params.reaction.reagents.map((c) => ({ ...c })),
  };
  const fitted: T = { ...params, reaction };
  const withStructure = [...reaction.reactants, ...reaction.reagents]
    .filter((c) => typeof c.molfile === "string")
    .sort((a, b) => b.molfile!.length - a.molfile!.length);
  let dropped = 0;
  for (const component of withStructure) {
    component.molfile = null;
    dropped += 1;
    if (jsonBytes(fitted) <= maxBytes) return { params: fitted, dropped };
  }
  return null;
}
