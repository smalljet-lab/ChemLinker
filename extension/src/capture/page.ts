/**
 * The capture page (capture.html): the user drags a box over the frozen
 * screenshot of the tab, the local program reads the molecules and the
 * reaction in it, and the page shows the result as a draft to check and
 * correct: the cut-out picture with numbered boxes next to the redrawn
 * structures, roles and steps that can be changed, the condition text, the
 * reaction SMILES, and "Open in ChemDraw".
 *
 * Rules of this page: nothing leaves the PC (no network call; only messages
 * to the background); the screenshot and everything read from it live in this
 * page's memory only and are never logged; text only ever enters the document
 * as text nodes, drawings only as <img src="data:...">.
 */

import type { EditorFactory, StructureEditor, StructureRenderer } from "../compounds/editor";
import { svgDataUrl } from "../content/safe-values";
import { makeEl, type Child } from "../content/ui/dom";
import {
  MAX_STRUCTURE_CHARS,
  type HostError,
  type RecognitionMode,
  type RecognitionStatusResult,
  type RecognizeResult,
} from "../shared/protocol";
import type { CaptureClient } from "./client";
import {
  CORNERS,
  boxPercent,
  cropProblem,
  cutOutView,
  encodeWithinLimit,
  moveCorner,
  nudgeCorner,
  rectFromPoints,
  toPictureRect,
  wholeRect,
  type Corner,
  type Point,
  type Rect,
  type Size,
} from "./geometry";
import type { Imaging, Picture } from "./imaging";
import {
  ROLES,
  ROLE_LABELS,
  addMolecule,
  addStep,
  applyEdit,
  chemdrawRequest,
  copyAllText,
  findMolecule,
  modelFromResult,
  moleculesOf,
  place,
  reactionSmiles,
  removeMolecule,
  removeStep,
  roleIn,
  unusedMolecules,
  type Model,
  type Molecule,
  type Placement,
  type Role,
} from "./model";
import {
  CONFIDENCE_HELP,
  warningText,
  RESULT_WARNING_TEXT,
  captureFailText,
  confidenceText,
  errorSentence,
  statusText,
} from "./texts";

export interface PageTimers {
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
  now(): number;
}

export interface CapturePageDeps {
  doc: Document;
  root: HTMLElement;
  client: CaptureClient;
  imaging: Imaging;
  createEditor: EditorFactory;
  renderStructure: StructureRenderer;
  /** The `id` of the page's address; null when the page was opened without one. */
  captureId: string | null;
  /**
   * The `failed` of the page's address: why the keyboard shortcut could not
   * take a screenshot, as one of the background's fixed words. Anything else
   * is treated as absent.
   */
  failed?: string | null;
  /** Default: the clipboard of this page. */
  copyText?: (text: string) => Promise<void>;
  timers?: PageTimers;
  /** How often the state of the recognition program is asked while it loads. */
  statusPollMs?: number;
}

export interface CapturePageHandle {
  destroy(): void;
}

/** The selection lives in fractions of the picture (0 to 1), so it does not depend on how large the picture is shown. */
const UNIT: Size = { w: 1, h: 1 };
/** A drag shorter than this (as a fraction of the picture) is a click, not a box. */
const MIN_DRAG = 0.004;
const CORNER_NAMES: Readonly<Record<Corner, string>> = { nw: "Top left", ne: "Top right", sw: "Bottom left", se: "Bottom right" };
/** After this long without an answer the status line says that the program may be busy. */
const STATUS_SLOW_MS = 5000;
const NEW_CAPTURE_HELP =
  "To capture another page: go to the tab with the reaction, click the ChemLinker button in the toolbar and choose Capture reaction.";

type View = "message" | "select" | "working" | "result";
type RoleChoice = Role | "unused";

interface Sent {
  url: string;
  scale: number;
}

/** True for a picture that is shown above the steps (full width) instead of beside them. */
export function isWidePicture(width: number, height: number): boolean {
  return width > 0 && height > 0 && width / height >= 1.8;
}

