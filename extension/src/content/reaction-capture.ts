/**
 * Stores what the experiment page shows about its reaction in the registry
 * (`set_reaction`), in the background, so that the compounds page can draw
 * how a compound was made.
 *
 * When it sends:
 *   (a) right after a successful "Create ID" for a product of the experiment,
 *       whatever the page's edit mode (`afterCreate`);
 *   (b) while the experiment is shown in its saved state (every input read
 *       is read-only) and at least one of its products has a batch registered
 *       in this experiment (`setContext`), whenever the page shows something
 *       else than what was last stored from it in this page session.
 *
 * Rules:
 * - at most one capture runs at a time; a trigger meanwhile is remembered and
 *   handled once the capture ends;
 * - (b) never sends while the experiment is being edited;
 * - what was stored successfully is not sent again (signature: the page's
 *   answer without row handles, `reactionSignature`);
 * - after a failure the same content is not sent again by (b) until the page
 *   shows something else or is loaded again (no retry loop); (a) tries once more;
 * - each row's structure is read through the shared bridge client, one at a
 *   time; a structure that cannot be read is sent as null and never blocks
 *   the capture;
 * - a content stored while some structure could not be read because the
 *   server or the page was slow (timeout, failed download, no answer) is not
 *   counted as stored: only those structures are read again, by (b) after
 *   STRUCTURE_RETRY_MS or on the next page change (at most
 *   MAX_STRUCTURE_RETRIES sends, counted afresh when the experiment is shown
 *   again), and by (a) on every Create ID; the structures read before are
 *   kept;
 * - a request over MAX_SET_REACTION_BYTES (the host stops on a frame over
 *   1 MiB) is sent without its largest structures, as few as possible, and
 *   the status line says so; one still too large without any is not sent;
 * - failures are silent: they are only reported in the panel's Advanced
 *   section (`status()`), never as a popup.
 *
 * The import of many experiments stores the reaction with `storeReactionOnce`:
 * one capture like (a), on an instance of its own, awaited.
 */

import type { ExperimentContext } from "../shared/adapter";
import { BRIDGE_MOLFILE_TIMEOUT_MS, type PageReaction } from "../shared/bridge";
import { MAX_REF_TEXT, type BackgroundReply, type SetReactionParams } from "../shared/protocol";
import type { MolfileResult, ReactionResult } from "./bridge-client";
import type { HostClient } from "./host-client";
import { fitSetReaction, reactionSignature, rowHandles, toReactionData } from "./reaction";
import { experimentLink } from "./safe-values";

/** What the capture needs of the page bridge. */
export interface ReactionBridge {
  reaction(timeoutMs: number): Promise<ReactionResult>;
  molfile(domId: string, timeoutMs: number): Promise<MolfileResult>;
}

/** What the panel knows right now. */
export interface CaptureContext {
  /** The experiment shown, or null. */
  experiment: ExperimentContext | null;
  /** True when a product of that experiment has a batch registered in it (from the lookups). */
  registered: boolean;
}

export interface ReactionCaptureOptions {
  bridge: ReactionBridge;
  client: HostClient;
  /** Called when `status()` changes. */
  onChange?: () => void;
  /** Watches the page; calls `onChange` after mutations; returns a stop function. Default: a MutationObserver on `doc`. */
  observe?: ((onChange: () => void) => () => void) | null;
  doc?: Document | null;
  /** Timer; returns a cancel function. */
  schedule?: (fn: () => void, ms: number) => () => void;
  /** Debounce of mirror checks after page changes. */
  debounceMs?: number;
  reactionTimeoutMs?: number;
  molfileTimeoutMs?: number;
  /** Clock for the status line (tests). */
  now?: () => Date;
}

export const REACTION_DEBOUNCE_MS = 1000;
/** Passes over the page within one capture when it changes while its structures are read. */
const MAX_PASSES = 3;
/** Mirror sends of one content whose structures could not all be read. */
export const MAX_STRUCTURE_RETRIES = 3;
/** Wait before the mirror reads such structures again. */
export const STRUCTURE_RETRY_MS = 30_000;
/** Structure read failures that may pass (slow server or page): read again later. */
const PASSING_FAILURES: ReadonlySet<string> = new Set(["timeout", "fetch_failed", "no_answer", "unexpected"]);

/** A content stored while some of its structures could not be read. */
interface Incomplete {
  signature: string;
  /** Sends of this content so far (counted afresh when the experiment is shown again). */
  tries: number;
  /** The structures read, by row handle. */
  read: Map<string, string>;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function defaultSchedule(fn: () => void, ms: number): () => void {
  const timer = setTimeout(fn, ms);
  return () => clearTimeout(timer);
}

function observeDocument(doc: Document): (onChange: () => void) => () => void {
  return (onChange) => {
    const View = doc.defaultView?.MutationObserver ?? MutationObserver;
    const observer = new View(() => onChange());
    observer.observe(doc, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["smiles", "href", "title", "class", "readonly", "value", "selected", "checked", "disabled"],
      characterData: true,
    });
    return () => observer.disconnect();
  };
}

