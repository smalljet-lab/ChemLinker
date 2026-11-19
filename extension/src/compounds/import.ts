/**
 * Import of many experiments from the ELN's exported experiment list (the
 * "Import" view of the compounds page). No DOM; every outside access is
 * injected (host requests, tabs, timers), so it runs in tests and scripts.
 *
 *   1. The file is read (`parseElnCsv`).
 *   2. The plan: every batch of the registry (`yields`, all pages, retired
 *      ones ignored) is matched with the listed products by experiment_ref +
 *      product number. Read-only; nothing is opened.
 *   3. The walk (only on "Check" / "Create IDs"): the experiments are opened
 *      one after the other in ONE background tab of the import's own; the
 *      content script in that tab is asked what it shows (`import-status`)
 *      and then to handle the chosen products (`import-apply`), see
 *      src/shared/import.ts. Only that tab is ever touched, and it is closed
 *      at the end.
 */

import type { HostClient } from "../content/host-client";
import { formatPercent } from "../content/yields";
import type { PageYields } from "../shared/adapter";
import {
  experimentPageUrl,
  IMPORT_APPLY_TIMEOUT_MS,
  IMPORT_MAX_OPEN_FAILURES,
  IMPORT_OPEN_TIMEOUT_MS,
  IMPORT_PAUSE_MS,
  IMPORT_POLL_MS,
  MAX_IMPORT_PRODUCTS,
  type ImportApplyRequest,
  type ImportOutcome,
  type ImportOutcomeKind,
  type ImportPageProduct,
  type ImportRequest,
  type ImportStatusRequest,
} from "../shared/import";
import { MAX_YIELD_PAGE_SIZE, type AnalysisStatus, type HostError, type YieldRow } from "../shared/protocol";
import { parseElnCsv, type ElnExperiment, type ElnParseResult, type ElnProduct } from "./eln-csv";
import { compareFormulas, compareYields, sameYield, type YieldComparison } from "./import-compare";
import { csvText, guardText, tsvText } from "./state";

// ------------------------------------------------------------------ options

export type ImportMode = "check" | "create";
export type ImportRule = "with_yield" | "all";

export interface ImportOptions {
  /** "check": look up only, nothing is created or stored; "create": create IDs. */
  mode: ImportMode;
  /** "with_yield": products whose isolated yield is above 0; "all": every product. */
  rule: ImportRule;
  /** Also open experiments whose handled products all have an ID already. */
  reopenRegistered: boolean;
  /** Passed on as `allow_warnings`. */
  allowWarnings: boolean;
  /** Passed on as `names` (the name generator is available). */
  names: boolean;
}

export const DEFAULT_IMPORT_OPTIONS: ImportOptions = {
  mode: "check",
  rule: "with_yield",
  reopenRegistered: false,
  allowWarnings: false,
  names: false,
};

/** When the page shows fewer products than the list, polling goes on this long after its answer became stable. */
export const IMPORT_FEWER_PRODUCTS_GRACE_MS = 10_000;
/** A status request that has no answer after this long counts as "no answer" (still loading). */
export const IMPORT_STATUS_TIMEOUT_MS = 10_000;

export const SKIP_NO_PRODUCTS = "the list shows no product for this experiment";
export const SKIP_NO_YIELD = "no product with a yield in the list";
export const SKIP_ALL_REGISTERED = "every product has an ID already";
export const YIELD_FALLBACK_NOTE = "The yield could not be read from the page; the list's value was used.";
/**
 * Why the walk stopped after IMPORT_MAX_OPEN_FAILURES experiments in a row could not be opened.
 * (Not "... in this browser?": that word must not appear in the Chrome bundle, see tests/chrome-build.test.ts.)
 */
export const OPEN_FAILURES_MESSAGE = "The experiments could not be opened. Is the ELN still logged in? Open it in another tab of this window to check.";
export const TAB_CLOSED_MESSAGE = "The import's tab was closed, so the import stopped.";
export const STOPPED_MESSAGE = "The import was stopped.";

// ------------------------------------------------------------------ plan

export interface RegisteredBatch {
  id: string;
  /** The ELN number stored with the batch. */
  code: string | null;
  isolated: number | null;
  analytical: number | null;
}

export interface PlanProduct {
  index: number;
  listed: ElnProduct;
  /** The batch the registry holds for this experiment and product number; null when none. */
  registered: RegisteredBatch | null;
  /** The list's ELN number equals the one stored with the batch; null without a batch or when either is missing. */
  codeMatches: boolean | null;
  /** The list's isolated yield ("first") against the registry's ("second"); null without a batch. */
  yieldComparison: YieldComparison | null;
}

export interface PlanExperiment {
  experiment: ElnExperiment;
  products: PlanProduct[];
  /** Batches the registry holds for this experiment under a product number the list does not have. */
  extraBatches: (RegisteredBatch & { index: number })[];
}

export interface ImportPlan {
  /** In walking order: natural order of the ELN number, experiments without one last (by pk). */
  experiments: PlanExperiment[];
}

export type LoadBatchesResult = { ok: true; rows: YieldRow[] } | { ok: false; error: HostError };

/** Every batch of the registry (`yields`, page by page until `total`), retired ones left out. */
export async function loadRegistryBatches(client: HostClient, pageSize: number = MAX_YIELD_PAGE_SIZE): Promise<LoadBatchesResult> {
  const rows: YieldRow[] = [];
  let offset = 0;
  for (;;) {
    let reply;
    try {
      reply = await client.request("yields", { offset, limit: pageSize });
    } catch (error) {
      return { ok: false, error: { code: "internal_error", message: String(error) } };
    }
    if (!reply.ok) return { ok: false, error: reply.error };
    const items = reply.result.items;
    for (const row of items) if (!row.retired) rows.push(row);
    offset += items.length;
    if (items.length === 0 || offset >= reply.result.total) break;
  }
  return { ok: true, rows };
}

/** "ABC-2" before "ABC-10"; ignores case. */
export function naturalCompare(a: string, b: string): number {
  const ta = a.toLowerCase().match(/\d+|\D+/g) ?? [];
  const tb = b.toLowerCase().match(/\d+|\D+/g) ?? [];
  for (let i = 0; i < Math.min(ta.length, tb.length); i++) {
    const x = ta[i]!;
    const y = tb[i]!;
    if (x === y) continue;
    const xd = /^\d/.test(x);
    const yd = /^\d/.test(y);
    if (xd && yd) {
      const c = compareDigits(x, y);
      if (c !== 0) return c;
      continue;
    }
    return x < y ? -1 : 1;
  }
  return ta.length - tb.length;
}

