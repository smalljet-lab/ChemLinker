/**
 * The "Details" of a compound and its name row, shared by the panel's cards
 * and the compounds page. Text only via text nodes.
 */

import { MAX_NAME_TEXT, type NameGeneratorInfo, type NameInfo } from "../../shared/protocol";
import { shortDate } from "../labels";
import { generatorLabel, nameKeys, type NameEditor } from "../name-edit";
import { detailsLines, type DetailsFacts } from "../properties";
import { nameSource } from "../validate";
import { renderErrorBox } from "./common";
import type { El } from "./dom";

export interface CopyContext {
  copyText: (text: string) => Promise<void>;
  announce: (message: string) => void;
}

/** A "Copy" button with its own feedback line. `what` names the value for screen readers. */
export function copyButton(el: El, ctx: CopyContext, text: string, what: string, key: string): HTMLElement {
  const feedback = el("span", { class: "small copy-feedback", attrs: { "aria-live": "polite" } });
  const button = el("button", {
    class: "btn small-btn",
    key,
    attrs: { type: "button", "aria-label": `Copy ${what}` },
    text: "Copy",
  });
  button.addEventListener("click", () => {
    ctx.copyText(text).then(
      () => {
        feedback.textContent = "Copied.";
        ctx.announce(`${what} copied.`);
      },
      () => {
        feedback.textContent = "Copying failed: select the text and press Ctrl+C.";
      },
    );
  });
  return el("span", { class: "copy" }, button, feedback);
}

export interface NameRowOptions {
  /** The CORE_STEREO named; null while the compound has no ID. */
  target: string | null;
  name: NameInfo | null;
  /** Why editing is not possible now (e.g. an out-of-date lookup), or null. */
  disabledReason?: string | null;
  /** Called by Save / Remove; the caller sends through `editor` and applies the result. */
  save: (target: string) => void;
  remove: (target: string) => void;
  /**
   * The naming program on this PC, as reported by `ping` (absent or null: not
   * known, or an older host). Its label names generated names in any case; its
   * buttons are offered only when it is available and `generate` is given.
   */
  generator?: NameGeneratorInfo | null;
  /** Called by "Get name from ChemDraw" (`replace` false) and "Get from ChemDraw" (`replace` true). */
  generate?: ((target: string, replace: boolean) => void) | null;
}

/**
 * The name: the stored text with "Edit" (or "No name stored" with "Add name"),
 * or the one-line editor; with a naming program on this PC, also the buttons
 * that get the name from it.
 */
