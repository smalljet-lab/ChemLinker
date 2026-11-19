/**
 * Pure helpers for the "Details" list of a compound: the display-only
 * properties computed by the host (formula, masses, elemental analysis) and
 * the identifiers, in one shape shared by the panel's cards and the compounds
 * page. No DOM.
 */

import type { FoundVariant, Properties, StructureInfo } from "../shared/protocol";
import { stereoModeLabel } from "./labels";

/** "345.574" -> "345.57 g/mol". */
export function formatMolarMass(value: number): string {
  return `${value.toFixed(2)} g/mol`;
}

/** "345.29259" -> "345.2926". */
export function formatExactMass(value: number): string {
  return value.toFixed(4);
}

/** "C 77.38, H 9.87, N 3.61 %"; null without any element. */
export function formatComposition(composition: Properties["composition"]): string | null {
  if (composition.length === 0) return null;
  return `${composition.map((c) => `${c.element} ${c.percent.toFixed(2)}`).join(", ")} %`;
}

/**
 * The sum formula, plus the registry's per-fragment formula when it says
 * something else ("C14H16ClN (per fragment: C14H15N.ClH)"). Either may be missing.
 */
export function formulaText(sumFormula: string | null, registryFormula: string | null): string | null {
  const sum = sumFormula?.trim() || null;
  const registry = registryFormula?.trim() || null;
  if (sum && registry && sum !== registry) return `${sum} (per fragment: ${registry})`;
  return sum ?? registry;
}

/** What the details list shows; every value may be missing (older host). */
export interface DetailsFacts {
  sumFormula: string | null;
  registryFormula: string | null;
  properties: Properties | null;
  smiles: string | null;
  inchi: string | null;
  inchikey: string | null;
  /** Already in words. */
  stereo: string | null;
}

/** One labelled line of the details list. `copy` marks the values with a Copy button. */
export interface DetailsLine {
  key: "formula" | "molar-mass" | "exact-mass" | "composition" | "smiles" | "inchi" | "inchikey" | "stereo";
  label: string;
  value: string;
  mono: boolean;
  copy: boolean;
}

const text = (value: string | null | undefined): string | null => (typeof value === "string" && value.trim() !== "" ? value : null);

export function detailsLines(facts: DetailsFacts): DetailsLine[] {
  const lines: DetailsLine[] = [];
  const push = (key: DetailsLine["key"], label: string, value: string | null, mono = false, copy = false): void => {
    if (value !== null) lines.push({ key, label, value, mono, copy });
  };
  const p = facts.properties;
  push("formula", "Formula", formulaText(facts.sumFormula, facts.registryFormula));
  push("molar-mass", "Molar mass", p ? formatMolarMass(p.mol_weight) : null);
  push("exact-mass", "Exact mass", p ? formatExactMass(p.exact_mass) : null);
  push("composition", "Elemental analysis", p ? formatComposition(p.composition) : null);
  push("smiles", "SMILES", text(facts.smiles), true, true);
  push("inchi", "InChI", text(facts.inchi), true, true);
  push("inchikey", "InChIKey", text(facts.inchikey), true, true);
  push("stereo", "Stereo", text(facts.stereo));
  return lines;
}

/** Facts of an analysed structure (the panel's card). */
export function factsFromStructure(s: StructureInfo): DetailsFacts {
  const stereo =
    stereoModeLabel(s.stereo_mode) + (s.detected_mode !== s.stereo_mode ? ` (drawing: ${stereoModeLabel(s.detected_mode)})` : "");
  return {
    sumFormula: s.properties?.sum_formula ?? null,
    registryFormula: s.formula,
    properties: s.properties ?? null,
    smiles: s.smiles,
    inchi: s.inchi ?? null,
    inchikey: s.inchikey,
    stereo,
  };
}

/** Facts of a registered stereo variant (the compounds page). */
export function factsFromVariant(v: FoundVariant, registryFormula: string | null): DetailsFacts {
  return {
    sumFormula: v.properties?.sum_formula ?? null,
    registryFormula,
    properties: v.properties ?? null,
    smiles: v.smiles,
    inchi: v.inchi ?? null,
    inchikey: v.inchikey,
    stereo: stereoModeLabel(v.stereo_mode),
  };
}