function compareDigits(x: string, y: string): number {
  const a = x.replace(/^0+(?=\d)/, "");
  const b = y.replace(/^0+(?=\d)/, "");
  if (a.length !== b.length) return a.length - b.length;
  return a < b ? -1 : a > b ? 1 : 0;
}

/** Walking order: by ELN number (natural order), experiments without one last, then by pk. */
export function compareExperiments(a: ElnExperiment, b: ElnExperiment): number {
  if (a.code !== null && b.code === null) return -1;
  if (a.code === null && b.code !== null) return 1;
  if (a.code !== null && b.code !== null) {
    const c = naturalCompare(a.code, b.code);
    if (c !== 0) return c;
  }
  return compareDigits(a.ref, b.ref);
}

function sameCode(a: string, b: string): boolean {
  return a.trim().toLowerCase() === b.trim().toLowerCase();
}

function batchOf(row: YieldRow): RegisteredBatch {
  return {
    id: row.full_id,
    code: row.experiment_code?.trim() ? row.experiment_code.trim() : null,
    isolated: row.yield?.isolated ?? null,
    analytical: row.yield?.analytical ?? null,
  };
}

/** Matches the listed products with the registry's batches. Pure. */
export function buildPlan(experiments: readonly ElnExperiment[], rows: readonly YieldRow[]): ImportPlan {
  const byRef = new Map<string, Map<number, RegisteredBatch>>();
  for (const row of rows) {
    if (row.retired) continue;
    let slots = byRef.get(row.experiment_ref);
    if (!slots) byRef.set(row.experiment_ref, (slots = new Map()));
    if (!slots.has(row.product_index)) slots.set(row.product_index, batchOf(row));
  }
  const planned = [...experiments].sort(compareExperiments).map((experiment): PlanExperiment => {
    const slots = byRef.get(experiment.ref) ?? new Map<number, RegisteredBatch>();
    const products = experiment.products.map((listed): PlanProduct => {
      const registered = slots.get(listed.index) ?? null;
      return {
        index: listed.index,
        listed,
        registered,
        codeMatches: registered && registered.code !== null && experiment.code !== null ? sameCode(registered.code, experiment.code) : null,
        yieldComparison: registered ? compareYields(listed.isolated, registered.isolated) : null,
      };
    });
    const listedIndexes = new Set(experiment.products.map((p) => p.index));
    const extraBatches = [...slots]
      .filter(([index]) => !listedIndexes.has(index))
      .sort(([a], [b]) => a - b)
      .map(([index, batch]) => ({ ...batch, index }));
    return { experiment, products, extraBatches };
  });
  return { experiments: planned };
}

export type PlanDecision = { open: true } | { open: false; reason: string };

/** The listed products the walk is about under `rule` (per the list and the plan). */
export function handledProducts(exp: PlanExperiment, rule: ImportRule): PlanProduct[] {
  if (rule === "all") return exp.products;
  return exp.products.filter((p) => p.registered !== null || (p.listed.isolated !== null && p.listed.isolated > 0));
}

/** Whether an experiment is opened, decided from the list and the plan alone. */
export function planDecision(exp: PlanExperiment, options: Pick<ImportOptions, "rule" | "reopenRegistered">): PlanDecision {
  if (exp.products.length === 0) return { open: false, reason: SKIP_NO_PRODUCTS };
  const handled = handledProducts(exp, options.rule);
  if (handled.length === 0) return { open: false, reason: SKIP_NO_YIELD };
  if (!options.reopenRegistered && handled.every((p) => p.registered !== null)) return { open: false, reason: SKIP_ALL_REGISTERED };
  return { open: true };
}

/** For the confirmation: experiments to open, and the listed products there that may get an ID. */
export function plannedWork(plan: ImportPlan, options: Pick<ImportOptions, "rule" | "reopenRegistered">): { experiments: number; products: number } {
  let experiments = 0;
  let products = 0;
  for (const exp of plan.experiments) {
    if (!planDecision(exp, options).open) continue;
    experiments += 1;
    products += handledProducts(exp, options.rule).filter((p) => p.registered === null).length;
  }
  return { experiments, products };
}

// ------------------------------------------------------------------ walk state

export type ExperimentPhase = "waiting" | "opening" | "working" | "done" | "skipped" | "not opened";

export type RowKind =
  /** Before the walk: the experiment will be opened. */
  | "planned"
  | "skipped"
  | "not_opened"
  /** The walk stopped before this experiment. */
  | "not_reached"
  | "busy"
  /** Not sent: not chosen under the rule. */
  | "not_handled"
  | ImportOutcomeKind;

export interface ProductRow {
  index: number;
  listed: ElnProduct | null;
  registered: RegisteredBatch | null;
  kind: RowKind;
  /** Plain words for the user. */
  result: string;
  outcome: ImportOutcome | null;
  /** The page's yields for the row (null: not read or not usable). */
  pageYields: PageYields | null;
  /** Differences found between the page, the list and the registry. */
  findings: string[];
  notes: string[];
}

export interface ExperimentRun {
  plan: PlanExperiment;
  decision: PlanDecision;
  phase: ExperimentPhase;
  /** Why it was skipped / not opened / had nothing to do / failed. */
  reason: string | null;
  rows: ProductRow[];
  /** About the experiment as a whole (ELN number, number of products). */
  findings: string[];
  notes: string[];
  /** How long it took (opened experiments). */
  duration: number | null;
}

export type ImportStatus = "idle" | "running" | "stopping" | "finished";

export interface ImportState {
  fileName: string | null;
  parsed: ElnParseResult | null;
  planLoading: boolean;
  planError: HostError | null;
  plan: ImportPlan | null;
  options: ImportOptions;
  /** One per planned experiment, in walking order: the preview before a walk, the results during and after it. */
  runs: readonly ExperimentRun[];
  status: ImportStatus;
  /** The mode of the walk shown (null before the first walk). */
  runMode: ImportMode | null;
  /** Index into `runs` of the experiment being handled. */
  current: number | null;
  /** Experiments to open in this walk, and how many were handled so far. */
  toOpen: number;
  handled: number;
  /** Why the walk ended early. */
  message: string | null;
}

