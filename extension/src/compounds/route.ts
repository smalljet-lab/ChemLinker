/**
 * "Synthesis route" of the stereo variants shown on the compounds page: the
 * "Generate ChemDraw tree" button asks the host to draw every reaction that
 * led to the variant (`route`), write it as a ChemDraw file into the
 * variant's folder and open it with ChemDraw. One request per variant at a
 * time, also when the variant is hidden (collapsed, list reloaded) and shown
 * again while it runs; an answer that comes while the variant is hidden is
 * kept until it is shown again. Plus the texts the section shows. No DOM.
 *
 * Starting material ("SM") switch: each answer lists its runs (`choices`);
 * the page offers a choice per run and saves it with `set_limiting`. The
 * choice shown for an experiment is the newest one known on this page (an
 * answer that was asked for after a save, else the save), so every route
 * showing the same experiment shows the same choice. One save per experiment
 * at a time; the route is redrawn only when the user asks for it again. The
 * host answers in order, so a drawing asked for after a save was sent
 * already uses it: that save does not say "redraw".
 */

import type {
  BackgroundReply,
  HostError,
  LimitingCandidate,
  RouteChoice,
  RouteMissing,
  RouteResult,
  SetLimitingResult,
} from "../shared/protocol";
import type { HostClient } from "../content/host-client";
import type { CompoundsHooks } from "./state";

export interface RouteEntry {
  /** True while `route` runs (the button is disabled meanwhile). */
  running: boolean;
  /** The last answer; null before the first one, and while a new drawing runs. */
  result: RouteResult | null;
  error: HostError | null;
}

/** What a variant's section says about its starting-material saves since its last drawing. */
export interface LimitingNotice {
  /** A choice was saved from this section that the last drawing asked for does not include: it is out of date. */
  saved: boolean;
  /** The last save from this section failed. */
  error: HostError | null;
  /** Registry warnings of the last save from this section (empty when it failed). */
  warnings: string[];
}

export const ROUTE_HEADING = "Synthesis route";
export const ROUTE_BUTTON = "Generate ChemDraw tree";
export const ROUTE_RUNNING = "Drawing the route…";
export const ROUTE_HINT =
  "Draws every reaction that led to this compound (reactants, reagents, solvent and the runs with a yield) as a ChemDraw file in its folder, and opens it.";
export const NOTHING_TO_DRAW = "Nothing to draw yet.";
export const MISSING_HEADING = "Reactions not stored yet";
/** ChemDraw's XML format; the type of the "Save a copy" file. */
export const CDXML_TYPE = "chemical/x-cdxml";

export const SM_HEADING = "Starting material (SM)";
export const SM_AMBIGUOUS_HINT =
  "Two or more reactants have 1 eq in these experiments. The SM mass on the run line is the one of the reactant chosen here.";
export const SM_OTHERS_TOGGLE = "Change for other experiments";
export const SM_SAVED = 'Saved. Click "Generate ChemDraw tree" to redraw.';
/** The option value of the automatic choice. */
export const SM_AUTO = "auto";

interface Known {
  /** The chosen reactant row, null = automatic. */
  selection: number | null;
  /** When it became known (`clock`). */
  stamp: number;
}

export class RouteController {
  private entries = new Map<string, RouteEntry>();
  private seq = new Map<string, number>();
  private counter = 0;
  /** The variants shown at the last `retain`; null before the first one. */
  private shown: ReadonlySet<string> | null = null;
  /** Variants whose answer came while they were hidden: kept until shown again. */
  private held = new Set<string>();

  /** Orders route requests and starting-material saves. */
  private clock = 0;
  /** Per experiment: the newest starting-material choice known on this page. */
  private known = new Map<string, Known>();
  /** Per experiment: the choice being saved (null = automatic). */
  private saving = new Map<string, number | null>();
  /** Per variant. */
  private notices = new Map<string, LimitingNotice>();
  /** Per variant: when its last drawing was asked for (`clock`). */
  private routeStarts = new Map<string, number>();
  /** Variants whose "Change for other experiments" is open. */
  private othersOpen = new Set<string>();
  /** Per variant: bumped when anything its section shows besides its entry changes. */
  private stamps = new Map<string, number>();
  private focusKey: string | null = null;