/**
 * What one capture did: "stored" (`set_reaction` accepted; `message` holds the
 * remarks of the status line, if any) or "failed" (`message` says why).
 */
export type ReactionStoreOutcome = { result: "stored"; message: string | null } | { result: "failed"; message: string };

function label(experiment: ExperimentContext): string {
  return experiment.code ?? `experiment ${experiment.ref}`;
}

function clock(date: Date): string {
  const two = (n: number) => String(n).padStart(2, "0");
  return `${two(date.getHours())}:${two(date.getMinutes())}`;
}

type Trigger = "create" | "mirror";

export class ReactionCapture {
  private readonly bridge: ReactionBridge;
  private readonly client: HostClient;
  private readonly onChange: () => void;
  private readonly schedule: (fn: () => void, ms: number) => () => void;
  private readonly debounceMs: number;
  private readonly reactionTimeoutMs: number;
  private readonly molfileTimeoutMs: number;
  private readonly now: () => Date;

  private context: CaptureContext = { experiment: null, registered: false };
  private contextKey = "";
  /** Signature last stored successfully, per experiment key. */
  private readonly sent = new Map<string, string>();
  /** Signature whose `set_reaction` failed last, per experiment key. */
  private readonly failed = new Map<string, string>();
  /** Content stored with structures still to read, per experiment key. */
  private readonly incomplete = new Map<string, Incomplete>();
  private running = false;
  /** An "after create" capture waiting for the running one. */
  private pendingCreate: ExperimentContext | null = null;
  /** A mirror check asked for while a capture ran. */
  private checkAgain = false;
  private cancelCheck: (() => void) | null = null;
  private stopObserving: (() => void) | null = null;
  private statusText: string | null = null;
  /** Why the last read of the page failed. */
  private lastReadError = "";
  private disposed = false;

  constructor(options: ReactionCaptureOptions) {
    this.bridge = options.bridge;
    this.client = options.client;
    this.onChange = options.onChange ?? (() => {});
    this.schedule = options.schedule ?? defaultSchedule;
    this.debounceMs = options.debounceMs ?? REACTION_DEBOUNCE_MS;
    this.reactionTimeoutMs = options.reactionTimeoutMs ?? 5000;
    this.molfileTimeoutMs = options.molfileTimeoutMs ?? BRIDGE_MOLFILE_TIMEOUT_MS + 5000;
    this.now = options.now ?? (() => new Date());
    const observe = options.observe ?? (options.doc ? observeDocument(options.doc) : null);
    if (observe) {
      // Page changes matter only while an experiment with a registered product is shown.
      this.stopObserving = observe(() => {
        if (this.context.experiment && this.context.registered) this.scheduleCheck();
      });
    }
  }

  /** The line for the panel's Advanced section; null when nothing was tried yet. */
  status(): string | null {
    return this.statusText;
  }

  /** True while a capture runs (tests, status). */
  isRunning(): boolean {
    return this.running;
  }

  /** What the panel knows now. Cheap; a change that allows mirroring schedules a check. */
  setContext(context: CaptureContext): void {
    if (this.disposed) return;
    const key = JSON.stringify([context.experiment?.ref ?? null, context.experiment?.code ?? null, context.registered]);
    this.context = { experiment: context.experiment, registered: context.registered };
    if (key === this.contextKey) return;
    this.contextKey = key;
    // Shown again: the structures still to read get their tries back.
    const waiting = context.experiment ? this.incomplete.get(context.experiment.ref) : undefined;
    if (waiting) waiting.tries = 0;
    if (context.experiment && context.registered) this.scheduleCheck();
  }

  /** Trigger (a): an ID was just created for a product of `experiment`. */
  afterCreate(experiment: ExperimentContext): void {
    if (this.disposed) return;
    this.pendingCreate = experiment;
    if (!this.running) void this.next();
  }

  dispose(): void {
    this.disposed = true;
    this.cancelCheck?.();
    this.cancelCheck = null;
    this.stopObserving?.();
    this.stopObserving = null;
    this.pendingCreate = null;
  }

  private scheduleCheck(ms: number = this.debounceMs): void {
    if (this.disposed) return;
    this.cancelCheck?.();
    this.cancelCheck = this.schedule(() => {
      this.cancelCheck = null;
      if (this.running) {
        this.checkAgain = true;
        return;
      }
      void this.next();
    }, ms);
  }