export interface ImportCounters {
  created: number;
  exists: number;
  wouldCreate: number;
  attention: number;
  failed: number;
  /** Experiments. */
  skipped: number;
  /** Experiments. */
  notOpened: number;
  findings: number;
}

export interface DistinctCompounds {
  /** Products that would get an ID. */
  products: number;
  /** Distinct compounds among them (by `compound_key`). */
  compounds: number;
  newCores: number;
  newStereos: number;
  furtherBatches: number;
}

// ------------------------------------------------------------------ texts

export function experimentLabel(exp: Pick<ElnExperiment, "ref" | "code">): string {
  return exp.code ?? `[${exp.ref}]`;
}

function percent(value: number): string {
  return `${formatPercent(value)} %`;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function planNotes(exp: PlanExperiment, p: PlanProduct): string[] {
  const notes: string[] = [];
  const r = p.registered;
  if (!r) return notes;
  if (p.codeMatches === false) notes.push(`The registry stores ${r.id} with the ELN number ${r.code}; the list says ${exp.experiment.code}.`);
  if (p.yieldComparison === "different") {
    notes.push(`The registry holds an isolated yield of ${percent(r.isolated!)}; the list says ${percent(p.listed.isolated!)}.`);
  } else if (p.yieldComparison === "only_first") {
    notes.push(`The list has an isolated yield of ${percent(p.listed.isolated!)}; the registry has none stored for ${r.id}.`);
  } else if (p.yieldComparison === "only_second") {
    notes.push(`The registry holds an isolated yield of ${percent(r.isolated!)} for ${r.id}; the list has none.`);
  }
  return notes;
}

function experimentNotes(exp: PlanExperiment): string[] {
  return exp.extraBatches.map(
    (b) => `The registry has ${b.id} as product ${b.index} of this experiment, but the list shows ${plural(exp.products.length, "product", "products")}.`,
  );
}

const PLANNED_TEXT = "will be opened";
const NOT_REACHED_TEXT = "not reached: the import ended before this experiment";

function previewRun(plan: PlanExperiment, options: Pick<ImportOptions, "rule" | "reopenRegistered">): ExperimentRun {
  const decision = planDecision(plan, options);
  const rows = plan.products.map(
    (p): ProductRow => ({
      index: p.index,
      listed: p.listed,
      registered: p.registered,
      kind: decision.open ? "planned" : "skipped",
      result: decision.open ? PLANNED_TEXT : `skipped: ${decision.reason}`,
      outcome: null,
      pageYields: null,
      findings: [],
      notes: planNotes(plan, p),
    }),
  );
  return {
    plan,
    decision,
    phase: decision.open ? "waiting" : "skipped",
    reason: decision.open ? null : decision.reason,
    rows,
    findings: [],
    notes: experimentNotes(plan),
    duration: null,
  };
}

function outcomeText(outcome: ImportOutcome): string {
  switch (outcome.kind) {
    case "exists":
      return "has an ID already";
    case "created":
      if (outcome.core_created) return "ID created (new compound)";
      if (outcome.stereo_created) return "ID created (new stereo variant)";
      return "ID created";
    case "would_create":
      if (outcome.status === "new") return "would get an ID (new compound)";
      if (outcome.status === "known_core") return "would get an ID (new stereo variant of a known compound)";
      if (outcome.status === "known_compound") return "would get an ID (another batch of a known compound)";
      return "would get an ID";
    case "attention":
      return `needs a look: ${outcome.message ?? "the structure needs a decision in the panel"}`;
    case "failed":
      return `failed: ${outcome.message ?? "no reason given"}`;
  }
}

// ------------------------------------------------------------------ counting and reporting

/** Counters of the walk (or of the preview: skipped experiments only). */
export function countRuns(runs: readonly ExperimentRun[]): ImportCounters {
  const c: ImportCounters = { created: 0, exists: 0, wouldCreate: 0, attention: 0, failed: 0, skipped: 0, notOpened: 0, findings: 0 };
  for (const run of runs) {
    if (run.phase === "skipped") c.skipped += 1;
    if (run.phase === "not opened") c.notOpened += 1;
    c.findings += run.findings.length;
    for (const row of run.rows) {
      c.findings += row.findings.length;
      if (row.kind === "created") c.created += 1;
      else if (row.kind === "exists") c.exists += 1;
      else if (row.kind === "would_create") c.wouldCreate += 1;
      else if (row.kind === "attention") c.attention += 1;
      else if (row.kind === "failed") c.failed += 1;
    }
  }
  return c;
}

/** The products a check found would get an ID, as distinct compounds (products sharing a `compound_key` are one compound). */
export function distinctCompounds(runs: readonly ExperimentRun[]): DistinctCompounds {
  const groups = new Map<string, AnalysisStatus | null>();
  let products = 0;
  let anonymous = 0;
  for (const run of runs) {
    for (const row of run.rows) {
      const o = row.outcome;
      if (row.kind !== "would_create" || !o) continue;
      products += 1;
      const key = o.compound_key ?? `\u0000${anonymous++}`;
      if (!groups.has(key)) groups.set(key, o.status);
    }
  }
  let newCores = 0;
  let newStereos = 0;
  let furtherBatches = 0;
  for (const status of groups.values()) {
    if (status === "new") newCores += 1;
    else if (status === "known_core") newStereos += 1;
    else if (status === "known_compound") furtherBatches += 1;
  }
  return { products, compounds: groups.size, newCores, newStereos, furtherBatches };
}

/** "N products would get an ID: K new compounds (new CORE), L new stereo variants of known compounds, M further batches of known compounds." */
export function distinctLine(d: DistinctCompounds): string {
  return (
    `${plural(d.products, "product", "products")} would get an ID: ` +
    `${plural(d.newCores, "new compound", "new compounds")} (new CORE), ` +
    `${plural(d.newStereos, "new stereo variant", "new stereo variants")} of known compounds, ` +
    `${plural(d.furtherBatches, "further batch", "further batches")} of known compounds.`
  );
}

/** The counters as one sentence. */
export function countersLine(c: ImportCounters, mode: ImportMode): string {
  const parts: string[] = [];
  if (mode === "create") parts.push(`${plural(c.created, "ID", "IDs")} created`);
  else parts.push(`${plural(c.wouldCreate, "product", "products")} would get an ID`);
  parts.push(`${plural(c.exists, "product", "products")} had an ID already`);
  if (mode === "create" && c.wouldCreate > 0) parts.push(`${c.wouldCreate} would get an ID`);
  parts.push(`${c.attention} ${c.attention === 1 ? "needs" : "need"} a look`);
  parts.push(`${c.failed} failed`);
  parts.push(`${plural(c.skipped, "experiment", "experiments")} skipped`);
  parts.push(`${c.notOpened} not opened`);
  parts.push(`${plural(c.findings, "difference", "differences")} to check`);
  return `${parts.join(", ")}.`;
}

/** The summary shown when a walk ended. */
export function summaryLines(state: ImportState): string[] {
  if (state.status !== "finished" || state.runMode === null) return [];
  const counters = countRuns(state.runs);
  const lines: string[] = [];
  const early = state.message !== null;
  const done = `${state.handled} of ${plural(state.toOpen, "experiment", "experiments")}`;
  if (state.runMode === "check") {
    lines.push(early ? `The check ended after ${done}. Nothing was created.` : "The check is finished. Nothing was created or stored.");
  } else {
    lines.push(early ? `The import ended after ${done}.` : "The import is finished.");
  }
  if (state.message) lines.push(state.message);
  lines.push(countersLine(counters, state.runMode));
  if (state.runMode === "check") lines.push(distinctLine(distinctCompounds(state.runs)));
  return lines;
}

/** "Experiment 12 of 700: ABC-012 …" while a walk runs. */
export function progressLine(state: ImportState): string | null {
  if ((state.status !== "running" && state.status !== "stopping") || state.current === null) return null;
  const run = state.runs[state.current];
  if (!run) return null;
  return `Experiment ${Math.min(state.handled + 1, state.toOpen)} of ${state.toOpen}: ${experimentLabel(run.plan.experiment)} …`;
}

/** "about 12 min left", from the mean time per experiment so far; null before the first one. */
export function timeLeftText(state: ImportState): string | null {
  if (state.status !== "running" && state.status !== "stopping") return null;
  const durations = state.runs.map((r) => r.duration).filter((d): d is number => d !== null);
  if (durations.length === 0) return null;
  const mean = durations.reduce((a, b) => a + b, 0) / durations.length + IMPORT_PAUSE_MS;
  const remaining = Math.max(0, state.toOpen - state.handled);
  const minutes = Math.round((mean * remaining) / 60_000);
  if (remaining === 0) return null;
  return minutes < 1 ? "less than a minute left" : `about ${minutes} min left`;
}

export const REPORT_HEADER = [
  "ELN number",
  "pk",
  "Product",
  "Result",
  "ID",
  "Formula on page",
  "Formula in list",
  "Isolated yield on page (%)",
  "Isolated yield in list (%)",
  "Isolated yield in registry (%)",
  "Findings and notes",
] as const;

const num = (value: number | null | undefined): string => (value === null || value === undefined ? "" : formatPercent(value));

/** One line per product row (header first): text cells, spreadsheet formulas guarded. */
export function reportRows(runs: readonly ExperimentRun[]): string[][] {
  const out: string[][] = [[...REPORT_HEADER]];
  for (const run of runs) {
    const exp = run.plan.experiment;
    const head = [...run.findings.map((f) => `Check: ${f}`), ...run.notes];
    if (run.rows.length === 0) {
      const reason = run.reason ?? "";
      out.push([guardText(exp.code ?? ""), exp.ref, "", guardText(reason), "", "", "", "", "", "", guardText(head.join(" "))]);
      continue;
    }
    run.rows.forEach((row, i) => {
      const remarks = [...(i === 0 ? head : []), ...row.findings.map((f) => `Check: ${f}`), ...row.notes];
      out.push([
        guardText(exp.code ?? ""),
        exp.ref,
        String(row.index),
        guardText(row.result),
        guardText(row.outcome?.id ?? row.registered?.id ?? ""),
        guardText(row.outcome?.formula ?? ""),
        guardText(row.listed?.formula ?? ""),
        num(row.pageYields?.isolated),
        num(row.listed?.isolated),
        num(row.registered?.isolated),
        guardText(remarks.join(" ")),
      ]);
    });
  }
  return out;
}

export function reportCsv(runs: readonly ExperimentRun[]): string {
  return csvText(reportRows(runs));
}

export function reportTsv(runs: readonly ExperimentRun[]): string {
  return tsvText(reportRows(runs));
}

// ------------------------------------------------------------------ answers of the content script

type StatusView =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; code: string | null; products: ImportPageProduct[]; raw: unknown };

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readYields(value: unknown): PageYields | null | undefined {
  if (value === null) return null;
  if (!isObject(value)) return undefined;
  const ok = (v: unknown) => v === null || (typeof v === "number" && Number.isFinite(v));
  if (!ok(value.isolated) || !ok(value.analytical)) return undefined;
  return { isolated: value.isolated as number | null, analytical: value.analytical as number | null };
}

