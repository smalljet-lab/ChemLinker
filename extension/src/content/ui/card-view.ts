/**
 * Renders one product card from its `CardController`. Re-rendered as a whole
 * on every state change; the panel restores focus by `data-key`.
 */

import type { AnalysisStatus, AnalyzeResult, Batch, NameGeneratorInfo, RegisterResult, StereoVariant } from "../../shared/protocol";
import { cardName, cardYield, yieldNote, type CardController, type CardView, type RegisterPlan } from "../card-state";
import { shortDate, STATUS_LABELS, stereoModeLabel } from "../labels";
import { capEntries, corePart, otherExperiments } from "../experiments";
import { shortYield, yieldLine } from "../yields";
import type { NameEditor } from "../name-edit";
import { factsFromStructure } from "../properties";
import { svgDataUrl } from "../safe-values";
import { retireHint } from "../errors";
import { experimentAnchor, renderErrorBox } from "./common";
import { copyButton, renderDetailsList, renderNameRow } from "./details";
import { readRegisterFolder } from "../validate";
import { makeEl, type Child, type El } from "./dom";

export interface CardRenderContext {
  doc: Document;
  copyText: (text: string) => Promise<void>;
  announce: (message: string) => void;
}

function warningsBox(el: El, title: string, items: string[]): HTMLElement | null {
  if (items.length === 0) return null;
  return el(
    "div",
    { class: "banner warn", attrs: { role: "note" } },
    el("strong", { text: title }),
    el("ul", null, ...items.map((w) => el("li", { text: w }))),
  );
}

function idWithCopy(el: El, ctx: CardRenderContext, id: string, key: string): HTMLElement {
  const feedback = el("span", { class: "small", attrs: { "aria-live": "polite" } });
  const button = el("button", {
    class: "btn",
    key,
    attrs: { type: "button", "aria-label": `Copy ${id}` },
    text: "Copy",
  });
  button.addEventListener("click", () => {
    ctx.copyText(id).then(
      () => {
        feedback.textContent = "Copied.";
        ctx.announce(`${id} copied.`);
      },
      () => {
        feedback.textContent = "Copying failed: select the ID and press Ctrl+C.";
      },
    );
  });
  return el("div", { class: "id-box" }, el("span", { class: "big-id mono", text: id }), button, feedback);
}

/**
 * "Folder: <path> [Copy]" after an ID was created: the compound's folder on the
 * registry's PC (as text, never a link). Null when the host made none (its
 * warnings say why) or is older.
 */
function folderLine(el: El, ctx: CardRenderContext, result: RegisterResult): HTMLElement | null {
  const folder = readRegisterFolder(result);
  if (folder === null) return null;
  return el(
    "p",
    { class: "small folder-line" },
    "Folder: ",
    el("span", { class: "mono folder-path", text: folder.path }),
    " ",
    copyButton(el, ctx, folder.path, `the folder of ${result.id}`, "copy-folder"),
  );
}

function experimentLabel(el: El, batch: Batch): Child {
  return experimentAnchor(el, batch.experiment_code || batch.experiment_ref, batch.experiment_url);
}

/** "Also made in: ABC-003, ABC-005 +2 more" (other experiments, active batches only). */
function alsoMadeIn(el: El, result: AnalyzeResult, currentRef: string | null): HTMLElement | null {
  const entries = otherExperiments(result, currentRef);
  if (entries.length === 0) return null;
  const { shown, more } = capEntries(entries);
  const children: Child[] = ["Also made in: "];
  shown.forEach((entry, i) => {
    if (i > 0) children.push(", ");
    children.push(experimentAnchor(el, entry.text, entry.url));
    const y = shortYield(entry.yield);
    if (y) children.push(` (${y})`);
  });
  if (more > 0) children.push(` +${more} more`);
  return el("p", { class: "also small" }, ...children);
}

function batchItem(el: El, batch: Batch): HTMLElement {
  return el(
    "li",
    null,
    el("span", { class: "mono", text: batch.full_id }),
    " · ",
    experimentLabel(el, batch),
    ` · product ${batch.product_index} · ${shortDate(batch.created_at)}`,
    batch.retired
      ? el("span", { class: "retired", text: ` · retired${batch.retired_reason ? `: ${batch.retired_reason}` : ""}` })
      : null,
  );
}

function thumbnail(el: El, svg: unknown, alt: string, small = false): HTMLElement | null {
  const src = svgDataUrl(svg);
  if (src === null) return null;
  return el("img", { class: small ? "thumb small" : "thumb", attrs: { src, alt } });
}

