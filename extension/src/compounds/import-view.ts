/**
 * The "Import" view of the compounds page: choose the experiment list the ELN
 * exported (CSV), see how it compares with the registry, then "Check only" or
 * "Create IDs" (after a confirmation). The work itself is `ImportController`
 * (import.ts); this module only builds and updates the DOM from its state.
 *
 * Choosing a file or opening the view never opens a tab and changes nothing;
 * only the two start buttons do. Text from the file or the host only ever goes
 * in as text nodes; experiment links only via `experimentAnchor`.
 */

import { experimentPageUrl } from "../shared/import";
import { formatPercent } from "../content/yields";
import { experimentAnchor, renderErrorBox } from "../content/ui/common";
import type { Child, El } from "../content/ui/dom";
import {
  countersLine,
  countRuns,
  experimentLabel,
  progressLine,
  summaryLines,
  timeLeftText,
  type ExperimentRun,
  type ImportController,
  type ImportRule,
  type ImportState,
} from "./import";
import { BOM } from "./state";

export const IMPORT_INTRO =
  "Export your experiment list from the ELN as a CSV file and choose it here. The file tells the registry which experiments exist; " +
  "every structure is still read from the experiment page, so each experiment is opened once in a background tab. " +
  "Stay logged in to the ELN while it runs.";
export const IMPORT_REPORT_NAME = "import-report.csv";
/** Larger files are refused before they are read (a list of thousands of experiments is far smaller). */
export const MAX_IMPORT_FILE_BYTES = 20 * 1024 * 1024;
/** While a walk runs, the progress is announced to screen readers every this many experiments. */
export const IMPORT_ANNOUNCE_EVERY = 10;

const DASH = "—";

export interface ImportViewDeps {
  doc: Document;
  el: El;
  controller: ImportController;
  copyText(text: string): Promise<void>;
  downloadText(fileName: string, text: string, mimeType: string): void;
  /** The page's polite live region. */
  announce(message: string): void;
  /** Whether the registry's name generator is available (passed on as `names`). */
  namesAvailable(): boolean;
  /** Reads a chosen file; default `file.arrayBuffer()`. */
  readFile?(file: File): Promise<ArrayBuffer>;
}

export interface ImportView {
  section: HTMLElement;
  render(): void;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function defaultReadFile(file: File): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === "function") return file.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error ?? new Error("the file could not be read"));
    reader.readAsArrayBuffer(file);
  });
}