  constructor(
    private readonly client: HostClient,
    private readonly hooks: CompoundsHooks,
  ) {}

  get(coreStereoId: string): RouteEntry | undefined {
    return this.entries.get(coreStereoId);
  }

  /** Changes whenever the starting-material part of the variant's section changes. */
  stamp(coreStereoId: string): number {
    return this.stamps.get(coreStereoId) ?? 0;
  }

  notice(coreStereoId: string): LimitingNotice | undefined {
    return this.notices.get(coreStereoId);
  }

  othersShown(coreStereoId: string): boolean {
    return this.othersOpen.has(coreStereoId);
  }

  /** Opens or closes "Change for other experiments" of a variant. */
  toggleOthers(coreStereoId: string): void {
    if (!this.othersOpen.delete(coreStereoId)) this.othersOpen.add(coreStereoId);
    this.touch([coreStereoId]);
    this.hooks.onChange();
  }

  /** True while a starting-material choice of this experiment is being saved. */
  isSaving(experimentRef: string): boolean {
    return this.saving.has(experimentRef);
  }

  /** The reactant row shown as chosen for a run (null = automatic): the one being saved, else the newest known. */
  selection(choice: RouteChoice): number | null {
    if (this.saving.has(choice.experiment_ref)) return this.saving.get(choice.experiment_ref) ?? null;
    const known = this.known.get(choice.experiment_ref);
    return known ? known.selection : selectionOf(choice);
  }

  /** The control to focus after the next render when the focus was lost (consumed). */
  takeFocusKey(): string | null {
    const key = this.focusKey;
    this.focusKey = null;
    return key;
  }

  /** Draws the route of one variant and opens it; nothing happens while one runs for it. */
  generate(coreStereoId: string): void {
    if (this.entries.get(coreStereoId)?.running) return;
    const seq = ++this.counter;
    this.seq.set(coreStereoId, seq);
    if (this.notices.delete(coreStereoId)) this.touch([coreStereoId]);
    this.set(coreStereoId, { running: true, result: null, error: null });
    this.hooks.announce?.(`Drawing the synthesis route of ${coreStereoId}…`);
    const startedAt = ++this.clock;
    this.routeStarts.set(coreStereoId, startedAt);
    void this.request(coreStereoId, seq, startedAt);
  }

  /**
   * Saves the starting material of one experiment (`row` null = automatic
   * again), asked from the section of `coreStereoId`. Nothing happens while a
   * save of the same experiment runs. `focusKey`: the control to focus again
   * when the save ends, if the focus was lost meanwhile.
   */
  setLimiting(coreStereoId: string, choice: RouteChoice, row: number | null, focusKey: string | null = null): void {
    const ref = choice.experiment_ref;
    if (this.saving.has(ref)) return;
    this.saving.set(ref, row);
    this.touch(this.idsShowing(ref, coreStereoId));
    this.hooks.onChange();
    void this.save(coreStereoId, choice, row, focusKey, ++this.clock);
  }

  private set(id: string, entry: RouteEntry): void {
    const next = new Map(this.entries);
    next.set(id, entry);
    this.entries = next;
    this.hooks.onChange();
  }

  private touch(ids: Iterable<string>): void {
    for (const id of ids) this.stamps.set(id, this.stamp(id) + 1);
  }

  /** The variants whose answer lists the experiment, plus `also`. */
  private idsShowing(ref: string, also: string): Set<string> {
    const ids = new Set<string>([also]);
    for (const [id, entry] of this.entries) {
      if (entry.result?.choices.some((c) => c.experiment_ref === ref)) ids.add(id);
    }
    return ids;
  }