function readPageProduct(value: unknown): ImportPageProduct | null {
  if (!isObject(value)) return null;
  if (typeof value.index !== "number" || !Number.isInteger(value.index) || value.index < 1) return null;
  const yields = readYields(value.yields);
  if (yields === undefined) return null;
  return {
    index: value.index,
    structure: value.structure === true,
    problem: typeof value.problem === "string" ? value.problem : null,
    yields,
  };
}

/** What a status answer means for the walk; anything unexpected counts as "still loading". */
function readStatus(answer: unknown, ref: string): StatusView {
  if (!isObject(answer)) return { kind: "loading" };
  if (answer.state === "error") return { kind: "error", message: typeof answer.message === "string" ? answer.message : "unknown error" };
  if (answer.state !== "ready") return { kind: "loading" };
  const experiment = answer.experiment;
  if (!isObject(experiment) || experiment.ref !== ref || !Array.isArray(answer.products)) return { kind: "loading" };
  const products: ImportPageProduct[] = [];
  for (const item of answer.products) {
    const product = readPageProduct(item);
    if (!product) return { kind: "loading" };
    products.push(product);
  }
  const code = typeof experiment.code === "string" && experiment.code.trim() !== "" ? experiment.code.trim() : null;
  return { kind: "ready", code, products, raw: answer };
}

