/**
 * Reads the open experiment and its product rows from a Sciformation page
 * (ZK 10). Pure DOM reading of the frame's own document: no bridge, no host,
 * no changes to the page.
 *
 * Layout (verified on the live site; DOM ids are random per session and are
 * only used as handles read at this moment):
 * - anchor: the VISIBLE `a[href*="addProductToInventory.uc&fromReactionId="]`
 *   (a hidden template link with another number exists); its `fromReactionId`
 *   is the experiment key; `anchor.closest(".z-grid")` is the products grid;
 * - check: a visible `a[href*="addOrgIncident.uc&elnReaction="]` must carry
 *   the same key when present;
 * - code: `title` of the page's `h2` (e.g. "ABC-008"), else the text of a
 *   visible `h2 span.z-html`;
 * - rows: `.z-grid-body tbody.z-rows > tr.z-row` of that grid, in page order;
 *   product no. = position (1-based);
 * - per row: `div.z-macro[smiles]`: its `id` is the handle for the page
 *   bridge, its `smiles` attribute is only a change detector (never the
 *   structure);
 * - per row, the yields: the row's DIRECT child cells (a cell contains tables
 *   of its own) by position: 9 analytical yield, 10 isolated yield, 11 comment
 *   and lot no. Header texts and placeholders are localised, so the shape is
 *   checked instead: at least 12 cells, exactly one input in cells 9 and 10 and
 *   exactly two in cell 11. The input's `value` is what the user sees (rounded,
 *   no percent sign); its `title` (unrounded) is never used. While the
 *   experiment is being edited the inputs are not read-only, and the values
 *   are not used (they may be half typed or unsaved).
 */

import type { PageYields } from "../shared/adapter";
import { MAX_REF_TEXT } from "../shared/protocol";
import { parseYieldText } from "./yields";

export interface ExtractedRow {
  /** 1-based product number (position in the grid). */
  index: number;
  /** DOM id of the structure macro; null when the row has none. */
  handle: string | null;
  /** The server's SMILES attribute (change detector only); null or "" = no structure drawn. */
  smiles: string | null;
  /** The yields the row shows in its saved (read-only) state; null when they cannot be used now. */
  yields: PageYields | null;
}

/** Cell positions in a product row (verified on the live site). */
export const ANALYTICAL_YIELD_CELL = 9;
export const ISOLATED_YIELD_CELL = 10;
export const COMMENT_CELL = 11;

function inputsOf(cell: Element): HTMLInputElement[] {
  return Array.from(cell.querySelectorAll("input")) as HTMLInputElement[];
}

/**
 * The yields of one product row (`tr.z-row`), or null when they are not
 * usable: unexpected shape, inputs not read-only (edit mode), or a value that
 * is not a plain number of percent in range.
 */
export function readRowYields(row: Element): PageYields | null {
  const cells = Array.from(row.children).filter((c) => c.tagName === "TD");
  if (cells.length <= COMMENT_CELL) return null;
  const analytical = inputsOf(cells[ANALYTICAL_YIELD_CELL]!);
  const isolated = inputsOf(cells[ISOLATED_YIELD_CELL]!);
  if (analytical.length !== 1 || isolated.length !== 1 || inputsOf(cells[COMMENT_CELL]!).length !== 2) return null;
  const a = analytical[0]!;
  const i = isolated[0]!;
  if (a.readOnly !== true || i.readOnly !== true) return null;
  const analyticalValue = parseYieldText(String(a.value ?? ""));
  const isolatedValue = parseYieldText(String(i.value ?? ""));
  if (analyticalValue === undefined || isolatedValue === undefined) return null;
  return { isolated: isolatedValue, analytical: analyticalValue };
}

export type Extraction =
  | { kind: "none" }
  | { kind: "error"; what: string }
  | { kind: "experiment"; key: string; code: string | null; rows: ExtractedRow[] };

export type IsVisible = (element: Element) => boolean;

export const defaultIsVisible: IsVisible = (element) => element.getClientRects().length > 0;

const PRODUCT_LINK = 'a[href*="addProductToInventory.uc&fromReactionId="]';
const INCIDENT_LINK = 'a[href*="addOrgIncident.uc&elnReaction="]';

function queryParam(doc: Document, href: string | null, name: string): string | null {
  if (href === null) return null;
  try {
    return new URL(href, doc.baseURI).searchParams.get(name);
  } catch {
    return null;
  }
}

function hasControlChars(text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c < 0x20 || c === 0x7f) return true;
  }
  return false;
}

function cleanCode(value: string | null | undefined): string | null {
  const text = (value ?? "").trim();
  if (text === "" || text.length > MAX_REF_TEXT || hasControlChars(text)) return null;
  return text;
}

export function readExperimentCode(doc: Document, isVisible: IsVisible = defaultIsVisible): string | null {
  for (const h2 of Array.from(doc.querySelectorAll("h2[title]"))) {
    const code = cleanCode(h2.getAttribute("title"));
    if (code !== null) return code;
  }
  for (const span of Array.from(doc.querySelectorAll("h2 span.z-html"))) {
    if (!isVisible(span)) continue;
    const code = cleanCode(span.textContent);
    if (code !== null) return code;
  }
  return null;
}

export function extractExperiment(doc: Document, isVisible: IsVisible = defaultIsVisible): Extraction {
  const anchors = Array.from(doc.querySelectorAll(PRODUCT_LINK)).filter(isVisible);
  if (anchors.length === 0) return { kind: "none" };

  const keys = new Set(anchors.map((a) => queryParam(doc, a.getAttribute("href"), "fromReactionId")));
  if (keys.size !== 1) return { kind: "error", what: "the products grid shows more than one experiment key" };
  const key = [...keys][0];
  if (typeof key !== "string" || !/^\d{1,18}$/.test(key)) {
    return { kind: "error", what: "the experiment key in the products grid is not a number" };
  }

  for (const link of Array.from(doc.querySelectorAll(INCIDENT_LINK)).filter(isVisible)) {
    const other = queryParam(doc, link.getAttribute("href"), "elnReaction");
    if (other !== key) return { kind: "error", what: "two links on the page name different experiment keys" };
  }

  const anchor = anchors[0]!;
  const grid = anchor.closest(".z-grid");
  if (!grid) return { kind: "error", what: "the products grid was not found" };
  // The rows container of THIS grid (a cell may contain a grid of its own).
  const body = Array.from(grid.querySelectorAll(".z-grid-body tbody.z-rows")).find((t) => t.closest(".z-grid") === grid);
  if (!body) return { kind: "error", what: "the products grid has no rows container" };

  const rows: ExtractedRow[] = [];
  for (const tr of Array.from(body.children)) {
    if (tr.tagName !== "TR" || !tr.classList.contains("z-row")) continue;
    const macro = Array.from(tr.querySelectorAll("div.z-macro[smiles]")).find(
      (m) => m.closest("tr.z-row") === tr,
    );
    const handle = macro && macro.id ? macro.id : null;
    const smiles = macro ? macro.getAttribute("smiles") : null;
    rows.push({ index: rows.length + 1, handle, smiles, yields: readRowYields(tr) });
  }
  return { kind: "experiment", key, code: readExperimentCode(doc, isVisible), rows };
}

/** True when the row shows a structure that can be read. */
export function rowHasStructure(row: ExtractedRow): row is ExtractedRow & { handle: string; smiles: string } {
  return row.handle !== null && row.smiles !== null && row.smiles.trim() !== "";
}