function variantItem(el: El, variant: StereoVariant): HTMLElement {
  const count = variant.batch_count === 1 ? "1 batch" : `${variant.batch_count} batches`;
  return el(
    "li",
    { class: "variant" },
    thumbnail(el, variant.svg, `Structure of ${variant.core_stereo_id}`, true),
    el(
      "span",
      null,
      el("span", { class: "mono", text: variant.core_stereo_id }),
      ` · ${stereoModeLabel(variant.stereo_mode)} · ${count}`,
    ),
  );
}

function modeSelect(el: El, controller: CardController, view: CardView): HTMLElement | null {
  const control = view.modeControl;
  if (!control) return null;
  const id = `crm-mode-${Math.random().toString(36).slice(2)}`;
  const select = el("select", { key: "mode", attrs: { id } });
  for (const option of control.options) {
    const node = el("option", { attrs: { value: option.value }, text: option.label });
    node.disabled = option.disabled;
    select.appendChild(node);
  }
  select.value = control.value;
  select.disabled = control.disabled;
  select.addEventListener("change", () => {
    void controller.selectMode(select.value);
  });
  return el("div", null, el("label", { class: "field", attrs: { for: id }, text: "Stereo mode" }), select);
}


/** One short line saying what "Create ID" will create, plus the stereo mode in words. */
export function createSummary(result: AnalyzeResult, status: AnalysisStatus): string {
  let what: string;
  if (status === "known_core") {
    const other = result.other_stereos[0];
    what = other ? `New stereo variant of ${corePart(other.core_stereo_id)}` : "New stereo variant";
  } else if (status === "known_compound") {
    what = result.core_stereo_id ? `New batch of ${result.core_stereo_id}` : "New batch";
  } else {
    what = "New compound";
  }
  const mode = result.structure.stereo_mode;
  return mode === "none" ? what : `${what} · ${stereoModeLabel(mode)}`;
}

function ignoredBlock(el: El, controller: CardController, view: CardView): HTMLElement | null {
  if (view.ignored.length === 0) return null;
  return el(
    "div",
    { class: "ignored banner warn" },
    el("strong", { text: "Stereo the ID will not distinguish" }),
    el("p", {
      text:
        "The drawing has stereo features that are NOT part of the registry keys. Stereoisomers that differ " +
        "only in these features would share the same ID. Tick each item to accept it before creating an ID.",
    }),
    ...view.ignored.map((item, index) => {
      const box = el("input", { key: `ignored-${index}`, attrs: { type: "checkbox" } });
      box.checked = item.ticked;
      box.disabled = view.busy !== null || view.stale;
      box.addEventListener("change", () => controller.setIgnoredTicked(index, box.checked));
      return el("label", { class: "check" }, box, el("span", { text: item.text }));
    }),
  );
}

function confirmation(el: El, controller: CardController, plan: RegisterPlan): HTMLElement {
  const what =
    plan.status === "new"
      ? "a new compound (new core number)"
      : plan.status === "known_core"
        ? "a new stereo variant of an already registered compound"
        : `a new batch of ${plan.coreStereoId ?? "the registered stereo variant"}`;
  const create = el("button", { class: "btn primary", key: "confirm-create", attrs: { type: "button" }, text: "Create" });
  create.addEventListener("click", () => {
    void controller.confirmCreate();
  });
  const cancel = el("button", { class: "btn", key: "confirm-cancel", attrs: { type: "button" }, text: "Cancel" });
  cancel.addEventListener("click", () => controller.cancelCreate());
  return el(
    "div",
    { class: "confirm", attrs: { role: "group", "aria-label": "Confirm creating an ID" } },
    el("strong", { text: `Create an ID for ${what}?` }),
    el(
      "dl",
      { class: "facts" },
      el("dt", { text: "Stereo mode" }),
      el("dd", { text: plan.stereoModeLabel }),
      el("dt", { text: "Experiment key" }),
      el("dd", { class: "mono", text: plan.experimentRef }),
      el("dt", { text: "Experiment code" }),
      el("dd", { text: plan.experimentCode ?? "(none)" }),
      el("dt", { text: "Product no." }),
      el("dd", { text: String(plan.productIndex) }),
    ),
    plan.acceptedIgnored.length > 0
      ? el("p", { text: `Accepted stereo the ID does not distinguish: ${plan.acceptedIgnored.join("; ")}` })
      : null,
    el("p", { text: "An issued ID is permanent: it can later be retired, but never deleted or reused." }),
    el("div", { class: "row" }, create, cancel),
  );
}

