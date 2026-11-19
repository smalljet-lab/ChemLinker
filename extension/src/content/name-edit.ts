/**
 * Editing the stored name (meant for the IUPAC name) of one CORE_STEREO.
 * Shared by the panel's cards and the compounds page. No DOM.
 *
 * The user pastes a name and `set_name` stores it, or, when the host found a
 * naming program on this PC (ChemDraw, see `ping.name_generator`),
 * `generate_name` asks the host to get the name from it and store it. One edit
 * at a time per editor; at most one `set_name` / `generate_name` in flight.
 * The editor never touches an analysis: the caller decides what to do with the
 * stored name it gets back.
 */

import {
  MAX_NAME_TEXT,
  type BackgroundReply,
  type GenerateNameResult,
  type HostError,
  type NameGeneratorInfo,
  type SetNameResult,
} from "../shared/protocol";
import type { HostClient } from "./host-client";

export type NameCheck = { ok: true; name: string } | { ok: false; message: string };

/** The host's rule: one line, no control characters, not blank, at most MAX_NAME_TEXT characters. */
export function checkName(text: string): NameCheck {
  const name = text.trim();
  if (name === "") return { ok: false, message: "Enter a name first." };
  if (/[\u0000-\u001f\u007f]/.test(name)) {
    return { ok: false, message: "The name must be one line of text, without tabs or control characters." };
  }
  if (name.length > MAX_NAME_TEXT) {
    return { ok: false, message: `The name is too long: at most ${MAX_NAME_TEXT} characters.` };
  }
  return { ok: true, name };
}

/** What to call the naming program: the label from `ping`, else "ChemDraw". */
export function generatorLabel(generator: NameGeneratorInfo | null | undefined): string {
  const label = generator?.label;
  return typeof label === "string" && label.trim() !== "" ? label.trim() : "ChemDraw";
}

/** `data-key`s of the name row's controls, unique per CORE_STEREO. */
export function nameKeys(target: string) {
  return {
    edit: `name-edit:${target}`,
    input: `name-input:${target}`,
    save: `name-save:${target}`,
    cancel: `name-cancel:${target}`,
    remove: `name-remove:${target}`,
    copy: `name-copy:${target}`,
    generate: `name-generate:${target}`,
  };
}

export interface NameEditState {
  /** The CORE_STEREO whose name is being edited, or null. */
  editing: string | null;
  draft: string;
  /** `generate`: waiting for the naming program (`generate_name`); else a `set_name`. */
  pending: { target: string; remove: boolean; generate: boolean } | null;
  /** Why the last save / removal / generation of `target` did not go through. */
  problem: { target: string; error: HostError | null; message: string | null; op?: "set_name" | "generate_name" } | null;
  /** Registry warnings of the last successful save or generation. */
  warnings: { target: string; items: string[] } | null;
  /** Why the last `generate_name` of `target` gave no name (the host's plain words). Not an error. */
  notice: { target: string; message: string } | null;
}

export interface NameEditHooks {
  onChange(): void;
  announce?(message: string): void;
}

export interface GenerateOptions {
  /** What to call the naming program in messages (see `generatorLabel`). */
  label?: string;
  /**
   * Started by the extension itself (after "Create ID"), not by a click: it
   * never moves the focus, is not sent while the user edits that name, and
   * says nothing in the status line but a generated name (an error or "no
   * name" shows only in the name row).
   */
  auto?: boolean;
}

const INITIAL: NameEditState = { editing: null, draft: "", pending: null, problem: null, warnings: null, notice: null };

export class NameEditor {
  private s: NameEditState = INITIAL;
  private focusKey: string | null = null;

  constructor(
    private readonly client: HostClient,
    private readonly hooks: NameEditHooks,
  ) {}

  get state(): Readonly<NameEditState> {
    return this.s;
  }

  private update(patch: Partial<NameEditState>, focusKey: string | null = null): void {
    this.s = { ...this.s, ...patch };
    if (focusKey !== null) this.focusKey = focusKey;
    this.hooks.onChange();
  }

  /** The control to focus after the next render (consumed). */
  takeFocusKey(): string | null {
    const key = this.focusKey;
    this.focusKey = null;
    return key;
  }

  /** "Edit" / "Add name": opens the one-line input with the current name. */
  begin(target: string, current: string | null): void {
    if (this.s.pending) return;
    this.update({ editing: target, draft: current ?? "", problem: null, warnings: null, notice: null }, nameKeys(target).input);
  }

