/**
 * Pure helpers about yields: reading a yield field's text from the ELN page,
 * comparing what the page shows with what the registry stores, and the texts
 * the panel and the compounds page show. No DOM.
 */

import type { PageYields } from "../shared/adapter";
import { MAX_YIELD_PERCENT, type YieldInfo } from "../shared/protocol";

/**
 * The text of one yield field as the ELN shows it (the input's value):
 *   null       empty (no yield entered);
 *   a number   a plain number in percent ("76.2", "76,2", "76.2 %", "90"), within 0..MAX_YIELD_PERCENT;
 *   undefined  anything else: not usable (never guessed).
 */
export function parseYieldText(text: string): number | null | undefined {
  const value = text.trim();
  if (value === "") return null;
  const m = /^(\d+)(?:[.,](\d+))?\s*%?$/.exec(value);
  if (!m) return undefined;
  const number = Number(m[2] === undefined ? m[1] : `${m[1]}.${m[2]}`);
  if (!Number.isFinite(number) || number < 0 || number > MAX_YIELD_PERCENT) return undefined;
  return number;
}

/** Rounded to two decimals, as the registry stores it. */
export function roundYield(value: number | null): number | null {
  if (value === null) return null;
  return Math.round(value * 100) / 100;
}

/** The page's values as they would be stored. */
export function roundedYields(page: PageYields): PageYields {
  return { isolated: roundYield(page.isolated), analytical: roundYield(page.analytical) };
}

/** Both values of the stored yield; a stored null counts as both null. */
export function storedValues(stored: YieldInfo | null): PageYields {
  return stored === null ? { isolated: null, analytical: null } : { isolated: stored.isolated, analytical: stored.analytical };
}

/** True when the page's values (rounded to two decimals) are what the registry stores. */
export function sameYields(page: PageYields, stored: YieldInfo | null): boolean {
  const a = roundedYields(page);
  const b = storedValues(stored);
  return a.isolated === roundYield(b.isolated) && a.analytical === roundYield(b.analytical);
}

export function sameValues(a: PageYields, b: PageYields): boolean {
  return a.isolated === b.isolated && a.analytical === b.analytical;
}

/** A percentage without trailing zeros: 76.2, 90, 73.65. */
export function formatPercent(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  return String(Object.is(rounded, -0) ? 0 : rounded);
}

/** True when a stored yield has at least one value. */
export function hasYield(stored: YieldInfo | null | undefined): stored is YieldInfo {
  return stored !== null && stored !== undefined && (stored.isolated !== null || stored.analytical !== null);
}

/**
 * The card's yield line: "Yield 76.2 % (isolated)", "Yield 78 % (analytical)",
 * or "Yield 76.2 % isolated · 78 % analytical". Null when nothing is stored.
 */
export function yieldLine(stored: YieldInfo | null | undefined): string | null {
  if (!hasYield(stored)) return null;
  const { isolated, analytical } = stored;
  if (isolated !== null && analytical !== null) {
    return `Yield ${formatPercent(isolated)} % isolated · ${formatPercent(analytical)} % analytical`;
  }
  if (isolated !== null) return `Yield ${formatPercent(isolated)} % (isolated)`;
  return `Yield ${formatPercent(analytical!)} % (analytical)`;
}

/**
 * Short form next to an ELN number: "76.2 %" (isolated), else "78 % analytical".
 * Null when nothing is stored.
 */
export function shortYield(stored: YieldInfo | null | undefined): string | null {
  if (!hasYield(stored)) return null;
  if (stored.isolated !== null) return `${formatPercent(stored.isolated)} %`;
  return `${formatPercent(stored.analytical!)} % analytical`;
}

/**
 * A batch's yield in a list of batches: "76.2 % isolated", "78 % analytical",
 * or "76.2 % isolated · 78 % analytical". Null when nothing is stored.
 */
export function batchYieldText(stored: YieldInfo | null | undefined): string | null {
  if (!hasYield(stored)) return null;
  const parts: string[] = [];
  if (stored.isolated !== null) parts.push(`${formatPercent(stored.isolated)} % isolated`);
  if (stored.analytical !== null) parts.push(`${formatPercent(stored.analytical)} % analytical`);
  return parts.join(" · ");
}

/** The first yield among batches (active ones first), for "ABC-006 (76.2 %)". */
export function firstYield(batches: readonly { retired: boolean; yield?: YieldInfo | null }[]): YieldInfo | null {
  for (const b of batches) if (!b.retired && hasYield(b.yield)) return b.yield;
  for (const b of batches) if (hasYield(b.yield)) return b.yield;
  return null;
}