function slotConflictBlock(el: El, result: AnalyzeResult): HTMLElement | null {
  const conflict = result.slot_conflict;
  if (!conflict) return null;
  return el(
    "div",
    { class: "banner error" },
    el("strong", { text: "Product slot already taken" }),
    el(
      "p",
      null,
      `Product no. ${conflict.product_index} of this experiment already holds `,
      el("span", { class: "mono", text: conflict.full_id }),
      ". That ID must be retired before another compound can take this slot.",
    ),
    el("p", { class: "small", text: retireHint(conflict.full_id) }),
  );
}

/** Details shown only with "Advanced" open. */
function advancedBlock(el: El, controller: CardController, view: CardView, lookUp: HTMLElement): HTMLElement {
  const state = controller.state;
  const analysis = state.analysis;
  const r = analysis?.result ?? null;
  const backup = state.registration?.result.backup ?? null;
  return el(
    "div",
    { class: "advanced-card" },
    modeSelect(el, controller, view),
    r && view.status ? el("p", { class: "small", text: STATUS_LABELS[view.status] }) : null,
    r?.core_stereo_id
      ? el("p", { class: "small" }, "Stereo variant: ", el("span", { class: "mono", text: r.core_stereo_id }))
      : null,
    r && r.batches.length > 0
      ? el(
          "div",
          null,
          el("h4", { text: "Batches of this compound" }),
          el("ul", { class: "list" }, ...r.batches.map((b) => batchItem(el, b))),
        )
      : null,
    r && r.other_stereos.length > 0
      ? el(
          "div",
          null,
          el("h4", { text: "Other stereo variants of this constitution" }),
          el("ul", { class: "list" }, ...r.other_stereos.map((v) => variantItem(el, v))),
        )
      : null,
    backup?.performed && backup.path
      ? el("p", { class: "small" }, "Backup written: ", el("span", { class: "mono", text: backup.path }))
      : null,
    el("div", { class: "row" }, lookUp),
  );
}

/** The card's own "Details" toggle and name editor; kept by the panel so they survive re-renders. */
export interface CardDetailsUi {
  open: boolean;
  toggle(): void;
  names: NameEditor;
  saveName(target: string): void;
  removeName(target: string): void;
  /** The naming program on this PC as far as the last ping knows (absent: not available). */
  generator?: NameGeneratorInfo | null;
  /** "Get name from ChemDraw" / "Get from ChemDraw". */
  generateName?: ((target: string, replace: boolean) => void) | null;
}

export interface CardRenderOptions {
  /** Show the details behind "Advanced" (the details list then shows on every card, without its toggle). */
  advanced: boolean;
  details?: CardDetailsUi | null;
}

let detailsCounter = 0;

/**
 * The card's "Details": a toggle (only while Advanced is closed) and the list
 * of names, formula, masses and identifiers. Shown once: with Advanced open
 * the list is always there and the toggle is not.
 */
function detailsSection(
  el: El,
  controller: CardController,
  view: CardView,
  label: string,
  ctx: CardRenderContext,
  options: CardRenderOptions,
): Child[] {
  const r = controller.state.analysis?.result;
  if (!r) return [];
  const ui = options.details ?? null;
  const shown = options.advanced || ui?.open === true;
  const id = `crm-details-${++detailsCounter}`;
  let toggle: HTMLElement | null = null;
  if (!options.advanced && ui) {
    toggle = el("button", {
      class: "disclosure details-toggle",
      key: "details",
      attrs: { type: "button", "aria-expanded": String(ui.open), "aria-controls": id },
      text: "Details",
    });
    toggle.addEventListener("click", () => ui.toggle());
  }
  if (!shown) return [toggle];
  const name = ui ? cardName(controller.state) : null;
  const nameRow =
    ui && name
      ? renderNameRow(el, ctx, ui.names, {
          target: name.target,
          name: name.name,
          disabledReason: view.stale ? "Look up again before changing the name." : null,
          save: (target) => ui.saveName(target),
          remove: (target) => ui.removeName(target),
          generator: ui.generator ?? null,
          generate: ui.generateName ?? null,
        })
      : null;
  return [
    toggle,
    el(
      "div",
      { class: "details-block", attrs: { id } },
      renderDetailsList(el, ctx, factsFromStructure(r.structure), { nameRow, keyPrefix: "", subject: label }),
    ),
  ];
}

