/**
 * What the capture page shows and the user edits after a recognition: the
 * molecules, and the reaction steps that use them. Pure: no DOM.
 *
 * A molecule is known by its `key`; a step holds three lists of keys
 * (reactants, molecules over the arrow, products). A molecule may be used in
 * several steps (the product of one step is the reactant of the next) or in
 * none ("not used").
 *
 * SMILES always come from the host (the registry's RDKit): from `recognize`,
 * or from `describe_structures` after the user edited a molecule. This file
 * only joins them.
 */

import {
  MAX_CHEMDRAW_CONDITION_LINES,
  MAX_CHEMDRAW_STEP_MOLECULES,
  MAX_CHEMDRAW_STEPS,
  MAX_REACTION_TEXT,
  STRUCTURE_WARNING_CODES,
  type ChemdrawStep,
  type MoleculeWarning,
  type PixelBox,
  type RecognizeResult,
  type StructureWarning,
} from "../shared/protocol";

export type Role = "reactant" | "agent" | "product";
export const ROLES: readonly Role[] = ["reactant", "agent", "product"];

export const ROLE_LABELS: Readonly<Record<Role, string>> = {
  reactant: "Reactant",
  agent: "Over the arrow",
  product: "Product",
};

const LIST_OF: Readonly<Record<Role, "reactants" | "agents" | "products">> = {
  reactant: "reactants",
  agent: "agents",
  product: "products",
};

export interface Molecule {
  key: string;
  /** The number shown on the picture and on the card. */
  n: number;
  /** Where it was found in the picture; null for a molecule the user added. */
  box: PixelBox | null;
  smiles: string | null;
  molfile: string | null;
  /** The model's value; null when unknown or after the user changed the molecule. */
  confidence: number | null;
  warnings: MoleculeWarning[];
  /** True when the structure is the user's (edited or drawn), not the model's. */
  edited: boolean;
}

export interface Step {
  reactants: string[];
  agents: string[];
  products: string[];
  /** The text at the arrow as the user sees and edits it, one condition per line. */
  conditions: string;
}

export interface Model {
  molecules: Molecule[];
  steps: Step[];
}

export interface Placement {
  step: number;
  role: Role;
}

/** What the host says about a structure after an edit. */
export interface Description {
  smiles: string | null;
  warnings: readonly StructureWarning[];
}

const keyOf = (n: number): string => `m${n}`;

export function modelFromResult(result: RecognizeResult): Model {
  const molecules: Molecule[] = result.molecules.map((m) => ({
    key: keyOf(m.n),
    n: m.n,
    box: m.box,
    smiles: m.smiles,
    molfile: m.molfile,
    confidence: m.confidence,
    warnings: [...m.warnings],
    edited: false,
  }));
  const known = new Set(molecules.map((m) => m.key));
  const keys = (numbers: readonly number[]): string[] => [...new Set(numbers.map(keyOf))].filter((key) => known.has(key));
  const steps: Step[] = result.reactions.map((r) => ({
    reactants: keys(r.reactants),
    agents: keys(r.agents),
    products: keys(r.products),
    conditions: r.conditions.flatMap((c) => c.lines).join("\n"),
  }));
  return { molecules, steps };
}

export function findMolecule(model: Model, key: string): Molecule | null {
  return model.molecules.find((m) => m.key === key) ?? null;
}

/** Every place a molecule is used, in step order. */
export function placementsOf(model: Model, key: string): Placement[] {
  const out: Placement[] = [];
  model.steps.forEach((step, index) => {
    for (const role of ROLES) if (step[LIST_OF[role]].includes(key)) out.push({ step: index, role });
  });
  return out;
}

/** The role a molecule has in a step, or null when it is not in that step. */
export function roleIn(model: Model, key: string, step: number): Role | null {
  const target = model.steps[step];
  if (!target) return null;
  return ROLES.find((role) => target[LIST_OF[role]].includes(key)) ?? null;
}

/** The molecules that are in no step. */
export function unusedMolecules(model: Model): Molecule[] {
  return model.molecules.filter((m) => placementsOf(model, m.key).length === 0);
}

export function moleculesOf(model: Model, step: number, role: Role): Molecule[] {
  const list = model.steps[step]?.[LIST_OF[role]] ?? [];
  return list.map((key) => findMolecule(model, key)).filter((m): m is Molecule => m !== null);
}

/** Adds an empty step at the end and returns its index. */
export function addStep(model: Model): number {
  model.steps.push({ reactants: [], agents: [], products: [], conditions: "" });
  return model.steps.length - 1;
}

/** Removes a step; its molecules stay (and become "not used" when no other step has them). */
export function removeStep(model: Model, step: number): void {
  if (step >= 0 && step < model.steps.length) model.steps.splice(step, 1);
}