const OUTCOME_KINDS = new Set<string>(["exists", "created", "would_create", "attention", "failed"]);

function readOutcome(value: unknown): ImportOutcome | null {
  if (!isObject(value) || typeof value.index !== "number" || typeof value.kind !== "string" || !OUTCOME_KINDS.has(value.kind)) return null;
  const text = (v: unknown) => (typeof v === "string" ? v : null);
  const yields = readYields(value.yields);
  return {
    index: value.index,
    kind: value.kind as ImportOutcomeKind,
    id: text(value.id),
    core_stereo_id: text(value.core_stereo_id),
    status: text(value.status) as AnalysisStatus | null,
    compound_key: text(value.compound_key),
    inchikey: text(value.inchikey),
    formula: text(value.formula),
    stereo_mode: text(value.stereo_mode) as ImportOutcome["stereo_mode"],
    core_created: value.core_created === true,
    stereo_created: value.stereo_created === true,
    message: text(value.message),
    notes: Array.isArray(value.notes) ? value.notes.filter((n): n is string => typeof n === "string") : [],
    yields: yields === undefined ? null : yields,
    yield_result: (["saved", "unchanged", "failed", "none"].includes(value.yield_result as string)
      ? value.yield_result
      : "none") as ImportOutcome["yield_result"],
    name: text(value.name),
  };
}

type ApplyView =
  | { ok: true; outcomes: Map<number, ImportOutcome>; reactionNote: string | null }
  | { ok: false; message: string };

function readApply(answer: unknown): ApplyView {
  if (!isObject(answer)) {
    return {
      ok: false,
      message: "the experiment page did not answer in time. Look at this experiment in the ELN: IDs may have been created.",
    };
  }
  if (answer.ok !== true) return { ok: false, message: typeof answer.message === "string" ? answer.message : "the page refused the request" };
  const outcomes = new Map<number, ImportOutcome>();
  if (Array.isArray(answer.products)) {
    for (const item of answer.products) {
      const outcome = readOutcome(item);
      if (outcome && !outcomes.has(outcome.index)) outcomes.set(outcome.index, outcome);
    }
  }
  const reactionNote =
    answer.reaction === "failed"
      ? `The reaction could not be stored${typeof answer.reaction_message === "string" ? `: ${answer.reaction_message}` : "."}`
      : null;
  return { ok: true, outcomes, reactionNote };
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    const y = b as unknown[];
    return a.length === y.length && a.every((v, i) => deepEqual(v, y[i]));
  }
  const x = a as Record<string, unknown>;
  const y = b as Record<string, unknown>;
  const kx = Object.keys(x);
  const ky = Object.keys(y);
  return kx.length === ky.length && kx.every((k) => Object.prototype.hasOwnProperty.call(y, k) && deepEqual(x[k], y[k]));
}

// ------------------------------------------------------------------ findings

function yieldFinding(page: PageYields | null, listed: ElnProduct | null): string | null {
  if (!page || !listed) return null;
  const p = page.isolated;
  const l = listed.isolated;
  if (p === null && l === null) return null;
  if (p !== null && l !== null) {
    return sameYield(p, l) ? null : `The page shows an isolated yield of ${percent(p)}; the list says ${percent(l)}.`;
  }
  if (p === null) return `The page shows no isolated yield; the list says ${percent(l!)}.`;
  return `The page shows an isolated yield of ${percent(p)}; the list has none.`;
}

function productFindings(row: ProductRow, outcome: ImportOutcome | null): string[] {
  const findings: string[] = [];
  if (outcome && row.listed && compareFormulas(outcome.formula, row.listed.formula) === "different") {
    findings.push(`The structure on the page has the formula ${outcome.formula}; the list says ${row.listed.formula}.`);
  }
  const y = yieldFinding(row.pageYields, row.listed);
  if (y) findings.push(y);
  if (outcome && row.registered && outcome.id !== row.registered.id) {
    findings.push(
      outcome.id
        ? `The registry had ${row.registered.id} for this product, but now ${outcome.id} was reported.`
        : `The registry had ${row.registered.id} for this product, but the page's structure was not found under it.`,
    );
  }
  return findings;
}

// ------------------------------------------------------------------ outside access

export interface ImportTabs {
  create(props: { url: string; active: boolean }): Promise<{ id?: number }>;
  update(tabId: number, props: { url: string }): Promise<unknown>;
  sendMessage(tabId: number, message: ImportRequest): Promise<unknown>;
  remove(tabId: number): Promise<void>;
  /** Calls `listener` with the id of every tab that is closed; returns an unsubscribe function. */
  onRemoved(listener: (tabId: number) => void): () => void;
}

export interface ImportTimers {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const DEFAULT_TIMERS: ImportTimers = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export interface ImportDeps {
  client: HostClient;
  tabs: ImportTabs;
  timers?: ImportTimers;
  /** Rows per `yields` request while planning. */
  pageSize?: number;
}

export interface ImportHooks {
  onChange?(): void;
  announce?(message: string): void;
}

type WaitResult =
  | { kind: "ready"; view: Extract<StatusView, { kind: "ready" }> }
  | { kind: "not_opened"; reason: string }
  | { kind: "closed" };

type HandleResult = "ok" | "not_opened" | "abort";

// ------------------------------------------------------------------ controller

export class ImportController {
  private s: ImportState = {
    fileName: null,
    parsed: null,
    planLoading: false,
    planError: null,
    plan: null,
    options: { ...DEFAULT_IMPORT_OPTIONS },
    runs: [],
    status: "idle",
    runMode: null,
    current: null,
    toOpen: 0,
    handled: 0,
    message: null,
  };
  private readonly listeners = new Set<() => void>();
  private readonly timers: ImportTimers;
  private planSeq = 0;
  private stopRequested = false;
  private tabId: number | null = null;
  private tabClosed = false;
  /** Pending sleeps and waits; woken when the tab is closed (all) or on stop (those marked). */
  private readonly wakers = new Set<{ wake: () => void; onStop: boolean }>();