export function renderCard(
  container: HTMLElement,
  controller: CardController,
  label: string,
  ctx: CardRenderContext,
  options: CardRenderOptions = { advanced: true },
): void {
  const el = makeEl(ctx.doc);
  const view = controller.view();
  const state = controller.state;
  const input = state.input;
  const analysis = state.analysis;
  const r = analysis?.result ?? null;
  const registration = state.registration;
  const registrationShown = registration !== null && analysis !== null && registration.analysisSeq === analysis.seq;

  const lookUp = el("button", { class: "btn", key: "lookup", attrs: { type: "button" }, text: view.lookUpLabel });
  lookUp.disabled = !view.canLookUp;
  lookUp.addEventListener("click", () => {
    void controller.lookUp();
  });

  // The one main line: the ID, or the Create button with what it will create.
  let main: Child = null;
  if (r && view.status === "registered_here" && view.existingId) {
    const yieldText = yieldLine(cardYield(state));
    main = el(
      "div",
      null,
      idWithCopy(el, ctx, view.existingId, "copy-id"),
      yieldText ? el("p", { class: "small yield-line", text: yieldText }) : null,
      registrationShown
        ? el("p", {
            class: "small",
            text: registration!.result.created
              ? "ID created now."
              : "This ID already existed for this compound in this experiment.",
          })
        : null,
      registrationShown ? folderLine(el, ctx, registration!.result) : null,
    );
  } else if (r && view.status && view.createLabel) {
    if (view.confirming) {
      main = confirmation(el, controller, view.confirming);
    } else {
      const create = el("button", { class: "btn primary", key: "create", attrs: { type: "button" }, text: view.createLabel });
      create.disabled = !view.canCreate;
      create.addEventListener("click", () => {
        void controller.create();
      });
      main = el(
        "div",
        { class: "create" },
        el("div", { class: "row" }, create),
        el("p", { class: "summary", text: createSummary(r, view.status) }),
        view.canCreate || view.busy
          ? null
          : el("ul", { class: "blockers" }, ...view.createBlockers.map((b) => el("li", { text: b }))),
      );
    }
  }

  const busyText =
    view.busy === "analyze"
      ? "Looking up…"
      : view.busy === "recheck"
        ? "Checking the structure on the page…"
        : view.busy === "register"
          ? "Creating the ID…"
          : null;
  const notice = state.recheckNotice;
  const yieldProblem = yieldNote(state);
  const showSmallIndex = input !== null && label !== `Product ${input.product.productIndex}`;
  // Without Advanced, "Look up" is offered only where the user has to act.
  const lookUpNeeded = !options.advanced && view.canLookUp && (state.error !== null || view.stale || !analysis);

  const children: Child[] = [
    el(
      "div",
      { class: "card-head" },
      r ? thumbnail(el, r.structure.svg, `Structure of ${label}`, true) : null,
      el(
        "div",
        { class: "card-title" },
        el("h3", { text: label }),
        showSmallIndex ? el("span", { class: "small", text: `Product no. ${input!.product.productIndex}` }) : null,
        busyText ? el("p", { class: "small busy", text: busyText }) : null,
      ),
    ),
    state.error ? renderErrorBox(el, state.error.error, state.error.op) : null,
    notice?.kind === "changed"
      ? el("div", { class: "banner warn", attrs: { role: "alert" }, text: notice.message })
      : null,
    notice?.kind === "unavailable"
      ? el(
          "div",
          { class: "banner error", attrs: { role: "alert" } },
          el("strong", { text: notice.heading }),
          el("p", { text: notice.message }),
        )
      : null,
    view.stale && view.staleReason
      ? el("p", { class: "stale-note", text: `Out of date: ${view.staleReason}` })
      : null,
    lookUpNeeded ? el("div", { class: "row" }, lookUp) : null,
    r
      ? el(
          "div",
          { class: view.stale ? "analysis stale" : "analysis", attrs: { "aria-disabled": view.stale ? "true" : "false" } },
          warningsBox(el, "Structure warnings", r.structure.warnings),
          warningsBox(el, "Registry warnings", r.warnings),
          slotConflictBlock(el, r),
          main,
          yieldProblem ? el("p", { class: "small yield-note", attrs: { role: "note" }, text: yieldProblem }) : null,
          alsoMadeIn(el, r, input?.experiment?.ref ?? analysis!.snapshot.experimentRef),
          ...detailsSection(el, controller, view, label, ctx, options),
        )
      : null,
    ignoredBlock(el, controller, view),
    registration && registration.result.warnings.length > 0
      ? warningsBox(el, `Warnings for ${registration.result.id}`, registration.result.warnings)
      : null,
    registration && !registrationShown
      ? el(
          "div",
          { class: "section" },
          el("p", { class: "small", text: registration.result.created ? "ID created:" : "ID (already existed):" }),
          idWithCopy(el, ctx, registration.result.id, "copy-registered"),
          folderLine(el, ctx, registration.result),
        )
      : null,
    options.advanced ? advancedBlock(el, controller, view, lookUpNeeded ? el("span") : lookUp) : null,
  ];
  container.replaceChildren();
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    container.appendChild(typeof child === "string" || typeof child === "number" ? ctx.doc.createTextNode(String(child)) : child);
  }
}