  private setStatus(text: string): void {
    if (text === this.statusText) return;
    this.statusText = text;
    this.onChange();
  }

  /** Runs the waiting "after create" capture, else one mirror check. One at a time. */
  private async next(): Promise<void> {
    if (this.running || this.disposed) return;
    let trigger: Trigger;
    let experiment: ExperimentContext;
    if (this.pendingCreate) {
      trigger = "create";
      experiment = this.pendingCreate;
      this.pendingCreate = null;
    } else {
      const { experiment: shown, registered } = this.context;
      if (!shown || !registered) return;
      trigger = "mirror";
      experiment = shown;
    }
    this.running = true;
    this.checkAgain = false;
    try {
      await this.capture(trigger, experiment);
    } catch (error) {
      this.setStatus(`The reaction of ${label(experiment)} could not be saved: ${String(error)}`);
    } finally {
      this.running = false;
    }
    if (this.disposed) return;
    if (this.pendingCreate) {
      void this.next();
    } else if (this.checkAgain) {
      this.checkAgain = false;
      void this.next();
    }
  }

  private stillShown(experiment: ExperimentContext): boolean {
    return this.context.experiment?.ref === experiment.ref;
  }

  private async readPage(): Promise<PageReaction | null> {
    let answer: ReactionResult;
    try {
      answer = await this.bridge.reaction(this.reactionTimeoutMs);
    } catch (error) {
      answer = { ok: false, code: "unexpected", message: String(error) };
    }
    if (!answer.ok) {
      this.lastReadError = answer.message;
      return null;
    }
    return answer.reaction;
  }


  /**
   * Trigger (a), awaited: one capture for `experiment` right now, exactly as
   * after "Create ID", and what it did. Refused while another capture runs.
   * Used through `storeReactionOnce` (the import), never by the panel.
   */
  async captureNow(experiment: ExperimentContext): Promise<ReactionStoreOutcome> {
    if (this.disposed) return { result: "failed", message: "The page is no longer watched." };
    if (this.running) return { result: "failed", message: "The reaction is being stored already." };
    this.running = true;
    try {
      return (
        (await this.capture("create", experiment)) ?? {
          result: "failed",
          message: `The reaction of ${label(experiment)} was not saved.`,
        }
      );
    } catch (error) {
      const message = `The reaction of ${label(experiment)} could not be saved: ${String(error)}`;
      this.setStatus(message);
      return { result: "failed", message };
    } finally {
      this.running = false;
    }
  }

  /** What was done; null when nothing was tried (a mirror check that had nothing to do, or disposed). */
  private async capture(trigger: Trigger, experiment: ExperimentContext): Promise<ReactionStoreOutcome | null> {
    const key = experiment.ref;
    let page = await this.readPage();
    for (let pass = 0; pass < MAX_PASSES; pass++) {
      if (this.disposed) return null;
      if (page === null) {
        if (trigger === "create") {
          const message = `The reaction of ${label(experiment)} could not be read from the page: ${this.lastReadError}`;
          this.setStatus(message);
          return { result: "failed", message };
        }
        return null;
      }
      if (!this.stillShown(experiment)) {
        return trigger === "create"
          ? { result: "failed", message: `The reaction of ${label(experiment)} was not saved: the experiment is no longer shown.` }
          : null;
      }
      if (trigger === "mirror" && (!page.readOnly || !this.context.registered)) return null;
      const signature = reactionSignature(page);
      if (this.sent.get(key) === signature) return { result: "stored", message: null };
      if (trigger === "mirror" && this.failed.get(key) === signature) return null;
      const earlier = this.incomplete.get(key);
      const known = earlier !== undefined && earlier.signature === signature ? earlier : null;
      if (trigger === "mirror" && known !== null && known.tries >= MAX_STRUCTURE_RETRIES) return null;

      // The structures, one at a time; a failed read gives null. Those read
      // for this same content before are kept.
      const molfiles = new Map<string, string | null>();
      const read = new Map<string, string>();
      let unread = 0;
      let rerendered = false;
      for (const handle of rowHandles(page)) {
        const kept = known?.read.get(handle);
        if (kept !== undefined) {
          molfiles.set(handle, kept);
          read.set(handle, kept);
          continue;
        }
        let answer: MolfileResult;
        try {
          answer = await this.bridge.molfile(handle, this.molfileTimeoutMs);
        } catch (error) {
          answer = { ok: false, code: "unexpected", message: String(error) };
        }
        if (this.disposed) return null;
        molfiles.set(handle, answer.ok ? answer.molfile : null);
        if (answer.ok) read.set(handle, answer.molfile);
        else if (answer.code === "no_widget") rerendered = true;
        else if (PASSING_FAILURES.has(answer.code)) unread += 1;
      }

      // Still the same page? (It may have been edited or re-rendered while the structures were read.)
      const again = await this.readPage();
      if (this.disposed) return null;
      const same =
        again !== null &&
        reactionSignature(again) === signature &&
        (trigger === "create" || again.readOnly) &&
        !rerendered;
      if (!same) {
        page = again;
        continue;
      }
      return await this.send(experiment, signature, toReactionData(page, molfiles), {
        read,
        unread,
        tries: known?.tries ?? 0,
      });
    }
    // The page kept changing: the next page change (or the next Create ID) tries again.
    if (trigger === "create") {
      const message = `The reaction of ${label(experiment)} was not saved: the page kept changing.`;
      this.setStatus(message);
      return { result: "failed", message };
    }
    return null;
  }