  constructor(
    private readonly deps: ImportDeps,
    private readonly hooks: ImportHooks = {},
  ) {
    this.timers = deps.timers ?? DEFAULT_TIMERS;
  }

  get state(): Readonly<ImportState> {
    return this.s;
  }

  /** Called after every change of `state`. Returns an unsubscribe function. */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private update(patch: Partial<ImportState>): void {
    this.s = { ...this.s, ...patch };
    this.hooks.onChange?.();
    for (const l of [...this.listeners]) l();
  }

  private announce(message: string): void {
    this.hooks.announce?.(message);
  }

  isRunning(): boolean {
    return this.s.status === "running" || this.s.status === "stopping";
  }

  // ---------------------------------------------------------------- file and plan

  /** Reads a chosen file and compares it with the registry. Opens nothing. */
  async loadFile(name: string, bytes: Uint8Array | ArrayBuffer): Promise<void> {
    if (this.isRunning()) return;
    const parsed = parseElnCsv(bytes);
    this.planSeq += 1;
    this.update({
      fileName: name,
      parsed,
      plan: null,
      planError: null,
      planLoading: false,
      runs: [],
      status: "idle",
      runMode: null,
      current: null,
      toOpen: 0,
      handled: 0,
      message: null,
    });
    if (parsed.error !== null) {
      this.announce(parsed.error);
      return;
    }
    await this.loadPlan();
  }

  /** Loads the registry's batches and builds the plan; false when it failed or was superseded. */
  async loadPlan(): Promise<boolean> {
    const parsed = this.s.parsed;
    if (!parsed || parsed.error !== null) return false;
    const seq = ++this.planSeq;
    this.update({ planLoading: true, planError: null });
    const loaded = await loadRegistryBatches(this.deps.client, this.deps.pageSize);
    if (seq !== this.planSeq) return false;
    if (!loaded.ok) {
      this.update({ planLoading: false, planError: loaded.error });
      this.announce(`The list could not be compared with the registry: ${loaded.error.message}`);
      return false;
    }
    const plan = buildPlan(parsed.experiments, loaded.rows);
    const patch: Partial<ImportState> = { planLoading: false, plan };
    if (!this.isRunning()) Object.assign(patch, { runs: plan.experiments.map((e) => previewRun(e, this.s.options)), status: "idle" as const, runMode: null, message: null });
    this.update(patch);
    if (!this.isRunning()) this.announce(this.planLine() ?? "");
    return true;
  }

  /** "9 experiments in the list, 7 will be opened, 2 skipped. 3 products have an ID already." */
  planLine(): string | null {
    const plan = this.s.plan;
    if (!plan) return null;
    const total = plan.experiments.length;
    const open = plan.experiments.filter((e) => planDecision(e, this.s.options).open).length;
    const registered = plan.experiments.reduce((n, e) => n + e.products.filter((p) => p.registered).length, 0);
    return (
      `${plural(open, "experiment", "experiments")} of ${total} will be opened, ${total - open} skipped. ` +
      `${plural(registered, "listed product has", "listed products have")} an ID already.`
    );
  }

  /** Changes rule and checkboxes; the preview follows while no walk has run on this plan. */
  setOptions(patch: Partial<ImportOptions>): void {
    const options = { ...this.s.options, ...patch };
    if (this.isRunning()) return;
    const preview = this.s.plan && this.s.status === "idle" ? { runs: this.s.plan.experiments.map((e) => previewRun(e, options)) } : {};
    this.update({ options, ...preview });
  }

  /** For the confirmation: experiments to open and products that may get an ID, under the current options. */
  plannedWork(options: Partial<ImportOptions> = {}): { experiments: number; products: number } {
    if (!this.s.plan) return { experiments: 0, products: 0 };
    return plannedWork(this.s.plan, { ...this.s.options, ...options });
  }

  // ---------------------------------------------------------------- the walk

  /**
   * Checks ("check") or creates ("create") the products of the listed
   * experiments, one experiment at a time. The plan is loaded again first
   * (the registry may have changed). Resolves when the walk has ended.
   */
  async run(patch: Partial<ImportOptions> = {}): Promise<void> {
    if (this.isRunning()) return;
    const parsed = this.s.parsed;
    if (!parsed || parsed.error !== null) return;
    const options: ImportOptions = { ...this.s.options, ...patch };
    this.stopRequested = false;
    this.tabClosed = false;
    this.tabId = null;
    this.abortMessage = null;
    this.update({ options, status: "running", runMode: options.mode, current: null, toOpen: 0, handled: 0, message: null });
    const planned = await this.loadPlan();
    const plan = this.s.plan;
    if (!planned || !plan) {
      this.update({ status: "idle", runMode: null });
      return;
    }
    const runs = plan.experiments.map((e) => previewRun(e, options));
    const toOpen = runs.filter((r) => r.decision.open).length;
    this.update({ runs, toOpen });
    this.announce(
      options.mode === "check"
        ? `Checking ${plural(toOpen, "experiment", "experiments")} in a background tab. Nothing is created.`
        : `Creating IDs: ${plural(toOpen, "experiment", "experiments")} are opened one after the other in a background tab.`,
    );

    const unsubscribe = this.deps.tabs.onRemoved((id) => {
      if (id !== this.tabId) return;
      this.tabId = null;
      this.tabClosed = true;
      for (const w of [...this.wakers]) w.wake();
    });
    let message: string | null = null;
    let failuresInRow = 0;
    let first = true;
    try {
      for (let i = 0; i < runs.length; i++) {
        if (!this.s.runs[i]!.decision.open) continue;
        if (this.stopRequested) {
          message = STOPPED_MESSAGE;
          break;
        }
        if (!first) await this.sleep(IMPORT_PAUSE_MS, true);
        first = false;
        if (this.tabClosed) {
          message = TAB_CLOSED_MESSAGE;
          break;
        }
        if (this.stopRequested) {
          message = STOPPED_MESSAGE;
          break;
        }
        const result = await this.handle(i, options);
        if (result === "abort") {
          message = this.tabClosed ? TAB_CLOSED_MESSAGE : (this.abortMessage ?? STOPPED_MESSAGE);
          break;
        }
        if (result === "not_opened") {
          failuresInRow += 1;
          if (failuresInRow >= IMPORT_MAX_OPEN_FAILURES) {
            message = OPEN_FAILURES_MESSAGE;
            break;
          }
        } else {
          failuresInRow = 0;
        }
      }
      if (message === null && this.stopRequested && this.s.handled < this.s.toOpen) message = STOPPED_MESSAGE;
    } finally {
      unsubscribe();
      await this.closeTab();
    }
    const finalRuns = this.s.runs.map((run) =>
      run.phase === "waiting"
        ? { ...run, rows: run.rows.map((row) => ({ ...row, kind: "not_reached" as const, result: NOT_REACHED_TEXT })) }
        : run,
    );
    this.update({ runs: finalRuns, status: "finished", current: null, message });
    this.announce(summaryLines(this.s).join(" "));
  }