/**
 * Moves one use of a molecule: away from `from` (null: it was not used) and
 * into `to` (null: "not used" there). `to.step` may be "new" for a new step at
 * the end. A molecule is never twice in the same list.
 */
export function place(model: Model, key: string, from: Placement | null, to: { step: number | "new"; role: Role } | null): void {
  if (findMolecule(model, key) === null) return;
  if (from !== null) {
    const list = model.steps[from.step]?.[LIST_OF[from.role]];
    const at = list?.indexOf(key) ?? -1;
    if (list && at >= 0) list.splice(at, 1);
  }
  if (to === null) return;
  const index = to.step === "new" ? addStep(model) : to.step;
  const target = model.steps[index];
  if (!target) return;
  // One role per step: a molecule moved onto a step it is already in changes its role there.
  for (const role of ROLES) {
    const list = target[LIST_OF[role]];
    const at = list.indexOf(key);
    if (at >= 0) list.splice(at, 1);
  }
  target[LIST_OF[to.role]].push(key);
}

/** The warnings that can be told from one structure; the others belong to the model's reading. */
function structureWarnings(warnings: readonly string[]): MoleculeWarning[] {
  return STRUCTURE_WARNING_CODES.filter((code) => warnings.includes(code));
}

/**
 * Marks "duplicate" on every molecule whose SMILES another molecule has too,
 * and removes it everywhere else. Called after every change of a structure.
 */
export function refreshDuplicates(model: Model): void {
  const count = new Map<string, number>();
  for (const m of model.molecules) if (m.smiles) count.set(m.smiles, (count.get(m.smiles) ?? 0) + 1);
  for (const m of model.molecules) {
    const duplicate = m.smiles !== null && m.smiles !== "" && (count.get(m.smiles) ?? 0) > 1;
    const has = m.warnings.includes("duplicate");
    if (duplicate && !has) m.warnings.push("duplicate");
    if (!duplicate && has) m.warnings = m.warnings.filter((w) => w !== "duplicate");
  }
}

/**
 * The user's structure replaces the model's: the molecule keeps its number and
 * its places, loses the model's confidence and the warnings about the model's
 * reading, and is marked as edited. Returns true when that happened.
 *
 * A save that changed nothing is not an edit: when the host gives the drawing
 * the SMILES the molecule already has, the molecule stays exactly as it was
 * (confidence, warnings, "not edited") and false is returned. Otherwise
 * opening the editor only to look, and pressing Save, would turn an unchecked
 * reading into "edited by you" and hide that the program was unsure.
 */
export function applyEdit(model: Model, key: string, molfile: string, description: Description): boolean {
  const molecule = findMolecule(model, key);
  if (!molecule) return false;
  if (description.smiles !== null && description.smiles !== "" && description.smiles === molecule.smiles) return false;
  molecule.molfile = molfile;
  molecule.smiles = description.smiles;
  molecule.warnings = structureWarnings(description.warnings);
  molecule.confidence = null;
  molecule.edited = true;
  refreshDuplicates(model);
  return true;
}

/** A molecule the user drew. It starts in no step. Returns its key. */
export function addMolecule(model: Model, molfile: string, description: Description): string {
  const n = model.molecules.reduce((max, m) => Math.max(max, m.n), 0) + 1;
  const key = keyOf(n);
  model.molecules.push({
    key,
    n,
    box: null,
    smiles: description.smiles,
    molfile,
    confidence: null,
    warnings: structureWarnings(description.warnings),
    edited: true,
  });
  refreshDuplicates(model);
  return key;
}

/** Removes a molecule from the list and from every step. */
export function removeMolecule(model: Model, key: string): void {
  model.molecules = model.molecules.filter((m) => m.key !== key);
  for (const step of model.steps) {
    for (const role of ROLES) {
      const name = LIST_OF[role];
      step[name] = step[name].filter((k) => k !== key);
    }
  }
  refreshDuplicates(model);
}

/**
 * "reactants>agents>products" of one step from the molecules' SMILES, each
 * part joined with ".". Agents without SMILES are left out. Null when a
 * reactant or product has no SMILES, or the step has neither.
 */
export function reactionSmiles(model: Model, step: number): string | null {
  if (!model.steps[step]) return null;
  const reactants = moleculesOf(model, step, "reactant");
  const products = moleculesOf(model, step, "product");
  if (reactants.length === 0 && products.length === 0) return null;
  const smiles = (list: Molecule[]) => list.map((m) => m.smiles);
  const r = smiles(reactants);
  const p = smiles(products);
  if ([...r, ...p].some((s) => s === null || s === "")) return null;
  const a = smiles(moleculesOf(model, step, "agent")).filter((s): s is string => s !== null && s !== "");
  return `${r.join(".")}>${a.join(".")}>${p.join(".")}`;
}