  private async request(id: string, seq: number, startedAt: number): Promise<void> {
    let reply: BackgroundReply<"route">;
    try {
      reply = await this.client.request("route", { core_stereo_id: id, open: true });
    } catch (error) {
      reply = { ok: false, error: { code: "internal_error", message: String(error) } };
    }
    if (this.seq.get(id) !== seq) return; // a newer request (cannot happen while one runs; defensive)
    if (this.shown !== null && !this.shown.has(id)) this.held.add(id);
    if (reply.ok && reply.result.core_stereo_id === id) {
      const touched = new Set<string>();
      for (const choice of reply.result.choices) {
        const known = this.known.get(choice.experiment_ref);
        if (known && known.stamp > startedAt) continue; // saved after this drawing was asked for
        const selection = selectionOf(choice);
        if (known && known.selection === selection) continue;
        this.known.set(choice.experiment_ref, { selection, stamp: startedAt });
        for (const other of this.idsShowing(choice.experiment_ref, id)) if (other !== id) touched.add(other);
      }
      this.touch(touched);
      this.set(id, { running: false, result: reply.result, error: null });
      this.hooks.announce?.(routeAnnouncement(reply.result));
      return;
    }
    const error: HostError = reply.ok
      ? { code: "host_bad_response", message: `The answer was about ${reply.result.core_stereo_id}, not ${id}.` }
      : reply.error;
    this.set(id, { running: false, result: null, error });
    this.hooks.announce?.(`The synthesis route of ${id} could not be drawn: ${error.message}`);
  }

  private async save(id: string, choice: RouteChoice, row: number | null, focusKey: string | null, sentAt: number): Promise<void> {
    const ref = choice.experiment_ref;
    let reply: BackgroundReply<"set_limiting">;
    try {
      reply = await this.client.request("set_limiting", { experiment_ref: ref, row });
    } catch (error) {
      reply = { ok: false, error: { code: "internal_error", message: String(error) } };
    }
    this.saving.delete(ref);
    const saved: SetLimitingResult | null = reply.ok && reply.result.experiment_ref === ref ? reply.result : null;
    const ok = saved !== null;
    const previous = this.notices.get(id);
    let error: HostError | null = null;
    if (saved) {
      this.known.set(ref, { selection: saved.row, stamp: ++this.clock });
    } else {
      error = reply.ok
        ? { code: "host_bad_response", message: `The answer was about experiment ${reply.result.experiment_ref}, not ${ref}.` }
        : reply.error;
    }
    // A drawing of this section asked for after the save was sent already uses it (the host answers in order).
    const redrawn = (this.routeStarts.get(id) ?? 0) > sentAt;
    const warnings = saved ? saved.warnings : [];
    const notice: LimitingNotice = { saved: (ok && !redrawn) || previous?.saved === true, error, warnings };
    // A section that is no longer kept (variant hidden, its answer forgotten) gets no notice.
    if (this.entries.has(id) && (notice.saved || error !== null || warnings.length > 0)) this.notices.set(id, notice);
    else this.notices.delete(id);
    this.touch(this.idsShowing(ref, id));
    this.focusKey = focusKey;
    this.hooks.onChange();
    const exp = choiceExperimentText(choice);
    if (saved) {
      const parts = [limitingSavedText(exp, saved)];
      if (!redrawn) parts.push(SM_SAVED);
      if (warnings.length > 0) parts.push(`Registry warnings: ${warnings.join(" ")}`);
      this.hooks.announce?.(parts.join(" "));
    } else if (error) this.hooks.announce?.(`The starting material of ${exp} could not be saved: ${error.message}`);
  }

  /**
   * Forgets every variant not in `ids` (the variants shown now), except one
   * whose drawing still runs (so it cannot be asked twice, and its answer is
   * shown) or whose answer came while it was hidden and was not shown yet.
   * Its starting-material notice and "other experiments" state go with it.
   */
  retain(ids: ReadonlySet<string>): void {
    this.shown = new Set(ids);
    let changed = false;
    for (const id of [...this.entries.keys()]) {
      if (ids.has(id)) {
        this.held.delete(id);
        continue;
      }
      if (this.entries.get(id)?.running || this.held.has(id)) continue;
      this.entries.delete(id);
      this.seq.delete(id);
      this.routeStarts.delete(id);
      changed = true;
    }
    if (changed) this.entries = new Map(this.entries);
    for (const id of [...this.notices.keys()]) if (!this.entries.has(id)) this.notices.delete(id);
    for (const id of [...this.othersOpen]) if (!this.entries.has(id)) this.othersOpen.delete(id);
  }
}

// ------------------------------------------------------------------ texts

/** "Opened in ChemDraw: EX0004_01_route_….cdxml", else the reason it was not opened. */
export function openedLine(result: RouteResult): string | null {
  if (!result.file) return null;
  if (result.opened) return `Opened in ChemDraw: ${result.file.name}`;
  return result.open_message ?? `Written: ${result.file.name}`;
}