  /** Ends the walk after the experiment being handled now; the pause before the next one is cut short. */
  stop(): void {
    if (!this.isRunning()) return;
    this.stopRequested = true;
    for (const w of [...this.wakers]) if (w.onStop) w.wake();
    if (this.s.status === "running") this.update({ status: "stopping" });
  }

  /** The page is left: stop, and close the import's tab now. */
  abandon(): void {
    this.stop();
    void this.closeTab();
  }

  private abortMessage: string | null = null;

  private async closeTab(): Promise<void> {
    const id = this.tabId;
    this.tabId = null;
    if (id === null || this.tabClosed) return;
    try {
      await this.deps.tabs.remove(id);
    } catch {
      // already gone
    }
  }

  private sleep(ms: number, onStop: boolean): Promise<void> {
    return new Promise((resolve) => {
      const entry = {
        onStop,
        wake: () => {
          this.timers.clearTimeout(handle);
          this.wakers.delete(entry);
          resolve();
        },
      };
      const handle = this.timers.setTimeout(() => entry.wake(), ms);
      this.wakers.add(entry);
      if (this.tabClosed || (onStop && this.stopRequested)) entry.wake();
    });
  }

  /** Sends one message to the import's tab; undefined for no answer, a rejection, a timeout or a closed tab. */
  private async ask(message: ImportRequest, ms: number): Promise<unknown> {
    const tabId = this.tabId;
    if (tabId === null) return undefined;
    let entry: { wake: () => void; onStop: boolean } | null = null;
    let handle: unknown = null;
    const giveUp = new Promise<undefined>((resolve) => {
      entry = { onStop: false, wake: () => resolve(undefined) };
      this.wakers.add(entry);
      handle = this.timers.setTimeout(() => resolve(undefined), ms);
    });
    try {
      return await Promise.race([this.deps.tabs.sendMessage(tabId, message).catch(() => undefined), giveUp]);
    } finally {
      this.timers.clearTimeout(handle);
      if (entry) this.wakers.delete(entry);
    }
  }

  private setRun(i: number, patch: Partial<ExperimentRun>, state: Partial<ImportState> = {}): void {
    const runs = [...this.s.runs];
    runs[i] = { ...runs[i]!, ...patch };
    this.update({ runs, ...state });
  }