  private async send(
    experiment: ExperimentContext,
    signature: string,
    reaction: SetReactionParams["reaction"],
    structures: { read: Map<string, string>; unread: number; tries: number },
  ): Promise<ReactionStoreOutcome | null> {
    const fitted = fitSetReaction<SetReactionParams>({
      experiment_ref: experiment.ref,
      experiment_code: experiment.code !== null ? experiment.code.slice(0, MAX_REF_TEXT) : null,
      experiment_url: experimentLink(experiment.url),
      reaction,
    });
    if (fitted === null) {
      this.failed.set(experiment.ref, signature);
      const message = `The reaction of ${label(experiment)} was not saved: it is too large to send to the registry.`;
      this.setStatus(message);
      return { result: "failed", message };
    }
    let reply: BackgroundReply<"set_reaction">;
    try {
      reply = await this.client.request("set_reaction", fitted.params);
    } catch (error) {
      reply = { ok: false, error: { code: "internal_error", message: String(error) } };
    }
    if (this.disposed) return null;
    if (reply.ok) {
      this.failed.delete(experiment.ref);
      let notes = "";
      if (structures.unread === 0) {
        this.sent.set(experiment.ref, signature);
        this.incomplete.delete(experiment.ref);
      } else {
        // Not counted as stored: the structures not read are read again.
        this.sent.delete(experiment.ref);
        const tries = structures.tries + 1;
        this.incomplete.set(experiment.ref, { signature, tries, read: structures.read });
        const again = tries < MAX_STRUCTURE_RETRIES;
        if (again) this.scheduleCheck(STRUCTURE_RETRY_MS);
        notes += ` ${plural(structures.unread, "structure", "structures")} could not be read from the page; ${
          again ? "trying again shortly" : "tried again when the experiment is shown again or on Create ID"
        }.`;
      }
      if (fitted.dropped > 0) {
        notes += ` ${plural(fitted.dropped, "structure was", "structures were")} left out: the reaction was too large to send.`;
      }
      this.setStatus(
        (reply.result.changed
          ? `Reaction of ${label(experiment)} saved (${clock(this.now())}).`
          : `Reaction of ${label(experiment)} is up to date (${clock(this.now())}).`) + notes,
      );
      return { result: "stored", message: notes.trim() === "" ? null : notes.trim() };
    }
    this.failed.set(experiment.ref, signature);
    const message = `The reaction of ${label(experiment)} could not be saved: ${reply.error.message}`;
    this.setStatus(message);
    return { result: "failed", message };
  }
}

export interface StoreReactionOnceOptions {
  bridge: ReactionBridge;
  client: HostClient;
  experiment: ExperimentContext;
  reactionTimeoutMs?: number;
  molfileTimeoutMs?: number;
}

/**
 * Stores the reaction the page shows for `experiment` ONCE, exactly as the
 * panel does right after "Create ID" (trigger (a): page read, structures read
 * one at a time through `bridge`, page read again, `set_reaction`), and says
 * what happened. A capture of its own: it does not watch the page, schedules
 * no retry and leaves the panel's capture alone. The caller makes sure the
 * page shows `experiment`.
 */
export async function storeReactionOnce(options: StoreReactionOnceOptions): Promise<ReactionStoreOutcome> {
  const capture = new ReactionCapture({
    bridge: options.bridge,
    client: options.client,
    observe: null,
    doc: null,
    // No later mirror check or retry: this capture ends with the call.
    schedule: () => () => {},
    ...(options.reactionTimeoutMs !== undefined ? { reactionTimeoutMs: options.reactionTimeoutMs } : {}),
    ...(options.molfileTimeoutMs !== undefined ? { molfileTimeoutMs: options.molfileTimeoutMs } : {}),
  });
  try {
    // `registered: false`: no mirror check is ever scheduled by this context.
    capture.setContext({ experiment: options.experiment, registered: false });
    return await capture.captureNow(options.experiment);
  } finally {
    capture.dispose();
  }
}