export function renderNameRow(el: El, ctx: CopyContext, editor: NameEditor, options: NameRowOptions): HTMLElement {
  const { target, name } = options;
  if (target === null) {
    return el("div", { class: "name-row" }, el("span", { class: "small", text: "No ID yet: a name can be stored once the compound has an ID." }));
  }
  const keys = nameKeys(target);
  const s = editor.state;
  const pending = s.pending !== null;
  const problem = s.problem?.target === target ? s.problem : null;
  const warnings = s.warnings?.target === target ? s.warnings.items : [];
  const notice = s.notice?.target === target ? s.notice.message : null;
  const disabledReason = options.disabledReason ?? null;
  const label = generatorLabel(options.generator);
  const generate = options.generator?.available === true && options.generate ? options.generate : null;
  const generating = s.pending?.generate === true && s.pending.target === target;
  const askingLine = generating
    ? el("p", { class: "small busy", attrs: { "aria-live": "polite" }, text: `Asking ${label}…` })
    : null;
  const noticeLine = notice ? el("p", { class: "small name-notice", attrs: { role: "status" }, text: notice }) : null;
  const errorBox = problem?.error ? renderErrorBox(el, problem.error, problem.op ?? "set_name") : null;

  if (s.editing === target) {
    const inputId = `crm-${keys.input.replace(/[^A-Za-z0-9_-]/g, "-")}`;
    const hintId = `${inputId}-hint`;
    const input = el("input", {
      key: keys.input,
      attrs: {
        id: inputId,
        type: "text",
        maxlength: String(MAX_NAME_TEXT),
        autocomplete: "off",
        spellcheck: "false",
        "aria-describedby": hintId,
      },
    });
    input.value = s.draft;
    input.disabled = pending;
    input.addEventListener("input", () => editor.setDraft(input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        editor.setDraft(input.value);
        if (!pending && disabledReason === null) options.save(target);
      } else if (event.key === "Escape") {
        // Cancels the edit only (in the panel, Escape would otherwise close the panel).
        event.preventDefault();
        event.stopPropagation();
        editor.cancel();
      }
    });
    const save = el("button", { class: "btn primary small-btn", key: keys.save, attrs: { type: "button" }, text: "Save" });
    save.disabled = pending || disabledReason !== null;
    save.addEventListener("click", () => {
      editor.setDraft(input.value);
      options.save(target);
    });
    const cancel = el("button", { class: "btn small-btn", key: keys.cancel, attrs: { type: "button" }, text: "Cancel" });
    cancel.disabled = pending;
    cancel.addEventListener("click", () => editor.cancel());
    let remove: HTMLElement | null = null;
    if (name !== null) {
      const button = el("button", {
        class: "btn small-btn",
        key: keys.remove,
        attrs: { type: "button", "aria-label": `Remove the stored name of ${target}` },
        text: "Remove",
      });
      button.disabled = pending || disabledReason !== null;
      button.addEventListener("click", () => options.remove(target));
      remove = button;
    }
    let replace: HTMLElement | null = null;
    if (name !== null && generate) {
      const button = el("button", {
        class: "btn small-btn",
        key: keys.generate,
        attrs: { type: "button", "aria-label": `Replace the name of ${target} with the one from ${label}` },
        text: `Get from ${label}`,
      });
      button.disabled = pending || disabledReason !== null;
      button.addEventListener("click", () => generate(target, true));
      replace = button;
    }
    return el(
      "div",
      { class: "name-row editing", attrs: { role: "group", "aria-label": `Name of ${target}` } },
      el("label", { class: "field", attrs: { for: inputId }, text: `Name of ${target}` }),
      input,
      el("p", {
        class: "small",
        attrs: { id: hintId },
        text: generate
          ? `One line, e.g. the IUPAC name. It is stored in the local registry only; ${label} runs on this PC and nothing is sent anywhere else.`
          : "One line, e.g. the IUPAC name. It is stored in the local registry only; nothing is generated or sent anywhere else.",
      }),
      el("div", { class: "row" }, save, cancel, remove, replace),
      disabledReason ? el("p", { class: "small", text: disabledReason }) : null,
      pending && !generating ? el("p", { class: "small busy", text: s.pending!.remove ? "Removing…" : "Saving…" }) : null,
      askingLine,
      noticeLine,
      problem?.message ? el("p", { class: "small problem", attrs: { role: "alert" }, text: problem.message }) : null,
      errorBox,
    );
  }

  const edit = el("button", {
    class: "btn small-btn",
    key: keys.edit,
    attrs: { type: "button", "aria-label": name ? `Edit the name of ${target}` : `Add a name for ${target}` },
    text: name ? "Edit" : "Add name",
  });
  edit.disabled = pending || disabledReason !== null;
  edit.addEventListener("click", () => editor.begin(target, name?.text ?? null));
  let ask: HTMLButtonElement | null = null;
  if (name === null && generate) {
    ask = el("button", {
      class: "btn small-btn",
      key: keys.generate,
      attrs: { type: "button", "aria-label": `Get the name of ${target} from ${label}` },
      text: `Get name from ${label}`,
    });
    ask.disabled = pending || disabledReason !== null;
    ask.addEventListener("click", () => generate(target, false));
  }
  return el(
    "div",
    { class: "name-row" },
    name
      ? el("span", { class: "name-text", text: name.text })
      : el("span", { class: "small", text: "No name stored" }),
    el(
      "span",
      { class: "row inline" },
      edit,
      ask,
      name ? copyButton(el, ctx, name.text, `the name of ${target}`, keys.copy) : null,
    ),
    name?.updated_at ? el("span", { class: "small", text: `Stored ${shortDate(name.updated_at)}` }) : null,
    name && nameSource(name) === "chemdraw" ? el("span", { class: "small name-source", text: `Generated by ${label}` }) : null,
    askingLine,
    noticeLine,
    errorBox,
    disabledReason ? el("p", { class: "small", text: disabledReason }) : null,
    warnings.length > 0
      ? el(
          "div",
          { class: "banner warn", attrs: { role: "note" } },
          el("strong", { text: "Registry warnings" }),
          el("ul", null, ...warnings.map((w) => el("li", { text: w }))),
        )
      : null,
  );
}

export interface DetailsListOptions {
  /** The name row (first line), or null when names are not available. */
  nameRow: HTMLElement | null;
  /** Prefix of the copy buttons' `data-key`s (unique per compound on a page). */
  keyPrefix: string;
  /** What the values belong to, for the copy buttons' labels ("Product 1", "000042_02"). */
  subject: string;
}

/** The details list: name, formula, molar mass, exact mass, elemental analysis, SMILES, InChI, InChIKey, stereo. */
export function renderDetailsList(el: El, ctx: CopyContext, facts: DetailsFacts, options: DetailsListOptions): HTMLElement {
  const list = el("dl", { class: "facts details" });
  if (options.nameRow) list.append(el("dt", { text: "IUPAC name" }), el("dd", null, options.nameRow));
  for (const line of detailsLines(facts)) {
    list.append(
      el("dt", { text: line.label }),
      el(
        "dd",
        null,
        el("span", { class: line.mono ? "mono" : "", text: line.value }),
        line.copy ? copyButton(el, ctx, line.value, `${line.label} of ${options.subject}`, `${options.keyPrefix}copy-${line.key}`) : null,
      ),
    );
  }
  return list;
}