  /** Typing: kept without a re-render (the input keeps its caret). */
  setDraft(text: string): void {
    this.s = { ...this.s, draft: text };
  }

  cancel(): void {
    const target = this.s.editing;
    if (this.s.pending || target === null) return;
    this.update({ editing: null, draft: "", problem: null, notice: null }, nameKeys(target).edit);
  }

  /** Stores the draft as the name of `target`. Null when nothing was stored. */
  async save(target: string): Promise<SetNameResult | null> {
    if (this.s.pending || this.s.editing !== target) return null;
    const check = checkName(this.s.draft);
    if (!check.ok) {
      this.update({ problem: { target, error: null, message: check.message }, notice: null }, nameKeys(target).input);
      this.hooks.announce?.(check.message);
      return null;
    }
    return this.send(target, check.name);
  }

  /** Removes the stored name of `target`. */
  async remove(target: string): Promise<SetNameResult | null> {
    if (this.s.pending || this.s.editing !== target) return null;
    return this.send(target, null);
  }

  private async send(target: string, name: string | null): Promise<SetNameResult | null> {
    this.update({ pending: { target, remove: name === null, generate: false }, problem: null, warnings: null, notice: null });
    this.hooks.announce?.(name === null ? "Removing the name…" : "Saving the name…");
    let reply: BackgroundReply<"set_name">;
    try {
      reply = await this.client.request("set_name", { core_stereo_id: target, name });
    } catch (error) {
      reply = { ok: false, error: { code: "internal_error", message: String(error) } };
    }
    if (!reply.ok) {
      this.update(
        { pending: null, problem: { target, error: reply.error, message: null, op: "set_name" } },
        nameKeys(target).input,
      );
      this.hooks.announce?.(`The name was not stored: ${reply.error.message}`);
      return null;
    }
    const result = reply.result;
    this.update(
      {
        editing: null,
        draft: "",
        pending: null,
        problem: null,
        warnings: result.warnings.length > 0 ? { target, items: result.warnings } : null,
      },
      nameKeys(target).edit,
    );
    this.hooks.announce?.(
      result.name === null
        ? `Name of ${result.core_stereo_id} removed.`
        : result.changed
          ? `Name of ${result.core_stereo_id} saved.`
          : `This name was already stored for ${result.core_stereo_id}.`,
    );
    return result;
  }

  /**
   * Asks the host to get the name of `target` from the naming program on this
   * PC and store it (`generate_name`). `replace` false: a name already stored
   * is kept. Returns the host's answer (also when no name was generated: its
   * `name` is what is stored now), or null when nothing was sent or the host
   * answered with an error.
   */
  async generate(target: string, replace: boolean, options: GenerateOptions = {}): Promise<GenerateNameResult | null> {
    const auto = options.auto === true;
    if (this.s.pending) return null;
    if (auto && this.s.editing === target) return null; // the user is typing this name
    const label = options.label ?? "ChemDraw";
    const keys = nameKeys(target);
    const say = (message: string): void => {
      if (!auto) this.hooks.announce?.(message);
    };
    this.update({ pending: { target, remove: false, generate: true }, problem: null, warnings: null, notice: null });
    say(`Asking ${label} for the name of ${target}…`);
    let reply: BackgroundReply<"generate_name">;
    try {
      reply = await this.client.request("generate_name", { core_stereo_id: target, replace });
    } catch (error) {
      reply = { ok: false, error: { code: "internal_error", message: String(error) } };
    }
    if (!reply.ok) {
      this.update(
        { pending: null, problem: { target, error: reply.error, message: null, op: "generate_name" } },
        auto ? null : keys.generate,
      );
      say(`No name from ${label}: ${reply.error.message}`);
      return null;
    }
    const result = reply.result;
    const warnings = result.warnings.length > 0 ? { target, items: result.warnings } : null;
    if (result.generated) {
      const closes = this.s.editing === target;
      this.update(
        {
          editing: closes ? null : this.s.editing,
          draft: closes ? "" : this.s.draft,
          pending: null,
          problem: null,
          warnings,
          notice: null,
        },
        auto ? null : keys.edit,
      );
      this.hooks.announce?.(`Name of ${target} generated by ${label}.`);
    } else {
      const message = result.message?.trim() || `${label} gave no name for ${target}.`;
      this.update({ pending: null, problem: null, warnings, notice: { target, message } }, auto ? null : keys.generate);
      say(message);
    }
    return result;
  }
}