export function buildImportView(deps: ImportViewDeps): ImportView {
  const { el, controller } = deps;
  const readFile = deps.readFile ?? defaultReadFile;

  // ---------------------------------------------------------------- file
  const fileInput = el("input", {
    key: "import-file",
    attrs: { id: "import-file", type: "file", accept: ".csv,text/csv", "aria-describedby": "import-file-hint" },
  });
  const fileInfo = el("div", { class: "import-file-info" });
  const fileProblem = el("div", { class: "import-file-problem" });

  // ---------------------------------------------------------------- plan line
  const planArea = el("div", { class: "import-plan" });

  // ---------------------------------------------------------------- options
  const ruleRadio = (value: ImportRule): HTMLInputElement => {
    const input = el("input", { key: `import-rule-${value}`, attrs: { type: "radio", name: "import-rule", value, id: `import-rule-${value}` } });
    input.checked = value === controller.state.options.rule;
    return input;
  };
  const ruleYield = ruleRadio("with_yield");
  const ruleAll = ruleRadio("all");
  const reopen = el("input", { key: "import-reopen", attrs: { id: "import-reopen", type: "checkbox" } });
  const warnings = el("input", { key: "import-warnings", attrs: { id: "import-warnings", type: "checkbox" } });
  const options = el(
    "fieldset",
    { class: "import-options" },
    el("legend", { text: "Which products" }),
    el(
      "label",
      { class: "check", attrs: { for: "import-rule-with_yield" } },
      ruleYield,
      el("span", { text: "Products with an isolated yield above 0 (recommended)" }),
    ),
    el("label", { class: "check", attrs: { for: "import-rule-all" } }, ruleAll, el("span", { text: "Every product, also without a yield" })),
    el(
      "label",
      { class: "check", attrs: { for: "import-reopen" } },
      reopen,
      el("span", { text: "Also open experiments whose products all have an ID already" }),
    ),
    el(
      "label",
      { class: "check", attrs: { for: "import-warnings" } },
      warnings,
      el("span", { text: "Also create IDs for structures that have warnings" }),
    ),
  );
  const readOptions = () => {
    controller.setOptions({
      rule: ruleAll.checked ? "all" : "with_yield",
      reopenRegistered: reopen.checked,
      allowWarnings: warnings.checked,
    });
  };
  for (const input of [ruleYield, ruleAll, reopen, warnings]) input.addEventListener("change", readOptions);

  // ---------------------------------------------------------------- buttons
  const checkButton = el("button", {
    class: "btn",
    key: "import-check",
    attrs: { id: "import-check", type: "button" },
    text: "Check only (creates nothing)",
  });
  const createButton = el("button", { class: "btn primary", key: "import-create", attrs: { id: "import-create", type: "button" }, text: "Create IDs" });
  const stopButton = el("button", { class: "btn", key: "import-stop", attrs: { id: "import-stop", type: "button" }, text: "Stop" });
  const startHint = el("p", { class: "small import-start-hint" });

  let confirming: { experiments: number; products: number } | null = null;
  const confirmText = el("p", { class: "import-confirm-text", attrs: { id: "import-confirm-text" } });
  const confirmButton = el("button", {
    class: "btn primary",
    key: "import-confirm",
    attrs: { id: "import-confirm", type: "button", "aria-describedby": "import-confirm-text" },
    text: "Create IDs",
  });
  const cancelButton = el("button", { class: "btn", key: "import-cancel", attrs: { id: "import-cancel", type: "button" }, text: "Cancel" });
  const confirmBox = el(
    "div",
    { class: "banner warn import-confirm", attrs: { role: "group", "aria-labelledby": "import-confirm-text" } },
    confirmText,
    el("div", { class: "row" }, confirmButton, cancelButton),
  );
  confirmBox.hidden = true;

  // ---------------------------------------------------------------- progress and summary
  const progressText = el("p", { class: "import-progress-line" });
  const progressBar = el("progress", { attrs: { id: "import-progress", "aria-label": "Experiments handled" } });
  const countersText = el("p", { class: "small import-counters" });
  const timeLeft = el("p", { class: "small import-time-left" });
  const progressArea = el("div", { class: "import-progress" }, progressText, progressBar, countersText, timeLeft);
  progressArea.hidden = true;

  const summary = el("div", { class: "import-summary" });
  const downloadButton = el("button", { class: "btn", key: "import-download", attrs: { type: "button" }, text: "Download report" });
  const copyButton = el("button", { class: "btn", key: "import-copy", attrs: { type: "button" }, text: "Copy table" });
  const reportFeedback = el("span", { class: "small export-feedback import-report-feedback", attrs: { "aria-live": "polite" } });
  const reportRow = el("div", { class: "row import-report" }, downloadButton, copyButton, reportFeedback);
  reportRow.hidden = true;

  // ---------------------------------------------------------------- table
  const tableBodies: HTMLTableSectionElement[] = [];
  const table = el(
    "table",
    { class: "import-table", attrs: { "aria-labelledby": "import-table-caption" } },
    el("caption", { class: "sr-only", attrs: { id: "import-table-caption" }, text: "Experiments in the list" }),
    el(
      "thead",
      null,
      el(
        "tr",
        null,
        el("th", { attrs: { scope: "col" }, text: "ELN number" }),
        el("th", { class: "num", attrs: { scope: "col" }, text: "Product" }),
        el("th", { attrs: { scope: "col" }, text: "Formula in the list" }),
        el("th", { class: "num", attrs: { scope: "col" }, text: "Yield in the list" }),
        el("th", { attrs: { scope: "col" }, text: "ID in the registry" }),
        el("th", { attrs: { scope: "col" }, text: "Result" }),
        el("th", { attrs: { scope: "col" }, text: "Notes" }),
      ),
    ),
  );
  table.hidden = true;

  const section = el(
    "section",
    { class: "results import-view", attrs: { id: "view-import", "aria-labelledby": "import-heading" } },
    el("h2", { attrs: { id: "import-heading" }, text: "Import experiments" }),
    el("p", { class: "small import-intro", attrs: { id: "import-file-hint" }, text: IMPORT_INTRO }),
    el(
      "div",
      { class: "import-file" },
      el("label", { class: "field", attrs: { for: "import-file" }, text: "Experiment list (CSV file)" }),
      fileInput,
    ),
    fileProblem,
    fileInfo,
    planArea,
    options,
    el("div", { class: "row import-buttons" }, checkButton, createButton, stopButton),
    startHint,
    confirmBox,
    progressArea,
    summary,
    reportRow,
    el("div", { class: "table-wrap" }, table),
  );
  section.hidden = true;

  // ---------------------------------------------------------------- events
  let reading = false;
  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (!file || controller.isRunning()) return;
    confirming = null;
    fileProblem.replaceChildren();
    if (file.size > MAX_IMPORT_FILE_BYTES) {
      showFileProblem(`${file.name} is too large for an experiment list (more than 20 MB).`);
      return;
    }
    reading = true;
    shown = null;
    render();
    readFile(file).then(
      (bytes) => {
        reading = false;
        shown = null;
        void controller.loadFile(file.name, bytes);
      },
      (error: unknown) => {
        reading = false;
        shown = null;
        showFileProblem(`${file.name} could not be read: ${error instanceof Error ? error.message : String(error)}`);
        render();
      },
    );
  });

  function showFileProblem(message: string): void {
    fileProblem.replaceChildren(el("div", { class: "banner error", attrs: { role: "alert" }, text: message }));
    deps.announce(message);
  }

  checkButton.addEventListener("click", () => {
    if (!canStart()) return;
    confirming = null;
    reportFeedback.textContent = "";
    void controller.run({ mode: "check", names: deps.namesAvailable() });
  });
  createButton.addEventListener("click", () => {
    if (!canStart()) return;
    confirming = controller.plannedWork();
    render();
    confirmButton.focus();
  });
  confirmButton.addEventListener("click", () => {
    if (confirming === null || !canStart()) return;
    confirming = null;
    reportFeedback.textContent = "";
    void controller.run({ mode: "create", names: deps.namesAvailable() });
  });
  cancelButton.addEventListener("click", () => {
    confirming = null;
    render();
    createButton.focus();
  });
  stopButton.addEventListener("click", () => controller.stop());

  downloadButton.addEventListener("click", () => {
    try {
      deps.downloadText(IMPORT_REPORT_NAME, BOM + controller.reportCsv(), "text/csv;charset=utf-8");
      reportFeedback.textContent = `The report was saved as ${IMPORT_REPORT_NAME}.`;
    } catch (error) {
      reportFeedback.textContent = `The report could not be saved: ${String(error)}`;
    }
  });
  copyButton.addEventListener("click", () => {
    deps.copyText(controller.reportTsv()).then(
      () => {
        reportFeedback.textContent = "The table was copied. Paste it into a spreadsheet.";
      },
      () => {
        reportFeedback.textContent = "Copying failed. Use Download report instead.";
      },
    );
  });

  // ---------------------------------------------------------------- state checks
  function canStart(): boolean {
    const s = controller.state;
    return (
      !controller.isRunning() &&
      !reading &&
      s.plan !== null &&
      !s.planLoading &&
      s.planError === null &&
      s.parsed !== null &&
      s.parsed.error === null &&
      controller.plannedWork().experiments > 0
    );
  }

  // ---------------------------------------------------------------- render: rows
  const bodies = new Map<ExperimentRun, HTMLTableSectionElement>();

  function notesCell(findings: readonly string[], notes: readonly string[]): HTMLElement {
    return el(
      "td",
      { class: "notes" },
      ...findings.map((f) => el("p", { class: "import-finding" }, el("strong", { text: "Check: " }), f)),
      ...notes.map((n) => el("p", { class: "small", text: n })),
    );
  }

  function runBody(run: ExperimentRun): HTMLTableSectionElement {
    const exp = run.plan.experiment;
    const quiet = run.phase === "skipped" || run.phase === "not opened" || (run.rows.length > 0 && run.rows.every((r) => r.kind === "not_reached"));
    const busy = run.phase === "opening" || run.phase === "working";
    const classes = ["import-experiment", quiet ? "import-quiet" : "", busy ? "import-busy" : ""].filter(Boolean).join(" ");
    const body = el("tbody", { class: classes, attrs: { "data-ref": exp.ref } });
    const link = (): Child => experimentAnchor(el, experimentLabel(exp), experimentPageUrl(exp.ref));
    const runFindings = run.findings;
    const runNotes = run.notes;
    if (run.rows.length === 0) {
      body.append(
        el(
          "tr",
          null,
          el("td", { class: "exp" }, link()),
          el("td", { class: "num", text: DASH }),
          el("td", { text: DASH }),
          el("td", { class: "num", text: DASH }),
          el("td", { text: DASH }),
          el("td", { class: "result", text: run.reason ? `skipped: ${run.reason}` : "" }),
          notesCell(runFindings, runNotes),
        ),
      );
      return body;
    }
    run.rows.forEach((row, i) => {
      const id = row.outcome?.id ?? row.registered?.id ?? null;
      const findings = [...(i === 0 ? runFindings : []), ...row.findings];
      const notes = [...(i === 0 ? runNotes : []), ...row.notes];
      body.append(
        el(
          "tr",
          { class: findings.length > 0 ? "import-row has-finding" : "import-row", attrs: { "data-product": String(row.index) } },
          el("td", { class: "exp" }, link()),
          el("td", { class: "num", text: String(row.index) }),
          el("td", { class: "formula", text: row.listed?.formula ?? DASH }),
          el("td", { class: "num", text: row.listed?.isolated != null ? `${formatPercent(row.listed.isolated)} %` : DASH }),
          el("td", { class: "id mono", text: id ?? DASH }),
          el("td", { class: "result", text: row.result }),
          notesCell(findings, notes),
        ),
      );
    });
    return body;
  }

  // ---------------------------------------------------------------- render
  let shown: ImportState | null = null;
  let announcedHandled = 0;

  function render(): void {
    const s = controller.state;
    const running = controller.isRunning();
    const started = canStart();

    fileInput.disabled = running;
    for (const input of [ruleYield, ruleAll, reopen, warnings]) input.disabled = running;
    ruleYield.checked = s.options.rule === "with_yield";
    ruleAll.checked = s.options.rule === "all";
    reopen.checked = s.options.reopenRegistered;
    warnings.checked = s.options.allowWarnings;

    checkButton.disabled = !started || confirming !== null;
    createButton.disabled = !started || confirming !== null;
    stopButton.hidden = !running;
    stopButton.disabled = s.status === "stopping";
    stopButton.textContent = s.status === "stopping" ? "Stopping after this experiment …" : "Stop";

    confirmBox.hidden = confirming === null;
    if (confirming !== null) {
      confirmText.textContent =
        `Create IDs for up to ${plural(confirming.products, "product", "products")} in ${plural(confirming.experiments, "experiment", "experiments")}? ` +
        "IDs are permanent and cannot be taken back.";
    }

    let hint = "";
    if (!running && s.plan !== null && !s.planLoading && controller.plannedWork().experiments === 0) {
      hint = "With these choices no experiment needs to be opened.";
    } else if (!running && s.parsed === null && !reading) {
      hint = "Choose a file first.";
    }
    startHint.textContent = hint;
    startHint.hidden = hint === "";

    if (s === shown) return;
    shown = s;

    // file
    const fileChildren: Child[] = [];
    if (reading) fileChildren.push(el("p", { class: "small busy", text: "Reading the file …" }));
    if (s.parsed !== null && s.fileName !== null) {
      if (s.parsed.error !== null) {
        fileChildren.push(
          el("div", { class: "banner error", attrs: { role: "alert" } }, el("strong", { text: s.fileName }), el("p", { text: s.parsed.error })),
        );
      } else {
        const products = s.parsed.experiments.reduce((n, e) => n + e.products.length, 0);
        fileChildren.push(
          el("p", { class: "import-file-name mono", text: s.fileName }),
          el("p", {
            class: "import-file-count",
            text: `${plural(s.parsed.experiments.length, "experiment", "experiments")}, ${plural(products, "product", "products")}`,
          }),
        );
        if (s.parsed.problems.length > 0) {
          fileChildren.push(
            el(
              "div",
              { class: "banner warn import-problems", attrs: { role: "note" } },
              el("strong", { text: `${plural(s.parsed.problems.length, "row is", "rows are")} not imported` }),
              el("ul", null, ...s.parsed.problems.map((p) => el("li", { text: `Line ${p.line}: ${p.reason}` }))),
            ),
          );
        }
      }
    }
    fileInfo.replaceChildren(...fileChildren.filter((c): c is HTMLElement => c instanceof HTMLElement));

    // plan
    const planChildren: Child[] = [];
    if (s.planLoading) planChildren.push(el("p", { class: "small busy", text: "Comparing the list with the registry …" }));
    if (s.planError) {
      planChildren.push(renderErrorBox(el, s.planError, "yields"));
      if (!running) {
        const retry = el("button", { class: "btn", key: "import-retry", attrs: { type: "button" }, text: "Retry" });
        retry.addEventListener("click", () => void controller.loadPlan());
        planChildren.push(el("div", { class: "row" }, retry));
      }
    }
    const line = controller.planLine();
    if (line && !s.planLoading && s.runMode === null) planChildren.push(el("p", { class: "import-plan-line", text: line }));
    planArea.replaceChildren(...planChildren.filter((c): c is HTMLElement => c instanceof HTMLElement));

    // progress
    progressArea.hidden = !running;
    if (running) {
      progressText.textContent = progressLine(s) ?? (s.status === "running" ? "Comparing the list with the registry …" : "");
      progressBar.max = Math.max(1, s.toOpen);
      progressBar.value = s.handled;
      countersText.textContent = s.runMode ? countersLine(countRuns(s.runs), s.runMode) : "";
      timeLeft.textContent = timeLeftText(s) ?? "";
      if (s.handled === 0) announcedHandled = 0;
      if (s.handled > 0 && s.handled % IMPORT_ANNOUNCE_EVERY === 0 && s.handled !== announcedHandled) {
        announcedHandled = s.handled;
        deps.announce(`${s.handled} of ${plural(s.toOpen, "experiment", "experiments")} done.`);
      }
    }

    // summary
    const lines = summaryLines(s);
    summary.replaceChildren(
      ...lines.map((text, i) => el("p", { class: i === 0 ? "import-summary-head" : "import-summary-line", text })),
    );
    summary.hidden = lines.length === 0;
    reportRow.hidden = s.status !== "finished";

    // table
    const nodes: HTMLTableSectionElement[] = [];
    const seen = new Set<ExperimentRun>();
    for (const run of s.runs) {
      seen.add(run);
      let body = bodies.get(run);
      if (!body) {
        body = runBody(run);
        bodies.set(run, body);
      }
      nodes.push(body);
    }
    for (const run of [...bodies.keys()]) if (!seen.has(run)) bodies.delete(run);
    if (nodes.length !== tableBodies.length || nodes.some((n, i) => n !== tableBodies[i])) {
      for (const old of tableBodies) old.remove();
      table.append(...nodes);
      tableBodies.splice(0, tableBodies.length, ...nodes);
    }
    table.hidden = nodes.length === 0;
    table.setAttribute("aria-busy", String(s.planLoading));
  }

  return { section, render };
}