  private async handle(i: number, options: ImportOptions): Promise<HandleResult> {
    const start = this.timers.now();
    const run = this.s.runs[i]!;
    const exp = run.plan.experiment;
    const busyRows = (text: string) => run.rows.map((row) => ({ ...row, kind: "busy" as const, result: text }));
    this.setRun(i, { phase: "opening", rows: busyRows("opening the experiment …") }, { current: i });

    const url = experimentPageUrl(exp.ref);
    try {
      if (this.tabId === null) {
        const tab = await this.deps.tabs.create({ url, active: false });
        if (typeof tab?.id !== "number") throw new Error("no tab was opened");
        this.tabId = tab.id;
      } else {
        await this.deps.tabs.update(this.tabId, { url });
      }
    } catch (error) {
      this.setRun(i, { phase: "waiting", rows: run.rows }, { current: null });
      if (!this.tabClosed) this.abortMessage = `The experiment could not be opened in a background tab: ${error instanceof Error ? error.message : String(error)}`;
      return "abort";
    }

    const waited = await this.waitReady(exp.ref, exp.products.length);
    if (waited.kind === "closed") {
      this.setRun(i, { phase: "waiting", rows: run.rows });
      return "abort";
    }
    if (waited.kind === "not_opened") {
      this.setRun(
        i,
        {
          phase: "not opened",
          reason: waited.reason,
          duration: this.timers.now() - start,
          rows: run.rows.map((row) => ({ ...row, kind: "not_opened" as const, result: `not opened: ${waited.reason}` })),
        },
        { handled: this.s.handled + 1 },
      );
      return "not_opened";
    }

    // ---------------------------------------------------------------- choose the products
    const view = waited.view;
    const findings: string[] = [];
    const listCode = exp.code;
    if (listCode !== null && view.code !== null && !sameCode(listCode, view.code)) {
      findings.push(`The ELN number on the page is ${view.code}; the list says ${listCode}.`);
    }
    if (view.products.length !== exp.products.length) {
      findings.push(
        `The page shows ${plural(view.products.length, "product", "products")}; the list ${plural(exp.products.length, "product", "products")}.`,
      );
    }
    const listed = new Map(exp.products.map((p) => [p.index, p]));
    const registered = new Map<number, RegisteredBatch>();
    for (const p of run.plan.products) if (p.registered) registered.set(p.index, p.registered);
    for (const b of run.plan.extraBatches) registered.set(b.index, b);
    const pageRows = new Map(view.products.map((p) => [p.index, p]));

    const selected = new Set<number>(registered.keys());
    const rowNotes = new Map<number, string[]>();
    const pageYields = new Map<number, PageYields | null>();
    for (const row of view.products) {
      pageYields.set(row.index, row.yields);
      if (options.rule === "all") {
        selected.add(row.index);
        continue;
      }
      let isolated: number | null;
      if (row.yields === null) {
        isolated = listed.get(row.index)?.isolated ?? null;
        rowNotes.set(row.index, [YIELD_FALLBACK_NOTE]);
      } else {
        isolated = row.yields.isolated;
      }
      if (isolated !== null && isolated > 0) selected.add(row.index);
    }
    const ordered = [...selected].filter((n) => Number.isInteger(n) && n >= 1).sort((a, b) => a - b);
    const send = ordered.slice(0, MAX_IMPORT_PRODUCTS);
    const indexes = [...new Set([...listed.keys(), ...pageRows.keys(), ...ordered])].sort((a, b) => a - b);

    const baseRow = (index: number): ProductRow => {
      const old = run.rows.find((r) => r.index === index);
      return {
        index,
        listed: listed.get(index) ?? null,
        registered: registered.get(index) ?? null,
        kind: "busy",
        result: "",
        outcome: null,
        pageYields: pageYields.get(index) ?? null,
        findings: [],
        notes: [...(old?.notes ?? []), ...(rowNotes.get(index) ?? [])],
      };
    };

    if (send.length === 0) {
      const nothingText =
        view.products.length === 0
          ? "nothing to do: the page shows no product"
          : "nothing to do: no isolated yield above 0 on the page";
      const rows = indexes.map((index): ProductRow => {
        const row = { ...baseRow(index), kind: "not_handled" as const, result: nothingText };
        return { ...row, findings: productFindings(row, null) };
      });
      this.setRun(
        i,
        { phase: "done", reason: "nothing to do", rows, findings, duration: this.timers.now() - start },
        { handled: this.s.handled + 1 },
      );
      return "ok";
    }

    this.setRun(i, {
      phase: "working",
      findings,
      rows: indexes.map((index) => ({
        ...baseRow(index),
        result: send.includes(index) ? (options.mode === "check" ? "checking …" : "working …") : "",
      })),
    });

    // ---------------------------------------------------------------- apply
    const request: ImportApplyRequest = {
      type: "import-apply",
      ref: exp.ref,
      products: send,
      dry_run: options.mode === "check",
      allow_warnings: options.allowWarnings,
      names: options.names,
    };
    const answer = await this.ask(request, IMPORT_APPLY_TIMEOUT_MS);
    const closed = this.tabClosed;
    const applied: ApplyView = closed
      ? { ok: false, message: "the import's tab was closed before this experiment was finished. Look at it in the ELN." }
      : readApply(answer);
    const runNotes = [...run.notes];
    if (applied.ok && applied.reactionNote) runNotes.push(applied.reactionNote);
    if (ordered.length > send.length) {
      runNotes.push(`Only the first ${MAX_IMPORT_PRODUCTS} products were handled; open the experiment for the others.`);
    }
    const rows = indexes.map((index): ProductRow => {
      const row = baseRow(index);
      if (!send.includes(index)) {
        const done = { ...row, kind: "not_handled" as const, result: "not handled: no isolated yield above 0 on the page" };
        return { ...done, findings: productFindings(done, null) };
      }
      if (!applied.ok) return { ...row, kind: "failed", result: `failed: ${applied.message}`, findings: productFindings(row, null) };
      const outcome = applied.outcomes.get(index) ?? null;
      if (!outcome) {
        return { ...row, kind: "failed", result: "failed: the page reported nothing for this product", findings: productFindings(row, null) };
      }
      const withOutcome: ProductRow = {
        ...row,
        kind: outcome.kind,
        result: outcomeText(outcome),
        outcome,
        pageYields: outcome.yields ?? row.pageYields,
        notes: [...row.notes, ...outcome.notes],
      };
      return { ...withOutcome, findings: productFindings(withOutcome, outcome) };
    });
    this.setRun(
      i,
      {
        phase: "done",
        reason: applied.ok ? null : applied.message,
        rows,
        findings,
        notes: runNotes,
        duration: this.timers.now() - start,
      },
      { handled: this.s.handled + 1 },
    );
    return closed ? "abort" : "ok";
  }

  /** Polls `import-status` until a trusted "ready" answer, an error, the timeout, or the tab is closed. */
  private async waitReady(ref: string, listedCount: number): Promise<WaitResult> {
    const start = this.timers.now();
    const request: ImportStatusRequest = { type: "import-status", ref };
    let previous: Extract<StatusView, { kind: "ready" }> | null = null;
    let fewerSince: number | null = null;
    for (;;) {
      await this.sleep(IMPORT_POLL_MS, false);
      if (this.tabClosed) return { kind: "closed" };
      const answer = await this.ask(request, IMPORT_STATUS_TIMEOUT_MS);
      if (this.tabClosed) return { kind: "closed" };
      const view = readStatus(answer, ref);
      if (view.kind === "error") return { kind: "not_opened", reason: `the experiment page could not be read (${view.message})` };
      if (view.kind === "ready") {
        if (previous !== null && deepEqual(previous.raw, view.raw)) {
          if (view.products.length >= listedCount) return { kind: "ready", view };
          fewerSince ??= this.timers.now();
          if (this.timers.now() - fewerSince >= IMPORT_FEWER_PRODUCTS_GRACE_MS) return { kind: "ready", view };
        } else {
          fewerSince = null;
        }
        previous = view;
      } else {
        previous = null;
        fewerSince = null;
      }
      if (this.timers.now() - start >= IMPORT_OPEN_TIMEOUT_MS) {
        if (previous !== null && fewerSince !== null) return { kind: "ready", view: previous };
        return {
          kind: "not_opened",
          reason: `the experiment did not finish loading within ${Math.round(IMPORT_OPEN_TIMEOUT_MS / 60_000)} minutes`,
        };
      }
    }
  }

  // ---------------------------------------------------------------- results

  counters(): ImportCounters {
    return countRuns(this.s.runs);
  }

  distinct(): DistinctCompounds {
    return distinctCompounds(this.s.runs);
  }

  reportCsv(): string {
    return reportCsv(this.s.runs);
  }

  reportTsv(): string {
    return reportTsv(this.s.runs);
  }
}

/** The ImportTabs of the browser (`browser.tabs`); only the import's own tab is ever addressed. */
export function browserImportTabs(api: typeof browser.tabs): ImportTabs {
  return {
    create: (props) => api.create(props),
    update: (tabId, props) => api.update(tabId, props),
    sendMessage: (tabId, message) => api.sendMessage(tabId, message),
    remove: (tabId) => api.remove(tabId),
    onRemoved: (listener) => {
      const handler = (tabId: number) => listener(tabId);
      api.onRemoved.addListener(handler);
      return () => api.onRemoved.removeListener(handler);
    },
  };
}