/** The folder of a file path on the registry's PC ("C:\a\b\x.cdxml" -> "C:\a\b"). */
export function folderOf(path: string): string {
  const cut = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
  return cut > 0 ? path.slice(0, cut) : path;
}

/** "Steps: 4 (ABC-001, ABC-002, ABC-004, …)". */
export function stepsLine(result: RouteResult): string {
  return result.experiments.length > 0 ? `Steps: ${result.steps} (${result.experiments.join(", ")})` : `Steps: ${result.steps}`;
}

/** What is shown for the experiment of a missing reaction: its ELN number, else its key. */
export function missingExperimentText(m: RouteMissing): string {
  return m.experiment_code && m.experiment_code.trim() !== "" ? m.experiment_code : `Experiment ${m.experiment_ref}`;
}

/** " (makes EX0003_01) - open it once in the ELN", after the experiment. */
export function missingTail(m: RouteMissing): string {
  return ` (makes ${m.compound}) - open it once in the ELN`;
}

/** The download name of "Save a copy". */
export function copyFileName(result: RouteResult): string {
  return result.file?.name ?? `${result.core_stereo_id}_route.cdxml`;
}

function routeAnnouncement(result: RouteResult): string {
  if (result.steps === 0) return `${result.core_stereo_id}: nothing to draw yet.`;
  return openedLine(result) ?? `${result.core_stereo_id}: ${stepsLine(result)}`;
}

// ------------------------------------------------------------------ starting material texts

/** The choice an answer reports: the user's row, else automatic (null). */
export function selectionOf(choice: RouteChoice): number | null {
  return choice.by === "user" && choice.row !== null ? choice.row : null;
}

/** A run's experiment: its ELN number, else its key. */
export function choiceExperimentText(choice: RouteChoice): string {
  return choice.experiment_code && choice.experiment_code.trim() !== "" ? choice.experiment_code : `Experiment ${choice.experiment_ref}`;
}

/** "ABC-002 (makes EX0002_01)". */
export function choiceLabel(choice: RouteChoice): string {
  return `${choiceExperimentText(choice)} (makes ${choice.compound})`;
}

/** Equivalents as written: "1", "1.2", "0.025" (3 significant digits; whole from 100). */
export function formatEq(eq: number): string {
  if (Math.abs(eq) >= 100) return String(Math.round(eq));
  return String(Number(eq.toPrecision(3)));
}

/** "Phenylethylamine (1 eq, 1.50 g)"; "Row 2" without a name; unknown parts left out. */
export function candidateLabel(c: LimitingCandidate): string {
  const name = c.name !== null && c.name.trim() !== "" ? c.name : `Row ${c.row}`;
  const parts: string[] = [];
  if (c.eq !== null) parts.push(`${formatEq(c.eq)} eq`);
  if (c.mass !== null && c.mass.trim() !== "") parts.push(c.mass);
  return parts.length > 0 ? `${name} (${parts.join(", ")})` : name;
}

/** "Automatic - Phenylethylamine (1 eq, 1.50 g)", "Automatic - none". */
export function autoOptionLabel(choice: RouteChoice): string {
  if (choice.auto_row === null) return "Automatic - none";
  const auto = choice.candidates.find((c) => c.row === choice.auto_row);
  return `Automatic - ${auto ? candidateLabel(auto) : `Row ${choice.auto_row}`}`;
}

/** The option value of a selection. */
export function selectionValue(selection: number | null): string {
  return selection === null ? SM_AUTO : String(selection);
}

/** The selection of an option value; undefined when it is none of the run's. */
export function valueSelection(choice: RouteChoice, value: string): number | null | undefined {
  if (value === SM_AUTO) return null;
  const row = Number(value);
  return choice.candidates.some((c) => c.row === row) ? row : undefined;
}

function limitingSavedText(exp: string, result: SetLimitingResult): string {
  if (result.row === null) return `${exp}: the starting material is chosen automatically again.`;
  return `${exp}: starting material set to ${result.name !== null && result.name.trim() !== "" ? result.name : `row ${result.row}`}.`;
}