/** The condition text as lines: trimmed, without blank lines, tabs as spaces. */
export function conditionLines(text: string): string[] {
  return text
    .split(/\r?\n|\r/)
    .map((line) => line.replace(/[\u0000-\u001f\u007f]/g, " ").trim())
    .filter((line) => line !== "");
}

/** Everything as plain text for the clipboard. */
export function copyAllText(model: Model): string {
  const lines: string[] = [];
  const describe = (label: string, m: Molecule): string => `${label} (${m.n}): ${m.smiles ?? "no SMILES"}`;
  model.steps.forEach((step, index) => {
    if (lines.length > 0) lines.push("");
    lines.push(model.steps.length > 1 ? `Step ${index + 1}` : "Reaction");
    lines.push(`Reaction SMILES: ${reactionSmiles(model, index) ?? "not available"}`);
    for (const role of ROLES) {
      for (const m of moleculesOf(model, index, role)) lines.push(describe(ROLE_LABELS[role], m));
    }
    const conditions = conditionLines(step.conditions);
    if (conditions.length > 0) lines.push(`Conditions: ${conditions.join("; ")}`);
  });
  const unused = unusedMolecules(model);
  if (unused.length > 0) {
    if (lines.length > 0) lines.push("");
    if (model.steps.length > 0) lines.push("Not used in a reaction");
    for (const m of unused) lines.push(describe("Molecule", m));
  }
  return lines.join("\n");
}

/** `leftOut`: how many molecules are in no step that is drawn, and so are not in the file. */
export type ChemdrawRequest = { ok: true; steps: ChemdrawStep[]; leftOut: number } | { ok: false; problem: string };

/**
 * The steps as `reaction_to_chemdraw` takes them, from the current state.
 * Steps without reactants and products are left out, and so are molecules
 * that are in no step (`leftOut` counts them). Anything the host would refuse
 * is reported in plain words instead of being sent.
 */
export function chemdrawRequest(model: Model): ChemdrawRequest {
  const steps: ChemdrawStep[] = [];
  const drawn = new Set<string>();
  for (let index = 0; index < model.steps.length; index++) {
    const step = model.steps[index]!;
    const name = model.steps.length > 1 ? `Step ${index + 1}` : "The reaction";
    const lists = { reactants: [] as string[], agents: [] as string[], products: [] as string[] };
    for (const role of ROLES) {
      const molecules = moleculesOf(model, index, role);
      if (molecules.length > MAX_CHEMDRAW_STEP_MOLECULES) {
        return { ok: false, problem: `${name} has more than ${MAX_CHEMDRAW_STEP_MOLECULES} molecules as ${ROLE_LABELS[role].toLowerCase()}. Remove some first.` };
      }
      for (const m of molecules) {
        if (m.molfile === null || m.molfile.trim() === "") {
          return { ok: false, problem: `Molecule ${m.n} has no structure. Edit it or set it to "Not used" first.` };
        }
        lists[LIST_OF[role]].push(m.molfile);
      }
    }
    if (lists.reactants.length === 0 && lists.products.length === 0) continue;
    const conditions = conditionLines(step.conditions);
    if (conditions.length > MAX_CHEMDRAW_CONDITION_LINES) {
      return { ok: false, problem: `${name} has more than ${MAX_CHEMDRAW_CONDITION_LINES} lines of conditions. Shorten the text first.` };
    }
    if (conditions.some((line) => line.length > MAX_REACTION_TEXT)) {
      return { ok: false, problem: `${name} has a condition line longer than ${MAX_REACTION_TEXT} characters. Shorten the text first.` };
    }
    steps.push({ ...lists, conditions });
    for (const role of ROLES) for (const key of step[LIST_OF[role]]) drawn.add(key);
  }
  if (steps.length === 0) {
    if (model.molecules.length === 0) return { ok: false, problem: "There is no molecule to draw." };
    // ChemDraw gets a reaction; a molecule alone (the result of "A single molecule") has no place in one yet.
    return {
      ok: false,
      problem:
        model.molecules.length === 1
          ? "ChemDraw gets a reaction. To open this molecule there, give it the role Reactant or Product first (the Role box on its card)."
          : "ChemDraw gets a reaction. Give at least one molecule the role Reactant or Product first (the Role box on its card).",
    };
  }
  if (steps.length > MAX_CHEMDRAW_STEPS) {
    return { ok: false, problem: `There are more than ${MAX_CHEMDRAW_STEPS} steps. Remove some first.` };
  }
  return { ok: true, steps, leftOut: model.molecules.filter((m) => !drawn.has(m.key)).length };
}