export function mountCapturePage(deps: CapturePageDeps): CapturePageHandle {
  const { doc, root, client } = deps;
  const el = makeEl(doc);
  const copyText = deps.copyText ?? ((text: string) => navigator.clipboard.writeText(text));
  const timers: PageTimers = deps.timers ?? {
    setInterval: (fn, ms) => setInterval(fn, ms),
    clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
    now: () => Date.now(),
  };
  let destroyed = false;

  // ---------------------------------------------------------------- state
  let view: View = "message";
  let picture: Picture | null = null;
  let selection: Rect | null = null;
  let mode: RecognitionMode = "reaction";
  let status: RecognitionStatusResult | null = null;
  /** Why the state could not be asked; shown instead of the state. */
  let statusError: HostError | null = null;
  let statusBusy = false;
  let statusAskedAt = 0;
  let result: RecognizeResult | null = null;
  let sent: Sent | null = null;
  let model: Model | null = null;
  /** The step an unused molecule goes to when it gets a role. */
  const targetStep = new Map<string, number | "new">();

  // ---------------------------------------------------------------- live region
  const statusLine = el("p", { class: "sr-only", attrs: { role: "status", "aria-live": "polite" } });
  const announce = (message: string): void => {
    statusLine.textContent = message;
  };

  const banner = (kind: "info" | "warn" | "error" | "ok", ...children: Child[]): HTMLElement =>
    el("div", { class: `banner ${kind}`, attrs: kind === "error" ? { role: "alert" } : {} }, ...children);

  const button = (text: string, onClick: () => void, cls = "btn", attrs: Record<string, string> = {}): HTMLButtonElement =>
    el("button", { class: cls, text, attrs: { type: "button", ...attrs }, on: { click: onClick } });

  function copyButton(what: string, text: () => string, key: string): HTMLButtonElement {
    const node = button("Copy", () => {
      copyText(text()).then(
        () => {
          node.textContent = "Copied";
          announce(`${what} copied.`);
        },
        () => {
          node.textContent = "Copy failed";
          announce(`${what} could not be copied.`);
        },
      );
    }, "btn small-btn", { "aria-label": `Copy ${what}` });
    node.dataset.key = key;
    return node;
  }

  // ---------------------------------------------------------------- frame
  const header = el(
    "header",
    { class: "page-head" },
    el("h1", { text: "Capture reaction" }),
    el("p", {
      class: "small",
      text: "Reads the molecules and the reaction from a picture of the tab. Everything runs on this PC: nothing is sent anywhere.",
    }),
  );
  const messageView = el("section", { class: "panel" });
  const selectView = el("section", { class: "panel", attrs: { "aria-label": "Select the reaction" } });
  const workingView = el("section", { class: "panel" });
  const resultView = el("section", { class: "result", attrs: { "aria-label": "Result" } });
  const overlayHost = el("div", null);
  root.replaceChildren(header, el("main", null, messageView, selectView, workingView, resultView), overlayHost, statusLine);

  function show(next: View): void {
    view = next;
    messageView.hidden = next !== "message";
    selectView.hidden = next !== "select";
    workingView.hidden = next !== "working";
    resultView.hidden = next !== "result";
  }

  function showMessage(title: string, ...paragraphs: string[]): void {
    messageView.replaceChildren(
      el("h2", { text: title }),
      ...paragraphs.map((text) => el("p", { text })),
      el("p", { class: "small", text: NEW_CAPTURE_HELP }),
    );
    show("message");
  }

  // ---------------------------------------------------------------- recognition status
  const statusBox = el("div", { class: "status-box" });

  function renderStatus(): void {
    const error = statusError;
    const again = button("Check again", () => void refreshStatus(), "btn small-btn");
    again.dataset.key = "status-again";
    if (error !== null) {
      statusBox.replaceChildren(banner("error", errorSentence(error, "recognition_status"), " ", again));
    } else if (status === null) {
      // The program answers one request at a time, for all Capture reaction tabs together.
      const slow = statusBusy && timers.now() - statusAskedAt >= STATUS_SLOW_MS;
      statusBox.replaceChildren(
        el("p", {
          class: "busy",
          text: slow
            ? "Still waiting for the program on this PC. It does one thing at a time: if another Capture reaction tab is reading a picture, it finishes that first. You can already select the reaction."
            : "Asking the program on this PC…",
        }),
      );
    } else {
      const text = statusText(status);
      const retry = status.state === "not_installed" || status.state === "failed";
      statusBox.replaceChildren(
        banner(
          text.kind,
          el("span", { text: text.text }),
          text.command ? el("code", { class: "command mono", text: text.command }) : null,
          retry ? again : null,
        ),
      );
    }
    updateSelectControls();
  }

  async function refreshStatus(): Promise<void> {
    if (statusBusy || destroyed) return;
    statusBusy = true;
    statusAskedAt = timers.now();
    const reply = await client.request("recognition_status", { start: true });
    statusBusy = false;
    if (destroyed) return;
    status = reply.ok ? reply.result : null;
    statusError = reply.ok ? null : reply.error;
    renderStatus();
  }

  const poll = timers.setInterval(() => {
    if (view !== "select") return;
    // A first question that is still open: say so once it has taken long.
    if (status === null && statusError === null && statusBusy) renderStatus();
    if (status === null) return;
    if (status.state === "loading" || status.state === "stopped") void refreshStatus();
  }, deps.statusPollMs ?? 2000);

  // ---------------------------------------------------------------- selection
  const shot = el("img", { class: "shot", attrs: { alt: "Screenshot of the tab", draggable: "false" } });
  const selBox = el("div", { class: "sel-box" });
  selBox.hidden = true;
  const handles = new Map<Corner, HTMLButtonElement>();
  for (const corner of CORNERS) {
    const handle = el("button", {
      class: `handle handle-${corner}`,
      attrs: { type: "button", "aria-label": `${CORNER_NAMES[corner]} corner of the selection. Arrow keys move it.` },
    });
    handle.dataset.key = `handle-${corner}`;
    handle.addEventListener("keydown", (event) => onHandleKey(corner, event));
    handles.set(corner, handle);
    selBox.append(handle);
  }
  const stage = el("div", { class: "stage" }, shot, selBox);

  const selectionNote = el("p", { class: "small selection-note", attrs: { role: "status" } });
  const selectProblem = el("div", null);
  const recogniseButton = button("Recognise", () => void recognise(), "btn primary");
  const wholeButton = button("Whole picture", () => setSelection(wholeRect(UNIT)));
  const clearButton = button("Clear selection", () => setSelection(null));
  // "New selection" must not cost the corrections already made: the result stays until a new one replaces it.
  const backButton = button("Back to the result", () => showResult());
  backButton.hidden = true;
  const keptNote = el("p", {
    class: "small",
    text: "The result you were checking is kept, with your corrections, until a new recognition has finished. Back to the result shows it again.",
  });
  keptNote.hidden = true;

  const modeName = "capture-mode";
  const modeRadio = (value: RecognitionMode, label: string): HTMLElement => {
    const input = el("input", { attrs: { type: "radio", name: modeName, value } });
    input.checked = mode === value;
    input.addEventListener("change", () => {
      if (input.checked) mode = value;
    });
    return el("label", { class: "check" }, input, el("span", { text: label }));
  };

  selectView.append(
    el("h2", { text: "1. Select the reaction" }),
    el("p", {
      text: "Drag a box around the reaction scheme: molecules, arrows and the text at the arrows. Leave out everything else.",
    }),
    statusBox,
    el(
      "div",
      { class: "toolbar" },
      el("fieldset", { class: "mode" }, el("legend", { text: "What is in the box?" }), modeRadio("reaction", "A reaction scheme"), modeRadio("molecule", "A single molecule")),
      el("div", { class: "row inline" }, wholeButton, clearButton, recogniseButton, backButton),
    ),
    keptNote,
    selectionNote,
    selectProblem,
    el("div", { class: "stage-wrap" }, stage),
    el("p", { class: "small", text: `Esc clears the selection. ${NEW_CAPTURE_HELP}` }),
  );

  function currentCrop(): Rect | null {
    if (picture === null || selection === null) return null;
    return toPictureRect(selection, UNIT, { w: picture.width, h: picture.height });
  }

  function updateSelectControls(): void {
    const crop = currentCrop();
    const problem = selection === null ? null : cropProblem(crop);
    const blocked = status !== null && status.state === "not_installed";
    recogniseButton.disabled = selection === null || problem !== null || blocked;
    clearButton.disabled = selection === null;
    backButton.hidden = model === null;
    keptNote.hidden = model === null;
    if (selection === null) {
      selectionNote.textContent = "Nothing is selected yet. Drag a box on the picture, or press Whole picture.";
    } else if (problem !== null) {
      selectionNote.textContent = problem;
    } else if (crop !== null) {
      selectionNote.textContent = `Selected: ${crop.w} × ${crop.h} pixels.`;
    }
  }

  function setSelection(next: Rect | null): void {
    selection = next;
    if (next === null) {
      selBox.hidden = true;
    } else {
      selBox.hidden = false;
      selBox.style.left = `${next.x * 100}%`;
      selBox.style.top = `${next.y * 100}%`;
      selBox.style.width = `${next.w * 100}%`;
      selBox.style.height = `${next.h * 100}%`;
    }
    updateSelectControls();
  }

  /** The pointer's place as a fraction of the picture. */
  function pointOf(event: { clientX: number; clientY: number }): Point | null {
    const box = stage.getBoundingClientRect();
    if (!(box.width > 0 && box.height > 0)) return null;
    return { x: (event.clientX - box.left) / box.width, y: (event.clientY - box.top) / box.height };
  }

  let drag: { kind: "new"; start: Point } | { kind: "corner"; corner: Corner } | null = null;

  function onPointerDown(event: PointerEvent): void {
    if (event.button !== undefined && event.button !== 0) return;
    const point = pointOf(event);
    if (point === null) return;
    const target = event.target as HTMLElement | null;
    const corner = CORNERS.find((c) => handles.get(c) === target);
    drag = corner && selection ? { kind: "corner", corner } : { kind: "new", start: point };
    try {
      stage.setPointerCapture?.(event.pointerId);
    } catch {
      // The pointer is gone already; the drag then ends with the next event.
    }
    event.preventDefault();
  }

  function onPointerMove(event: PointerEvent): void {
    if (drag === null) return;
    const point = pointOf(event);
    if (point === null) return;
    if (drag.kind === "new") setSelection(rectFromPoints(drag.start, point, UNIT));
    else if (selection) setSelection(moveCorner(selection, drag.corner, point, UNIT));
  }

  function onPointerEnd(): void {
    if (drag === null) return;
    drag = null;
    if (selection && (selection.w < MIN_DRAG || selection.h < MIN_DRAG)) setSelection(null);
  }

  stage.addEventListener("pointerdown", onPointerDown);
  stage.addEventListener("pointermove", onPointerMove);
  stage.addEventListener("pointerup", onPointerEnd);
  stage.addEventListener("pointercancel", onPointerEnd);

  function onHandleKey(corner: Corner, event: KeyboardEvent): void {
    if (!selection) return;
    const step = event.shiftKey ? 0.002 : 0.01;
    const moves: Record<string, [number, number]> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    };
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    const next = nudgeCorner(selection, corner, move[0], move[1], UNIT);
    if (next.w >= MIN_DRAG && next.h >= MIN_DRAG) setSelection(next);
  }

  function onDocumentKey(event: KeyboardEvent): void {
    if (event.key !== "Escape") return;
    if (editing !== null) {
      // Esc belongs to the editor's own small dialogs first (atom label, mass, valence): the same
      // key press reaches this listener too and must not close the whole editor behind them.
      if (editorBusy || event.defaultPrevented || libraryDialogOpen()) return;
      // Esc never throws a drawing away: only an untouched editor closes.
      if (editorChanged()) {
        editorProblem("The drawing was changed. Press Cancel to discard the changes, or Save to keep them.", "info");
        return;
      }
      closeEditor();
      return;
    }
    if (view === "select" && selection !== null) {
      drag = null;
      setSelection(null);
      announce("Selection cleared.");
    }
  }
  doc.addEventListener("keydown", onDocumentKey);

  // ---------------------------------------------------------------- recognise
  let working: unknown = null;

  function stopWorking(): void {
    if (working !== null) timers.clearInterval(working);
    working = null;
  }

  function showSelectProblem(text: string | null, kind: "error" | "info" = "error"): void {
    selectProblem.replaceChildren(...(text === null ? [] : [banner(kind, text)]));
  }

  /** Counts the recognitions asked for; an answer is used only when it belongs to the latest one. */
  let recogniseRun = 0;
  /** Recognitions the user went back from and the program is still working on. */
  let abandoned = 0;
  const ABANDONED_NOTE =
    "The program still finishes reading the earlier picture; its result is thrown away. A new recognition starts after that, so it takes longer.";

  function showResult(): void {
    if (!model || !result || !sent) return;
    renderResult();
    show("result");
    resultView.querySelector<HTMLElement>('[data-key="result-title"]')?.focus();
  }

  async function recognise(): Promise<void> {
    if (picture === null || selection === null || view !== "select") return;
    showSelectProblem(abandoned > 0 ? ABANDONED_NOTE : null, "info");
    const crop = currentCrop();
    const problem = cropProblem(crop);
    if (crop === null || problem !== null) {
      showSelectProblem(problem);
      return;
    }
    const source = picture;
    const encoded = await encodeWithinLimit(crop, (w, h) => deps.imaging.crop(source, crop, w, h));
    if (destroyed) return;
    if (encoded === null) {
      showSelectProblem("The selected area could not be prepared for reading. Select a smaller area and try again.");
      return;
    }
    if (view !== "select") return; // pressed twice while the picture was prepared
    const asked = mode;
    const run = ++recogniseRun;
    const started = timers.now();
    const elapsed = el("p", { class: "elapsed", attrs: { "aria-hidden": "true" }, text: "0 s" });
    const workingTitle = el("h2", { text: asked === "reaction" ? "Reading the reaction…" : "Reading the molecule…", attrs: { tabindex: "-1" } });
    // The way out of a wrong box or a wrong mode. The program cannot be stopped in the middle
    // of a picture, so the answer is simply not used when it comes.
    const back = button("Back to the selection", () => {
      if (run !== recogniseRun || view !== "working") return;
      recogniseRun += 1;
      abandoned += 1;
      stopWorking();
      show("select");
      showSelectProblem(ABANDONED_NOTE, "info");
      recogniseButton.focus();
      announce("Back at the selection. The program still finishes the earlier picture.");
    });
    workingView.replaceChildren(
      workingTitle,
      elapsed,
      el("p", {
        text:
          "This usually takes 10 to 60 seconds. The first time after a restart the program also loads its models, " +
          "which takes 20 to 40 seconds more, and a few minutes the very first time.",
      }),
      el("p", { class: "small", text: "You can leave this tab open and work in another one. Nothing leaves this PC." }),
      el(
        "div",
        { class: "row" },
        back,
        el("span", { class: "small", text: "Wrong box or wrong choice? Go back; the program finishes this picture first, and its result is thrown away." }),
      ),
    );
    show("working");
    // The Recognise button that had the focus is hidden now.
    workingTitle.focus();
    announce("Reading the picture. This can take a minute.");
    stopWorking();
    working = timers.setInterval(() => {
      elapsed.textContent = `${Math.round((timers.now() - started) / 1000)} s`;
    }, 1000);

    const reply = await client.request("recognize", { image: encoded.base64, mode: asked });
    if (destroyed) return;
    if (run !== recogniseRun) {
      // The user went back from this one: its answer is not used.
      abandoned = Math.max(0, abandoned - 1);
      if (abandoned === 0 && view === "select" && selectProblem.textContent === ABANDONED_NOTE) showSelectProblem(null);
      return;
    }
    stopWorking();
    if (!reply.ok) {
      show("select");
      showSelectProblem(errorSentence(reply.error, "recognize"));
      recogniseButton.focus();
      void refreshStatus();
      return;
    }
    // The program has just answered: it is installed and ready, whatever the last status question said.
    statusError = null;
    status = { ...(status ?? {}), installed: true, state: "ready" };
    renderStatus();
    result = reply.result;
    sent = { url: `data:image/png;base64,${encoded.base64}`, scale: encoded.scale };
    model = modelFromResult(reply.result);
    targetStep.clear();
    chemdrawNote = null;
    resultNotice = null;
    showResult();
    announce(`Finished: ${model.molecules.length} molecule${model.molecules.length === 1 ? "" : "s"} found. Check every structure.`);
  }

  // ---------------------------------------------------------------- result
  let chemdrawNote: { kind: "ok" | "info" | "error"; text: string } | null = null;
  let chemdrawBusy = false;
  /** Why the last change of a role or step was not made; shown until the next change. */
  let resultNotice: string | null = null;
  const boxNodes = new Map<string, HTMLElement>();

  /**
   * Re-renders the result and puts the focus back where it was, or on the
   * control `preferKey` when the one that was used has moved (a card that went
   * to another step or role).
   */
  function rerender(preferKey: string | null = null): void {
    const active = doc.activeElement as HTMLElement | null;
    const key = preferKey ?? active?.dataset?.key ?? null;
    const card = active?.closest?.<HTMLElement>("[data-mol]") ?? null;
    const mol = card?.dataset.mol ?? null;
    const kind = active?.dataset?.kind ?? null;
    renderResult();
    const again =
      (key !== null ? [...resultView.querySelectorAll<HTMLElement>("[data-key]")].find((node) => node.dataset.key === key) : undefined) ??
      (mol !== null && kind !== null
        ? [...resultView.querySelectorAll<HTMLElement>("[data-mol]")]
            .filter((node) => node.dataset.mol === mol)
            .flatMap((node) => [...node.querySelectorAll<HTMLElement>("[data-kind]")])
            .find((node) => node.dataset.kind === kind)
        : undefined);
    again?.focus();
  }

  function structureImage(molecule: Molecule): HTMLElement {
    const svg = molecule.molfile !== null ? deps.renderStructure(molecule.molfile, 260, 170) : null;
    const src = svgDataUrl(svg);
    if (src === null) return el("div", { class: "structure none", text: molecule.molfile === null ? "No structure" : "Cannot be drawn" });
    const alt = molecule.edited ? `Structure of molecule ${molecule.n} as you drew it` : `Structure of molecule ${molecule.n} as the program read it`;
    return el("img", { class: "structure", attrs: { src, alt } });
  }

  function stepOptions(select: HTMLSelectElement, current: number | "new"): void {
    if (!model) return;
    model.steps.forEach((_step, index) => select.append(el("option", { attrs: { value: String(index) }, text: `Step ${index + 1}` })));
    select.append(el("option", { attrs: { value: "new" }, text: "A new step" }));
    select.value = String(current);
  }

  function readStep(value: string): number | "new" {
    return value === "new" ? "new" : Number(value);
  }

  function card(molecule: Molecule, placement: Placement | null): HTMLElement {
    const m = model!;
    const where = placement ? `${placement.step}:${placement.role}` : "unused";
    const keyBase = `${molecule.key}:${where}`;

    const roleSelect = el("select", { attrs: { "aria-label": `Role of molecule ${molecule.n}` } });
    roleSelect.dataset.key = `role:${keyBase}`;
    roleSelect.dataset.kind = "role";
    for (const role of ROLES) roleSelect.append(el("option", { attrs: { value: role }, text: ROLE_LABELS[role] }));
    roleSelect.append(el("option", { attrs: { value: "unused" }, text: "Not used" }));
    roleSelect.value = placement ? placement.role : "unused";

    const stepSelect = el("select", { attrs: { "aria-label": `Step of molecule ${molecule.n}` } });
    stepSelect.dataset.key = `step:${keyBase}`;
    stepSelect.dataset.kind = "step";
    const defaultTarget: number | "new" = m.steps.length > 0 ? 0 : "new";
    const remembered = targetStep.get(molecule.key);
    const target = remembered === "new" || (typeof remembered === "number" && remembered < m.steps.length) ? remembered : defaultTarget;
    stepOptions(stepSelect, placement ? placement.step : target);

    /** The key of this molecule's control `kind` on the card it has after a move. */
    const keyAt = (kind: "role" | "step", to: { step: number | "new"; role: Role } | null): string =>
      to === null ? `${kind}:${molecule.key}:unused` : `${kind}:${molecule.key}:${to.step === "new" ? m.steps.length - 1 : to.step}:${to.role}`;

    roleSelect.addEventListener("change", () => {
      resultNotice = null;
      const choice = roleSelect.value as RoleChoice;
      const to = choice === "unused" ? null : { step: placement ? placement.step : readStep(stepSelect.value), role: choice };
      place(m, molecule.key, placement, to);
      rerender(keyAt("role", to));
    });
    stepSelect.addEventListener("change", () => {
      resultNotice = null;
      const step = readStep(stepSelect.value);
      if (!placement) {
        targetStep.set(molecule.key, step);
        rerender();
        return;
      }
      // A molecule has one role per step. Moving this card onto a step that already uses the
      // molecule would silently take that other use away, so it is refused and said instead.
      const there = step !== "new" && step !== placement.step ? roleIn(m, molecule.key, step) : null;
      if (step !== "new" && there !== null) {
        resultNotice =
          `Molecule ${molecule.n} is already in step ${step + 1} (${ROLE_LABELS[there].toLowerCase()}), and a molecule has one role per step. ` +
          `Nothing was changed. To change what it does in step ${step + 1}, use the Role box of its card there; to take it out of step ${placement.step + 1}, set this card to Not used.`;
        announce(resultNotice);
        rerender();
        return;
      }
      const to = { step, role: placement.role };
      place(m, molecule.key, placement, to);
      rerender(keyAt("step", to));
    });

    const edit = button("Edit", () => openEditor(molecule.key), "btn small-btn", { "aria-label": `Edit molecule ${molecule.n}` });
    edit.dataset.key = `edit:${keyBase}`;
    edit.dataset.kind = "edit";
    const remove = button(
      "Remove",
      () => {
        resultNotice = null;
        removeMolecule(m, molecule.key);
        announce(`Molecule ${molecule.n} removed.`);
        rerender();
      },
      "btn small-btn",
      { "aria-label": `Remove molecule ${molecule.n} from the result` },
    );
    remove.dataset.key = `remove:${keyBase}`;

    // The program's score says nothing about a molecule it could not read at all.
    const readable = molecule.smiles !== null && molecule.smiles !== "";
    const confidence = readable ? confidenceText(molecule.confidence) : null;
    const node = el(
      "li",
      { class: "card" },
      el(
        "div",
        { class: "card-head" },
        // "Molecule" as real (hidden) text: an aria-label on a plain span is not read.
        el("span", { class: "card-name" }, el("span", { class: "sr-only", text: "Molecule " }), el("span", { class: "num", text: String(molecule.n) })),
        molecule.edited
          ? el("span", { class: "tag edited", text: "edited by you" })
          : confidence !== null
            ? el("span", { class: "tag", text: confidence, attrs: { title: CONFIDENCE_HELP } })
            : null,
      ),
      structureImage(molecule),
      el(
        "div",
        { class: "smiles-row" },
        molecule.smiles !== null && molecule.smiles !== ""
          ? el("code", { class: "mono smiles", text: molecule.smiles })
          : el("span", { class: "small", text: "No SMILES" }),
        molecule.smiles ? copyButton(`SMILES of molecule ${molecule.n}`, () => molecule.smiles ?? "", `copy:${keyBase}`) : null,
      ),
      molecule.warnings.length > 0
        ? el("ul", { class: "warnings" }, ...molecule.warnings.map((code) => el("li", { text: warningText(code, molecule.edited) })))
        : null,
      el(
        "div",
        { class: "card-controls" },
        el("label", { class: "ctl" }, el("span", { text: "Role" }), roleSelect),
        el("label", { class: "ctl" }, el("span", { text: placement ? "Step" : "Goes to" }), stepSelect),
        el("div", { class: "row inline" }, edit, remove),
      ),
    );
    node.dataset.mol = molecule.key;
    const hot = (on: boolean) => () => boxNodes.get(molecule.key)?.classList.toggle("hot", on);
    node.addEventListener("mouseenter", hot(true));
    node.addEventListener("mouseleave", hot(false));
    node.addEventListener("focusin", hot(true));
    node.addEventListener("focusout", hot(false));
    return node;
  }

  function roleGroup(step: number, role: Role): HTMLElement {
    const molecules = moleculesOf(model!, step, role);
    return el(
      "div",
      { class: `role-group role-${role}` },
      el("h4", { text: role === "reactant" ? "Reactants" : role === "agent" ? "Over the arrow" : "Products" }),
      molecules.length > 0
        ? el("ul", { class: "cards" }, ...molecules.map((molecule) => card(molecule, { step, role })))
        : el("p", { class: "small empty", text: "None" }),
    );
  }

  function stepSection(index: number): HTMLElement {
    const m = model!;
    const step = m.steps[index]!;
    const smiles = reactionSmiles(m, index);
    const title = m.steps.length > 1 ? `Step ${index + 1}` : "Reaction";
    const removeButton = button(
      "Remove this step",
      () => {
        removeStep(m, index);
        announce(`${title} removed. Its molecules are kept.`);
        rerender();
      },
      "btn small-btn",
    );
    removeButton.dataset.key = `remove-step:${index}`;

    const conditions = el("textarea", {
      class: "conditions",
      attrs: {
        rows: String(Math.min(12, Math.max(3, step.conditions.split("\n").length + 1))),
        spellcheck: "false",
        // Text read from the picture stays in memory: without this, Firefox's session restore
        // writes the content of a form field that differs from its default into the profile.
        autocomplete: "off",
        "aria-describedby": `conditions-note-${index}`,
      },
    });
    conditions.value = step.conditions;
    conditions.dataset.key = `conditions:${index}`;
    conditions.addEventListener("input", () => {
      step.conditions = conditions.value;
    });

    return el(
      "section",
      { class: "step" },
      el("div", { class: "step-head" }, el("h3", { text: title }), removeButton),
      el("div", { class: "roles" }, roleGroup(index, "reactant"), el("div", { class: "arrow", attrs: { "aria-hidden": "true" }, text: "→" }), roleGroup(index, "agent"), el("div", { class: "arrow", attrs: { "aria-hidden": "true" }, text: "→" }), roleGroup(index, "product")),
      el(
        "div",
        { class: "rxn-line" },
        el("span", { class: "field-label", text: "Reaction SMILES" }),
        smiles !== null
          ? el("code", { class: "mono smiles", text: smiles })
          : el("span", { class: "small", text: "Not available: the step needs a reactant or a product, and each of them needs a readable structure." }),
        smiles !== null ? copyButton(`reaction SMILES of ${title.toLowerCase()}`, () => reactionSmiles(m, index) ?? "", `copy-rxn:${index}`) : null,
      ),
      el(
        "label",
        { class: "field" },
        el("span", { text: "Conditions (the text read at the arrow, one per line)" }),
        conditions,
      ),
      el("p", {
        class: "small",
        attrs: { id: `conditions-note-${index}` },
        text: "Formulas of reagents are often misread (about every second one). Correct the text here; it is used as it stands.",
      }),
    );
  }

  function picturePanel(): HTMLElement {
    boxNodes.clear();
    const m = model!;
    const r = result!;
    const frame = el("div", { class: "picture" }, el("img", { class: "picture-img", attrs: { src: sent!.url, alt: "The selected area with a numbered box around every molecule found" } }));
    // Never shown larger than it is: an enlarged picture is blurred and harder to compare.
    // The stylesheet also keeps a wide picture low enough to stay in view above the steps.
    frame.style.setProperty("--natural", `${r.width}px`);
    frame.style.setProperty("--ratio", String(Math.round((r.width / r.height) * 1000) / 1000));
    for (const molecule of m.molecules) {
      if (molecule.box === null) continue;
      const at = boxPercent(molecule.box, r.width, r.height);
      if (at === null) continue;
      const node = el("div", { class: "mol-box", attrs: { "aria-hidden": "true" } }, el("span", { class: "num", text: String(molecule.n) }));
      node.style.left = `${at.left}%`;
      node.style.top = `${at.top}%`;
      node.style.width = `${at.width}%`;
      node.style.height = `${at.height}%`;
      boxNodes.set(molecule.key, node);
      frame.append(node);
    }
    return el(
      "aside",
      { class: "picture-panel", attrs: { "aria-label": "The selected picture" } },
      el("h2", { text: "The picture" }),
      frame,
      el("p", {
        class: "small",
        text:
          `Read in ${Math.round(r.seconds)} s. The numbers are the molecules below.` +
          (sent!.scale < 1 ? ` The area was large and was reduced to ${Math.round(sent!.scale * 100)} % before reading; small text may have suffered.` : ""),
      }),
    );
  }

  async function openInChemdraw(): Promise<void> {
    if (!model || chemdrawBusy) return;
    const request = chemdrawRequest(model);
    if (!request.ok) {
      chemdrawNote = { kind: "error", text: request.problem };
      rerender();
      return;
    }
    chemdrawBusy = true;
    chemdrawNote = { kind: "info", text: "Writing the ChemDraw file…" };
    rerender();
    const reply = await client.request("reaction_to_chemdraw", { steps: request.steps });
    chemdrawBusy = false;
    if (destroyed) return;
    // Molecules in no step are not part of a reaction, so they are not in the file: say so.
    const leftOut =
      request.leftOut === 0
        ? ""
        : request.leftOut === 1
          ? " One molecule that is in no step was left out."
          : ` ${request.leftOut} molecules that are in no step were left out.`;
    if (!reply.ok) chemdrawNote = { kind: "error", text: errorSentence(reply.error, "reaction_to_chemdraw") };
    else if (reply.result.opened) chemdrawNote = { kind: "ok", text: `Opened in ChemDraw. The file is ${reply.result.path}${leftOut}` };
    else chemdrawNote = { kind: "info", text: `The file was written but not opened${reply.result.message ? `: ${reply.result.message}` : "."} It is ${reply.result.path}${leftOut}` };
    rerender();
    announce(chemdrawNote.text);
  }

  function renderResult(): void {
    if (!model || !result || !sent) return;
    const m = model;
    const unused = unusedMolecules(m);
    const hasSteps = m.steps.length > 0;

    const copyAll = copyButton("the whole result as text", () => copyAllText(m), "copy-all");
    copyAll.textContent = "Copy all";
    copyAll.className = "btn";
    const chemdraw = button(chemdrawBusy ? "Opening…" : "Open in ChemDraw", () => void openInChemdraw(), "btn primary");
    chemdraw.dataset.key = "chemdraw";
    chemdraw.disabled = chemdrawBusy;
    const addMol = button("Add molecule", () => openEditor(null));
    addMol.dataset.key = "add-molecule";
    const addStepButton = button("Add step", () => {
      addStep(m);
      rerender();
    });
    addStepButton.dataset.key = "add-step";
    const again = button("New selection", () => {
      showSelectProblem(null);
      // The result is not thrown away: "Back to the result" in the selection view shows it again.
      updateSelectControls();
      show("select");
      recogniseButton.focus();
      // The status line may be old (or was never answered); ask again.
      void refreshStatus();
    });
    again.dataset.key = "new-selection";

    const noteKind = chemdrawNote?.kind === "ok" ? "ok" : chemdrawNote?.kind === "error" ? "error" : "info";
    // Takes the focus when the result appears (the button that was pressed is hidden by then).
    const resultTitle = el("h2", { text: "2. Check the result", attrs: { tabindex: "-1" } });
    resultTitle.dataset.key = "result-title";

    resultView.replaceChildren(
      el(
        "div",
        { class: "panel result-head" },
        resultTitle,
        banner(
          "warn",
          el("strong", { text: "This is a draft." }),
          "Compare every structure with the picture. A misread structure often looks perfectly reasonable: " +
            "typical mistakes are a wrong or missing group, wrong wedges, a lost counter-ion or an odd element.",
        ),
        ...result.warnings.map((code) => banner("info", RESULT_WARNING_TEXT[code])),
        el("div", { class: "row" }, chemdraw, copyAll, addMol, addStepButton, again),
        chemdrawNote ? banner(noteKind, chemdrawNote.text) : null,
        resultNotice !== null ? banner("warn", resultNotice) : null,
      ),
      el(
        "div",
        // A wide scheme (most are) gets the full width above the steps; beside them it would be too small to compare.
        { class: isWidePicture(result.width, result.height) ? "result-body wide" : "result-body" },
        picturePanel(),
        el(
          "div",
          { class: "steps" },
          ...m.steps.map((_step, index) => stepSection(index)),
          unused.length > 0 || !hasSteps
            ? el(
                "section",
                { class: "step unused" },
                el("h3", { text: hasSteps ? "Not used in a reaction" : "Molecules" }),
                hasSteps
                  ? el("p", { class: "small", text: "Found in the picture but in no step. Give a molecule a role to use it." })
                  : el("p", { class: "small", text: "To build a reaction from these, give them a role." }),
                unused.length > 0
                  ? el("ul", { class: "cards" }, ...unused.map((molecule) => card(molecule, null)))
                  : el("p", { class: "small empty", text: "No molecule." }),
              )
            : null,
          el("p", { class: "small", text: NEW_CAPTURE_HELP }),
        ),
      ),
    );
  }

  // ---------------------------------------------------------------- editor
  /** The molecule being edited: its key, or "new" for "Add molecule"; null when the editor is closed. */
  let editing: string | null = null;
  let editor: StructureEditor | null = null;
  let editorBusy = false;
  let editorReturnKey: string | null = null;
  /** The drawing as it was when the editor opened, to tell whether the user changed it. */
  let editorStart: string | null = null;
  const editorHost = el("div", { class: "editor" });
  /** The part of the picture the molecule was read from, to compare with while drawing. */
  const editorReference = el("div", { class: "ref-host" });
  const editorMessage = el("div", null);
  const editorTitle = el("h2", { class: "dialog-title", attrs: { id: "editor-title", tabindex: "-1" } });
  const saveButton = button("Save", () => void saveEditor(), "btn primary");
  const cancelButton = button("Cancel", () => closeEditor());
  const dialog = el(
    "div",
    { class: "dialog", attrs: { role: "dialog", "aria-modal": "true", "aria-labelledby": "editor-title" } },
    editorTitle,
    el("p", { class: "small", text: "Draw the structure as it is in the picture. Save takes it over; the SMILES is then worked out by the program on this PC." }),
    editorReference,
    editorHost,
    editorMessage,
    el("div", { class: "row" }, saveButton, cancelButton),
  );
  const overlay = el("div", { class: "overlay" }, dialog);
  overlay.hidden = true;
  overlayHost.append(overlay);
  // Keeps the keyboard focus inside the open dialog.
  const onFocusIn = (event: FocusEvent): void => {
    if (editing === null || libraryDialogOpen()) return;
    const target = event.target as Node | null;
    if (target && !dialog.contains(target)) editorTitle.focus();
  };
  doc.addEventListener("focusin", onFocusIn);

  function editorProblem(text: string | null, kind: "error" | "info" = "error"): void {
    editorMessage.replaceChildren(...(text === null ? [] : [banner(kind, text)]));
  }

  /**
   * True while the structure editor shows one of its own small dialogs (atom
   * label, mass, valence). OpenChemLib opens them as a native <dialog>, in the
   * document or in the shadow tree of its editor; this page has no <dialog> of
   * its own.
   */
  function libraryDialogOpen(): boolean {
    const openIn = (scope: ParentNode): boolean => {
      if (scope.querySelector("dialog[open]") !== null) return true;
      for (const node of scope.querySelectorAll("*")) if (node.shadowRoot && openIn(node.shadowRoot)) return true;
      return false;
    };
    return doc.querySelector("dialog[open]") !== null || openIn(editorHost);
  }

  /** What the drawing is compared by: the editor's signature of the structure, else its molfile. */
  function readDrawing(): string | null {
    if (editor === null) return null;
    return editor.getSignature ? editor.getSignature() : editor.getMolfile();
  }

  /** True when the drawing is not what it was when the editor opened (or cannot be read: then nothing is thrown away either). */
  function editorChanged(): boolean {
    if (editor === null) return false;
    try {
      return readDrawing() !== editorStart;
    } catch {
      return true;
    }
  }

  /**
   * The part of the picture a molecule was read from (with a little of its
   * surroundings), or the whole picture for a molecule that has no box: shown
   * in the editor, which covers the picture on the page.
   */
  function referencePicture(molecule: Molecule | null): HTMLElement | null {
    if (!sent || !result) return null;
    const box = molecule?.box ?? null;
    const at = cutOutView(box, result.width, result.height) ?? cutOutView(null, result.width, result.height);
    if (at === null) return null;
    const whole = box === null || cutOutView(box, result.width, result.height) === null;
    const caption = whole ? "The picture" : `Molecule ${molecule!.n} in the picture`;
    const img = el("img", { class: "ref-img", attrs: { src: sent.url, alt: caption, draggable: "false" } });
    img.style.left = `${at.left}%`;
    img.style.top = `${at.top}%`;
    img.style.width = `${at.width}%`;
    img.style.height = `${at.height}%`;
    const frame = el("div", { class: "ref-frame" }, img);
    frame.style.setProperty("--ratio", String(Math.round(at.ratio * 1000) / 1000));
    // A small molecule may be shown up to twice its size, not more: beyond that it only gets blurred.
    frame.style.setProperty("--natural", `${Math.max(1, Math.round(at.pixels * 2))}px`);
    return el("figure", { class: "ref" }, el("figcaption", { class: "small", text: `${caption}:` }), frame);
  }

  function openEditor(key: string | null): void {
    if (!model || editing !== null) return;
    const molecule = key !== null ? findMolecule(model, key) : null;
    if (key !== null && molecule === null) return;
    editing = key ?? "new";
    editorReturnKey = (doc.activeElement as HTMLElement | null)?.dataset?.key ?? null;
    editorTitle.textContent = molecule ? `Edit molecule ${molecule.n}` : "Add a molecule";
    editorProblem(null);
    editorStart = null;
    const reference = referencePicture(molecule);
    editorReference.replaceChildren(...(reference ? [reference] : []));
    overlay.hidden = false;
    doc.body.classList.add("modal-open");
    try {
      editor ??= deps.createEditor(editorHost);
      editor.clear();
    } catch (error) {
      editor = null;
      editorProblem(`The structure editor could not be started: ${String(error)}`);
      saveButton.disabled = true;
      editorTitle.focus();
      return;
    }
    saveButton.disabled = false;
    if (molecule?.molfile) {
      try {
        editor.setMolfile(molecule.molfile);
      } catch {
        editorProblem("The structure the program read cannot be shown in the editor. Draw the molecule from scratch.");
      }
    }
    try {
      editorStart = readDrawing();
    } catch {
      editorStart = null;
    }
    editorTitle.focus();
  }

  function closeEditor(): void {
    if (editing === null || editorBusy) return;
    editing = null;
    overlay.hidden = true;
    editorReference.replaceChildren();
    doc.body.classList.remove("modal-open");
    const key = editorReturnKey;
    editorReturnKey = null;
    const back = key !== null ? [...resultView.querySelectorAll<HTMLElement>("[data-key]")].find((node) => node.dataset.key === key) : undefined;
    back?.focus();
  }

  async function saveEditor(): Promise<void> {
    if (!model || editing === null || editor === null || editorBusy) return;
    editorProblem(null);
    let molfile: string | null;
    try {
      molfile = editor.getMolfile();
    } catch (error) {
      editorProblem(`The drawing could not be read: ${String(error)}`);
      return;
    }
    if (molfile === null || molfile.trim() === "") {
      editorProblem("Nothing is drawn. Draw the structure, or press Cancel.");
      return;
    }
    if (molfile.length > MAX_STRUCTURE_CHARS) {
      editorProblem("The drawing is too large.");
      return;
    }
    editorBusy = true;
    saveButton.disabled = true;
    cancelButton.disabled = true;
    editorMessage.replaceChildren(el("p", { class: "busy", text: "Asking the program on this PC for the SMILES…" }));
    const reply = await client.request("describe_structures", { structures: [{ molfile }] });
    editorBusy = false;
    saveButton.disabled = false;
    cancelButton.disabled = false;
    if (destroyed || !model || editing === null) return;
    editorProblem(null);
    if (!reply.ok) {
      editorProblem(errorSentence(reply.error, "describe_structures"));
      return;
    }
    const description = reply.result.structures[0];
    if (!description) {
      editorProblem("The program on this PC gave no answer for this structure. Try again.");
      return;
    }
    const target = editing;
    let key: string;
    // False when Save was pressed on a drawing that is still the molecule as it was: then it is
    // not "edited by you", and it keeps the program's score and its warnings.
    let changed = true;
    if (target === "new") {
      key = addMolecule(model, molfile, description);
    } else {
      changed = applyEdit(model, target, molfile, description);
      key = target;
    }
    const n = findMolecule(model, key)?.n;
    closeEditor();
    renderResult();
    const card = [...resultView.querySelectorAll<HTMLElement>("[data-mol]")].find((node) => node.dataset.mol === key);
    card?.querySelector<HTMLElement>('[data-kind="edit"]')?.focus();
    card?.scrollIntoView?.({ block: "nearest" });
    announce(
      target === "new"
        ? `Molecule ${n} added. Give it a role to use it in a step.`
        : changed
          ? `Molecule ${n} changed.`
          : `Molecule ${n} is the same structure as before; nothing was changed.`,
    );
  }

  // ---------------------------------------------------------------- start
  show("message");
  messageView.replaceChildren(el("p", { class: "busy", text: "Loading the screenshot…" }));

  async function start(): Promise<void> {
    if (deps.captureId === null || deps.captureId === "") {
      // The keyboard shortcut could not take the picture: the background names the reason.
      const why = captureFailText(deps.failed);
      if (why !== null) {
        showMessage("The tab was not captured", ...why);
        return;
      }
      showMessage(
        "No screenshot",
        "This page was opened without a screenshot. Either the tab could not be captured (Firefox and Chrome do not allow pictures of their own pages, such as settings, the new tab page or the add-on store), or the page was opened by hand.",
      );
      return;
    }
    const reply = await client.image(deps.captureId);
    if (destroyed) return;
    if (!reply.ok) {
      // Nothing to read here (a reloaded, duplicated or restored tab): the recognition program is not started for it.
      showMessage("The screenshot is no longer available", reply.error.message);
      return;
    }
    // There is a picture: warm the recognition program up while it is shown and the user draws the box.
    // (The answer above comes from the background's memory at once, so this loses no time.)
    void refreshStatus();
    try {
      picture = await deps.imaging.load(reply.result.image);
    } catch (error) {
      if (destroyed) return;
      showMessage("The screenshot could not be shown", `The picture could not be read (${error instanceof Error ? error.message : String(error)}).`);
      return;
    }
    if (destroyed) return;
    shot.setAttribute("src", picture.url);
    shot.setAttribute("width", String(picture.width));
    shot.setAttribute("height", String(picture.height));
    setSelection(null);
    renderStatus();
    show("select");
    wholeButton.focus();
  }
  void start();

  return {
    destroy(): void {
      destroyed = true;
      timers.clearInterval(poll);
      stopWorking();
      doc.removeEventListener("keydown", onDocumentKey);
      doc.removeEventListener("focusin", onFocusIn);
      doc.body.classList.remove("modal-open");
      try {
        editor?.destroy();
      } catch {
        // already gone
      }
      root.replaceChildren();
    },
  };
}
