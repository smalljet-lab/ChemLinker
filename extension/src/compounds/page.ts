/**
 * The extension's compounds page (compounds.html): every registered compound,
 * one entry per CORE, with a text search, a drawn-structure search, sort
 * order and paging. Expanding an entry shows its stereo variants with names,
 * identifiers, properties, batches (with their yields) and "Spectra and
 * files": the variant's folder on this PC, the spectra recorded by the
 * MestReNova add-on and the other files in the folder (`compound_files`). A
 * PDF is loaded piece by piece (`read_pdf`) and shown in a viewer inside the
 * page (a blob URL of the bytes, typed application/pdf). Paths are text only,
 * never links. "Synthesis route" draws every reaction that led to the
 * variant as a ChemDraw file in its folder and opens it (`route`), with
 * "Save a copy" of the document, the experiments whose reaction is not
 * stored yet, and a switch of the starting material ("SM") of its runs
 * (`set_limiting`).
 *
 * A second view, "Yields" (switch at the top; the compound list is the
 * default), is a table of every batch with its experiment and stored yield
 * (`yields`), with a filter, "Show more", and an export of all matching rows
 * to the clipboard (tab-separated) or to a CSV file on this computer.
 *
 * A third view, "Import" (import-view.ts, import.ts), reads the experiment
 * list exported from the ELN and walks through the listed experiments in one
 * background tab of its own, where the content script looks the products up
 * and creates their IDs.
 *
 * Built once (header, search form, drawing area); the header and the results
 * are re-rendered from `CompoundsController` state. Entries whose data did not
 * change are kept as they are. Text from the host only ever goes in as text
 * nodes; drawings only as <img src="data:..."> via `svgDataUrl`; links only via
 * `experimentLink`.
 */

import {
  MAX_SEARCH_TEXT,
  MAX_STRUCTURE_CHARS,
  type CompoundFilesResult,
  type CompoundItem,
  type FindResult,
  type FolderFile,
  type FoundVariant,
  type HostError,
  type MatchMode,
  type RouteChoice,
  type SpectrumInfo,
  type YieldRow,
} from "../shared/protocol";
import { batchYieldText, formatPercent, hasYield } from "../content/yields";
import { shortDate } from "../content/labels";
import { readFilesFolder } from "../content/validate";
import {
  FilesController,
  FOLDER_MISSING_TEXT,
  folderFileLine,
  NO_SPECTRA_TEXT,
  otherFiles,
  spectrumHeading,
  type FilesEntry,
} from "./files";
import { loadPdf, progressText, type PdfLoad, type PdfSource } from "./pdf";
import {
  CDXML_TYPE,
  copyFileName,
  folderOf,
  missingExperimentText,
  missingTail,
  MISSING_HEADING,
  NOTHING_TO_DRAW,
  openedLine,
  RouteController,
  ROUTE_BUTTON,
  ROUTE_HEADING,
  ROUTE_HINT,
  ROUTE_RUNNING,
  SM_AUTO,
  SM_HEADING,
  SM_AMBIGUOUS_HINT,
  SM_OTHERS_TOGGLE,
  SM_SAVED,
  autoOptionLabel,
  candidateLabel,
  choiceLabel,
  selectionValue,
  valueSelection,
  stepsLine,
  type RouteEntry,
} from "./route";
import { defaultObjectUrls, PdfViewer, type ObjectUrls, type OpenOptions, type PdfViewerDeps } from "./pdf-viewer";
import type { HostClient } from "../content/host-client";
import { experimentText, datePart } from "../content/experiments";
import { matchedByText, stereoModeLabel } from "../content/labels";
import { generatorLabel, NameEditor } from "../content/name-edit";
import { idExamplesOf } from "../content/id-examples";
import { hostVersionOf, nameGeneratorOf, versionLine } from "../content/ping-state";
import { factsFromVariant, formatMolarMass, formulaText } from "../content/properties";
import { svgDataUrl } from "../content/safe-values";
import { experimentAnchor, renderErrorBox } from "../content/ui/common";
import { copyButton, renderDetailsList, renderNameRow, type CopyContext } from "../content/ui/details";
import { makeEl, type Child, type El } from "../content/ui/dom";
import type { EditorFactory, StructureEditor } from "./editor";
import { ImportController, type ImportTabs, type ImportTimers } from "./import";
import { buildImportView } from "./import-view";
import { EXTENSION_VERSION } from "../shared/site";
import {
  BOM,
  CompoundsController,
  isSearch,
  isYieldsFiltered,
  pageInfo,
  resultLine,
  yieldExperimentText,
  yieldsCountLine,
  YieldsController,
  yieldsCsv,
  yieldsTsv,
  type Expansion,
  type SearchQuery,
  type YieldsState,
} from "./state";

export interface CompoundsPageDeps {
  doc: Document;
  /** The element the page is built into. */
  root: HTMLElement;
  client: HostClient;
  createEditor: EditorFactory;
  copyText?: (text: string) => Promise<void>;
  /** Saves a text file on this computer (the "Download CSV" button). */
  downloadText?: (fileName: string, text: string, mimeType: string) => void;
  /** Timer for the debounce of the yields filter; returns a cancel function. */
  schedule?: (fn: () => void, ms: number) => () => void;
  /** Blob URLs of the PDF viewer; default `URL.createObjectURL` / `revokeObjectURL` of the page. */
  objectUrls?: ObjectUrls;
  /** The PDF viewer; default `PdfViewer` built on `objectUrls`. */
  createViewer?: (deps: PdfViewerDeps) => PdfViewerLike;
  /** The browser's tabs, for the import, which only ever touches its own tab. Default: none, so the import cannot run. */
  tabs?: ImportTabs;
  /** Clock and timers of the import; default the page's own. */
  importTimers?: ImportTimers;
  /** Reads a file chosen for the import; default `file.arrayBuffer()`. */
  readFile?: (file: File) => Promise<ArrayBuffer>;
  /** This extension's version, shown and compared with the registry program's (default: the version this build was made with). */
  extensionVersion?: string | null;
}

/** What the page needs of the PDF viewer. */
export interface PdfViewerLike {
  open(options: OpenOptions): void;
  close(): void;
  /** The page is left: close and revoke every URL. */
  revokeAll(): void;
}

export interface CompoundsPageHandle {
  controller: CompoundsController;
  names: NameEditor;
  yields: YieldsController;
  files: FilesController;
  routes: RouteController;
  viewer: PdfViewerLike;
  importer: ImportController;
  /** Switches between the compound list (the default), the yields table and the import. */
  showView(view: PageView): void;
  destroy(): void;
}

export type PageView = "compounds" | "yields" | "import";

export const YIELDS_FILTER_DEBOUNCE_MS = 300;
export const YIELDS_CSV_NAME = "yields.csv";
export const NO_YIELDS_TEXT =
  "No yields are stored yet. Open an experiment in the ELN: yields are saved automatically for products that have an ID.";

/** Saves text as a file through a link with `download` (a blob URL of this page; nothing leaves the computer). */
function defaultDownloadText(doc: Document): (fileName: string, text: string, mimeType: string) => void {
  return (fileName, text, mimeType) => {
    const view = doc.defaultView;
    if (!view) throw new Error("no window");
    const url = view.URL.createObjectURL(new view.Blob([text], { type: mimeType }));
    const link = doc.createElement("a");
    link.href = url;
    link.download = fileName;
    link.hidden = true;
    doc.body.append(link);
    link.click();
    link.remove();
    view.setTimeout(() => view.URL.revokeObjectURL(url), 10_000);
  };
}

/** Without a tabs API (tests, another context) the import cannot open anything. */
const NO_TABS: ImportTabs = {
  create: () => Promise.reject(new Error("tabs cannot be opened from this page")),
  update: () => Promise.reject(new Error("tabs cannot be opened from this page")),
  sendMessage: () => Promise.resolve(undefined),
  remove: () => Promise.resolve(),
  onRemoved: () => () => {},
};

function defaultSchedule(fn: () => void, ms: number): () => void {
  const timer = setTimeout(fn, ms);
  return () => clearTimeout(timer);
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** A safe id fragment from a registry ID. */
function idPart(text: string): string {
  return text.replace(/[^A-Za-z0-9_-]/g, "-");
}

/** "Search: text “abc” and a drawn structure (substructure)"; null without a search. */
export function describeQuery(query: SearchQuery): string | null {
  const parts: string[] = [];
  if (query.text !== null) parts.push(`text “${query.text}”`);
  if (query.molfile !== null) {
    parts.push(query.match === "exact" ? "a drawn structure (exact: same constitution)" : "a drawn structure (substructure)");
  }
  if (parts.length === 0) return null;
  return `Search: ${parts.join(" and ")}`;
}

/** The first stored name among the stereo variants. */
function firstName(item: CompoundItem): string | null {
  for (const v of item.variants) if (v.name && v.name.text.trim() !== "") return v.name.text;
  return null;
}

function thumbnail(el: El, svg: unknown, alt: string, cls: string): HTMLElement | null {
  const src = svgDataUrl(svg);
  return src === null ? null : el("img", { class: cls, attrs: { src, alt } });
}

export function mountCompoundsPage(deps: CompoundsPageDeps): CompoundsPageHandle {
  const { doc, root } = deps;
  const el = makeEl(doc);
  const copyText = deps.copyText ?? ((text: string) => navigator.clipboard.writeText(text));

  // ---------------------------------------------------------------- live region
  const statusLine = el("p", { class: "sr-only", attrs: { role: "status", "aria-live": "polite" } });
  const announce = (message: string): void => {
    statusLine.textContent = message;
  };
  const copyCtx: CopyContext = { copyText, announce };

  const extensionVersion = deps.extensionVersion === undefined ? EXTENSION_VERSION : deps.extensionVersion;
  const controller = new CompoundsController(deps.client, { onChange: () => render(), announce }, extensionVersion);
  const names = new NameEditor(controller.client, { onChange: () => render(), announce });
  const files = new FilesController(controller.client, { onChange: () => render(), announce });
  const routes = new RouteController(controller.client, { onChange: () => render(), announce });
  const objectUrls = deps.objectUrls ?? defaultObjectUrls(doc);
  const viewerDeps: PdfViewerDeps = { doc, background: root, urls: objectUrls };
  const viewer: PdfViewerLike = deps.createViewer ? deps.createViewer(viewerDeps) : new PdfViewer(viewerDeps);

  // ---------------------------------------------------------------- header
  const registryArea = el("div", { class: "registry" });
  const header = el(
    "header",
    { class: "page-head" },
    el("h1", { text: "All compounds" }),
    el("p", { class: "small", text: "Compound registry on this computer. Nothing on this page is sent anywhere else." }),
    registryArea,
  );

  // ---------------------------------------------------------------- text search
  const searchInput = el("input", {
    attrs: {
      id: "search-text",
      type: "search",
      maxlength: String(MAX_SEARCH_TEXT),
      autocomplete: "off",
      spellcheck: "false",
      placeholder: "ID, SMILES, InChI, InChIKey, name, formula or ELN number",
      "aria-describedby": "search-hint",
    },
  });
  const searchButton = el("button", { class: "btn primary", attrs: { type: "submit" }, text: "Search" });
  const clearButton = el("button", { class: "btn", attrs: { type: "button" }, text: "Clear" });
  // The example IDs follow the registry's ID format once the ping has answered.
  const searchHintText = (): string => {
    const ids = idExamplesOf(controller.state.ping);
    return (
      `An ID (${ids.core}, ${ids.coreStereo}, ${ids.full}), a SMILES or InChI (same constitution, stereo ignored), ` +
      "an InChIKey, part of a stored name, a formula, or an ELN number."
    );
  };
  const searchHint = el("p", { class: "small", attrs: { id: "search-hint" }, text: searchHintText() });
  const form = el(
    "form",
    { class: "search-form", attrs: { role: "search", "aria-label": "Search compounds" } },
    el("label", { class: "field", attrs: { for: "search-text" }, text: "Search text" }),
    el("div", { class: "search-row" }, searchInput, searchButton, clearButton),
    searchHint,
  );

  // ---------------------------------------------------------------- drawn structure
  const drawId = "draw-area";
  const drawToggle = el("button", {
    class: "disclosure",
    attrs: { type: "button", "aria-expanded": "false", "aria-controls": drawId },
    text: "Draw structure",
  });
  const editorHost = el("div", {
    class: "editor",
    attrs: { role: "group", "aria-label": "Structure editor (drawing with the mouse)" },
  });
  const editorMessage = el("div", { class: "editor-message", attrs: { "aria-live": "polite" } });
  const matchName = "match";
  const radio = (value: MatchMode, label: string, checked: boolean): HTMLElement => {
    const input = el("input", { attrs: { type: "radio", name: matchName, value, id: `match-${value}` } });
    input.checked = checked;
    return el("label", { class: "check", attrs: { for: `match-${value}` } }, input, el("span", { text: label }));
  };
  const drawSearch = el("button", { class: "btn primary", attrs: { type: "button" }, text: "Search" });
  const drawClear = el("button", { class: "btn", attrs: { type: "button" }, text: "Clear drawing" });
  const drawArea = el(
    "div",
    { class: "draw-area", attrs: { id: drawId } },
    el("p", {
      class: "small",
      text:
        "Draw a structure or a part of one. It is searched together with the search text above (both must match). " +
        "The editor needs a mouse; the text search above works with the keyboard alone.",
    }),
    editorHost,
    editorMessage,
    el(
      "fieldset",
      { class: "match" },
      el("legend", { text: "Match" }),
      radio("substructure", "Substructure: compounds that contain the drawing", true),
      radio("exact", "Exact: same constitution as the drawing (stereo ignored)", false),
    ),
    el("div", { class: "row" }, drawSearch, drawClear),
  );
  drawArea.hidden = true;

  const searchSection = el(
    "section",
    { class: "search-section", attrs: { "aria-labelledby": "search-heading" } },
    el("h2", { attrs: { id: "search-heading" }, text: "Search" }),
    form,
    el("div", { class: "draw" }, drawToggle, drawArea),
  );

  // ---------------------------------------------------------------- results
  const orderSelect = el("select", { attrs: { id: "order" } });
  for (const [value, label] of [
    ["asc", "CORE number, ascending"],
    ["desc", "Newest first (CORE number descending)"],
  ] as const) {
    orderSelect.appendChild(el("option", { attrs: { value }, text: label }));
  }
  orderSelect.value = "asc";
  const resultLineEl = el("p", { class: "result-line" });
  const queryLine = el("p", { class: "small query-line" });
  const messages = el("div", { class: "messages" });
  const list = el("ol", { class: "compounds", attrs: { "aria-label": "Compounds" } });
  const pager = el("nav", { class: "pager", attrs: { "aria-label": "Pages" } });
  const resultsSection = el(
    "section",
    { class: "results", attrs: { "aria-labelledby": "results-heading" } },
    el(
      "div",
      { class: "results-head" },
      el("h2", { attrs: { id: "results-heading" }, text: "Compounds" }),
      el("div", { class: "order" }, el("label", { attrs: { for: "order" }, text: "Order" }), orderSelect),
    ),
    resultLineEl,
    queryLine,
    messages,
    list,
    pager,
  );

  // ---------------------------------------------------------------- view switch
  const schedule = deps.schedule ?? defaultSchedule;
  const downloadText = deps.downloadText ?? defaultDownloadText(doc);
  const yields = new YieldsController(controller.client, { onChange: () => render(), announce });
  const compoundsTab = el("button", {
    class: "view-btn",
    key: "view-compounds",
    attrs: { type: "button", "aria-pressed": "true", "aria-controls": "view-compounds" },
    text: "Compounds",
  });
  const yieldsTab = el("button", {
    class: "view-btn",
    key: "view-yields",
    attrs: { type: "button", "aria-pressed": "false", "aria-controls": "view-yields" },
    text: "Yields",
  });
  const importTab = el("button", {
    class: "view-btn",
    key: "view-import",
    attrs: { type: "button", "aria-pressed": "false", "aria-controls": "view-import" },
    text: "Import",
  });
  const viewSwitch = el("nav", { class: "view-switch", attrs: { "aria-label": "View" } }, compoundsTab, yieldsTab, importTab);

  // ---------------------------------------------------------------- import view
  const importer = new ImportController(
    { client: controller.client, tabs: deps.tabs ?? NO_TABS, ...(deps.importTimers ? { timers: deps.importTimers } : {}) },
    { onChange: () => render(), announce },
  );
  const importView = buildImportView({
    doc,
    el,
    controller: importer,
    copyText,
    downloadText: (name, text, type) => downloadText(name, text, type),
    announce,
    namesAvailable: () => nameGeneratorOf(controller.state.ping).available,
    ...(deps.readFile ? { readFile: deps.readFile } : {}),
  });

  // ---------------------------------------------------------------- yields view
  const yieldsFilter = el("input", {
    key: "yields-filter",
    attrs: {
      id: "yields-filter",
      type: "search",
      maxlength: String(MAX_SEARCH_TEXT),
      autocomplete: "off",
      spellcheck: "false",
      placeholder: "ELN number, ID or name",
    },
  });
  const onlyRecorded = el("input", { key: "yields-only", attrs: { id: "yields-only", type: "checkbox" } });
  const copyTable = el("button", { class: "btn", key: "yields-copy", attrs: { type: "button" }, text: "Copy table" });
  const downloadCsv = el("button", { class: "btn", key: "yields-csv", attrs: { type: "button" }, text: "Download CSV" });
  const exportFeedback = el("span", { class: "small export-feedback", attrs: { "aria-live": "polite" } });
  const yieldsCount = el("p", { class: "result-line yields-count" });
  const yieldsMessages = el("div", { class: "messages yields-messages" });
  const yieldsBody = el("tbody");
  const yieldsTable = el(
    "table",
    { class: "yields-table", attrs: { "aria-labelledby": "yields-heading" } },
    el(
      "thead",
      null,
      el(
        "tr",
        null,
        el("th", { attrs: { scope: "col" }, text: "Experiment" }),
        el("th", { class: "num", attrs: { scope: "col" }, text: "Product" }),
        el("th", { attrs: { scope: "col" }, text: "Compound ID" }),
        el("th", { attrs: { scope: "col" }, text: "Name" }),
        el("th", { class: "num", attrs: { scope: "col" }, text: "Isolated yield" }),
        el("th", { class: "num", attrs: { scope: "col" }, text: "Analytical yield" }),
      ),
    ),
    yieldsBody,
  );
  const yieldsMore = el("div", { class: "row yields-more" });
  const yieldsSection = el(
    "section",
    { class: "results yields-view", attrs: { id: "view-yields", "aria-labelledby": "yields-heading" } },
    el("h2", { attrs: { id: "yields-heading" }, text: "Yields" }),
    el("p", {
      class: "small",
      text: "One row per product that has an ID. Yields are read from the ELN and saved automatically while an experiment is open.",
    }),
    el(
      "div",
      { class: "yields-controls" },
      el(
        "div",
        { class: "yields-filter" },
        el("label", { class: "field", attrs: { for: "yields-filter" }, text: "Filter" }),
        yieldsFilter,
      ),
      el("label", { class: "check", attrs: { for: "yields-only" } }, onlyRecorded, el("span", { text: "Only with a yield" })),
    ),
    el("div", { class: "row" }, copyTable, downloadCsv, exportFeedback),
    yieldsCount,
    yieldsMessages,
    el("div", { class: "table-wrap" }, yieldsTable),
    yieldsMore,
  );
  yieldsSection.hidden = true;

  const compoundsView = el("div", { class: "compounds-view", attrs: { id: "view-compounds" } }, searchSection, resultsSection);

  root.replaceChildren(header, viewSwitch, el("main", null, compoundsView, yieldsSection, importView.section), statusLine);

  // ---------------------------------------------------------------- editor
  let editor: StructureEditor | null = null;
  let editorFailed = false;
  function ensureEditor(): void {
    if (editor || editorFailed) return;
    try {
      editor = deps.createEditor(editorHost);
    } catch (error) {
      editorFailed = true;
      editorMessage.replaceChildren(
        el("div", { class: "banner error", attrs: { role: "alert" }, text: `The structure editor could not be started: ${String(error)}` }),
      );
    }
  }
  const drawOpen = (): boolean => !drawArea.hidden;
  drawToggle.addEventListener("click", () => {
    const open = !drawOpen();
    drawArea.hidden = !open;
    drawToggle.setAttribute("aria-expanded", String(open));
    if (open) ensureEditor();
  });

  function currentMatch(): MatchMode {
    const checked = drawArea.querySelector<HTMLInputElement>(`input[name="${matchName}"]:checked`);
    return checked?.value === "exact" ? "exact" : "substructure";
  }

  /** The drawing, when the drawing area is open and something is drawn; "too-large" when it cannot be sent. */
  function drawnMolfile(): string | null | "too-large" {
    if (!drawOpen() || !editor) return null;
    let molfile: string | null;
    try {
      molfile = editor.getMolfile();
    } catch (error) {
      editorMessage.replaceChildren(
        el("div", { class: "banner error", attrs: { role: "alert" }, text: `The drawing could not be read: ${String(error)}` }),
      );
      return null;
    }
    if (molfile !== null && molfile.length > MAX_STRUCTURE_CHARS) return "too-large";
    return molfile;
  }

  function runSearch(requireDrawing: boolean): void {
    editorMessage.replaceChildren();
    const text = searchInput.value.trim();
    const molfile = drawnMolfile();
    if (molfile === "too-large") {
      editorMessage.replaceChildren(el("p", { class: "small problem", attrs: { role: "alert" }, text: "The drawing is too large to search." }));
      return;
    }
    if (requireDrawing && molfile === null) {
      editorMessage.replaceChildren(el("p", { class: "small problem", attrs: { role: "alert" }, text: "Draw a structure first." }));
      return;
    }
    controller.search({ text: text === "" ? null : text, molfile, match: currentMatch() });
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch(false);
  });
  clearButton.addEventListener("click", () => {
    searchInput.value = "";
    runSearch(false);
    searchInput.focus();
  });
  drawSearch.addEventListener("click", () => runSearch(true));
  drawClear.addEventListener("click", () => {
    editor?.clear();
    editorMessage.replaceChildren();
    // A shown result that used the drawing is searched again without it.
    if (controller.state.request.query.molfile !== null) runSearch(false);
  });
  orderSelect.addEventListener("change", () => controller.setOrder(orderSelect.value === "desc" ? "desc" : "asc"));

  // ---------------------------------------------------------------- yields: events
  let view: PageView = "compounds";
  let cancelFilter: (() => void) | null = null;
  let exporting = false;

  function currentYieldsFilter() {
    const text = yieldsFilter.value.trim();
    return { text: text === "" ? null : text, onlyRecorded: onlyRecorded.checked };
  }
  function applyYieldsFilter(): void {
    cancelFilter?.();
    cancelFilter = null;
    const next = currentYieldsFilter();
    const shown = yields.state.filter;
    if (yields.state.total !== null && next.text === shown.text && next.onlyRecorded === shown.onlyRecorded) return;
    yields.load(next);
  }
  yieldsFilter.addEventListener("input", () => {
    cancelFilter?.();
    cancelFilter = schedule(() => {
      cancelFilter = null;
      applyYieldsFilter();
    }, YIELDS_FILTER_DEBOUNCE_MS);
  });
  yieldsFilter.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      applyYieldsFilter();
    }
  });
  onlyRecorded.addEventListener("change", () => {
    cancelFilter?.();
    cancelFilter = null;
    yields.load(currentYieldsFilter());
  });

  function showView(next: PageView): void {
    view = next;
    compoundsView.hidden = next !== "compounds";
    yieldsSection.hidden = next !== "yields";
    importView.section.hidden = next !== "import";
    compoundsTab.setAttribute("aria-pressed", String(next === "compounds"));
    yieldsTab.setAttribute("aria-pressed", String(next === "yields"));
    importTab.setAttribute("aria-pressed", String(next === "import"));
    // Every visit to the table shows what the registry holds now.
    if (next === "yields" && yields.state.loading === null) yields.load(currentYieldsFilter());
    render();
  }
  compoundsTab.addEventListener("click", () => {
    if (view !== "compounds") showView("compounds");
  });
  yieldsTab.addEventListener("click", () => {
    if (view !== "yields") showView("yields");
  });
  // Opening the import changes nothing and opens no tab.
  importTab.addEventListener("click", () => {
    if (view !== "import") showView("import");
  });

  /** A compound ID in the table: show it in the compound list. */
  function showCompound(id: string): void {
    showView("compounds");
    searchInput.value = id;
    controller.search({ text: id, molfile: null, match: currentMatch() });
    searchInput.focus();
  }

  async function exportRows(kind: "copy" | "csv"): Promise<void> {
    if (exporting) return;
    exporting = true;
    exportFeedback.textContent = "Loading every row…";
    render();
    const outcome = await yields.fetchAll();
    exporting = false;
    if (outcome === null) {
      exportFeedback.textContent = "The filter changed meanwhile. Nothing was exported.";
    } else if (!outcome.ok) {
      exportFeedback.textContent = `Nothing was exported: ${outcome.error.message}`;
    } else {
      const count = `${outcome.rows.length} ${outcome.rows.length === 1 ? "row" : "rows"}`;
      if (kind === "copy") {
        try {
          await copyText(yieldsTsv(outcome.rows));
          exportFeedback.textContent = `${count} copied. Paste them into a spreadsheet.`;
        } catch {
          exportFeedback.textContent = "Copying failed. Use Download CSV instead.";
        }
      } else {
        try {
          downloadText(YIELDS_CSV_NAME, BOM + yieldsCsv(outcome.rows), "text/csv;charset=utf-8");
          exportFeedback.textContent = `${count} saved as ${YIELDS_CSV_NAME}.`;
        } catch (error) {
          exportFeedback.textContent = `The file could not be saved: ${String(error)}`;
        }
      }
    }
    announce(exportFeedback.textContent ?? "");
    render();
  }
  copyTable.addEventListener("click", () => void exportRows("copy"));
  downloadCsv.addEventListener("click", () => void exportRows("csv"));

  // ---------------------------------------------------------------- render: header
  function renderRegistry(): void {
    const ping = controller.state.ping;
    const hint = searchHintText();
    if (searchHint.textContent !== hint) searchHint.textContent = hint;
    const children: Child[] = [];
    if (ping.kind === "pending") {
      children.push(el("p", { class: "small busy", text: "Contacting the local registry program…" }));
    } else if (ping.kind === "ok") {
      const stats = ping.result.stats ?? {};
      const counts: string[] = [];
      if (typeof stats.cores === "number") counts.push(plural(stats.cores, "compound (CORE)", "compounds (CORE)"));
      if (typeof stats.stereos === "number") counts.push(plural(stats.stereos, "stereo variant", "stereo variants"));
      if (typeof stats.batches === "number") {
        counts.push(
          plural(stats.batches, "batch", "batches") + (typeof stats.retired === "number" && stats.retired > 0 ? ` (${stats.retired} retired)` : ""),
        );
      }
      children.push(el("p", { class: "small" }, "Registry database: ", el("span", { class: "mono", text: ping.dbPath })));
      const filesFolder = readFilesFolder(ping.result);
      if (filesFolder !== null) {
        children.push(
          el(
            "p",
            { class: "small files-folder" },
            "Compound folders (spectra and files): ",
            el("span", { class: "mono", text: filesFolder }),
            " ",
            copyButton(el, copyCtx, filesFolder, "the folder of the compound folders", "copy-files-folder"),
          ),
        );
      }
      if (counts.length > 0) children.push(el("p", { class: "small", text: counts.join(" · ") }));
      if (ping.nonDefaultHome !== null) {
        children.push(el("p", { class: "banner info compact", attrs: { role: "note" }, text: `Registry: ${ping.nonDefaultHome} (not the default)` }));
      }
      if (ping.warnings.length > 0) {
        children.push(
          el(
            "div",
            { class: "banner warn", attrs: { role: "note" } },
            el("strong", { text: "Registry warnings" }),
            el("ul", null, ...ping.warnings.map((w) => el("li", { text: w }))),
          ),
        );
      }
      if (ping.versionNotice !== null) {
        children.push(
          el(
            "div",
            { class: "banner warn version-notice", attrs: { role: "note" } },
            el("strong", { text: "Extension and registry program are from different releases" }),
            el("p", { text: ping.versionNotice }),
          ),
        );
      }
    } else if (ping.kind === "mismatch") {
      children.push(
        el(
          "div",
          { class: "banner error", attrs: { role: "alert" } },
          el("strong", { text: "Extension and registry program do not match" }),
          el("p", { text: ping.message }),
          ping.hostVersion ? el("p", { class: "code", text: `Host version ${ping.hostVersion}` }) : null,
        ),
      );
    } else if (ping.kind === "error") {
      children.push(renderErrorBox(el, ping.error, "ping"));
    }
    if (ping.kind === "error" || ping.kind === "mismatch") {
      const retry = el("button", { class: "btn", key: "retry-ping", attrs: { type: "button" }, text: "Retry" });
      retry.addEventListener("click", () => controller.retry());
      children.push(el("div", { class: "row" }, retry));
    }
    const versions = versionLine(extensionVersion, hostVersionOf(ping));
    if (versions !== null) children.push(el("p", { class: "small versions", text: versions }));
    registryArea.replaceChildren(...children.filter((c): c is HTMLElement => c instanceof HTMLElement));
  }

  // ---------------------------------------------------------------- render: one entry
  function variantBlock(item: CompoundItem, variant: FoundVariant, searched: boolean): HTMLElement {
    const id = variant.core_stereo_id;
    const listed = item.variants.find((v) => v.core_stereo_id === id);
    const matched = searched && listed?.matched === true;
    const nameRow =
      variant.name === undefined
        ? null
        : renderNameRow(el, copyCtx, names, {
            target: id,
            name: variant.name ?? null,
            save: (target) => {
              void names.save(target).then((result) => {
                if (result) controller.nameStored(result);
              });
            },
            remove: (target) => {
              void names.remove(target).then((result) => {
                if (result) controller.nameStored(result);
              });
            },
            generator: nameGeneratorOf(controller.state.ping),
            generate: (target, replace) => {
              const generator = nameGeneratorOf(controller.state.ping);
              if (!generator.available) return;
              void names.generate(target, replace, { label: generatorLabel(generator) }).then((result) => {
                if (result) controller.nameStored(result);
              });
            },
          });
    const batches = variant.batches ?? [];
    const headingId = `variant-${idPart(id)}`;
    return el(
      "section",
      { class: matched ? "variant-block matched" : "variant-block", attrs: { "aria-labelledby": headingId } },
      el(
        "div",
        { class: "variant-head" },
        thumbnail(el, variant.svg, `Structure of ${id}`, "thumb"),
        el(
          "div",
          null,
          el("h4", { class: "mono", attrs: { id: headingId }, text: id }),
          el("p", { class: "small", text: `${stereoModeLabel(variant.stereo_mode)} · ${plural(variant.batch_count, "batch", "batches")}` }),
          matched ? el("p", { class: "match-mark", text: "Matches the search" }) : null,
        ),
      ),
      renderDetailsList(el, copyCtx, factsFromVariant(variant, item.formula), {
        nameRow,
        keyPrefix: `${id}:`,
        subject: id,
      }),
      batches.length > 0
        ? el(
            "div",
            null,
            el("h5", { text: "Batches" }),
            el(
              "ul",
              { class: "list batches" },
              ...batches.map((b) => {
                // No yield: nothing on the line (a plain list, not a table).
                const yieldText = batchYieldText(b.yield);
                return el(
                  "li",
                  { class: b.retired ? "batch retired-batch" : "batch", attrs: { "data-id": b.full_id } },
                  el("span", { class: "mono", text: b.full_id }),
                  " · ",
                  experimentAnchor(el, experimentText(b.experiment_ref, b.experiment_code), b.experiment_url),
                  ` · product ${b.product_index} · ${datePart(b.created_at)}`,
                  yieldText ? " · yield " : null,
                  yieldText ? el("span", { class: "batch-yield", text: yieldText }) : null,
                  b.retired ? el("span", { class: "retired", text: ` · retired${b.retired_reason ? `: ${b.retired_reason}` : ""}` }) : null,
                );
              }),
            ),
          )
        : el("p", { class: "small", text: "No batch is recorded." }),
      filesSectionNode(id),
      routeSectionNode(id),
    );
  }

  // ---------------------------------------------------------------- synthesis route
  /** One "Synthesis route" section per stereo variant shown; its node is kept, its content rebuilt when its state changes. */
  interface RouteSection {
    node: HTMLElement;
    /** The state the content was built from (null: never built). */
    shown: RouteEntry | undefined | null;
    /** `routes.stamp(id)` when it was built. */
    stamp: number;
  }
  const routeSections = new Map<string, RouteSection>();

  function routeSectionNode(id: string): HTMLElement {
    let section = routeSections.get(id);
    if (!section) {
      const node = el("section", {
        class: "route-section",
        attrs: { "aria-labelledby": `route-${idPart(id)}`, "data-core-stereo": id },
      });
      section = { node, shown: null, stamp: -1 };
      routeSections.set(id, section);
    }
    return section.node;
  }

  function routeContent(id: string, entry: RouteEntry | undefined): Child[] {
    const running = entry?.running === true;
    const generate = el("button", {
      class: "btn small-btn route-generate",
      key: `route-generate:${id}`,
      attrs: { type: "button", "aria-label": `${ROUTE_BUTTON} for ${id}`, title: ROUTE_HINT },
      text: ROUTE_BUTTON,
    });
    generate.disabled = running;
    generate.addEventListener("click", () => routes.generate(id));
    const out: Child[] = [
      el("div", { class: "route-head" }, el("h5", { attrs: { id: `route-${idPart(id)}` }, text: ROUTE_HEADING }), generate),
    ];
    const notice = routes.notice(id);
    if (notice?.saved) out.push(el("p", { class: "small route-sm-saved", attrs: { role: "status" }, text: SM_SAVED }));
    if (notice && notice.warnings.length > 0) {
      out.push(
        el(
          "div",
          { class: "banner warn route-sm-warnings", attrs: { role: "note" } },
          el("strong", { text: "Registry warnings" }),
          el("ul", null, ...notice.warnings.map((w) => el("li", { text: w }))),
        ),
      );
    }
    if (running) out.push(el("p", { class: "small busy route-running", text: ROUTE_RUNNING }));
    if (entry?.error) out.push(renderErrorBox(el, entry.error, "route"));
    const result = entry?.result ?? null;
    if (!result) return out;
    if (result.warnings.length > 0) {
      out.push(
        el(
          "div",
          { class: "banner warn", attrs: { role: "note" } },
          el("strong", { text: "Registry warnings" }),
          el("ul", null, ...result.warnings.map((w) => el("li", { text: w }))),
        ),
      );
    }
    if (result.steps === 0) {
      out.push(el("p", { class: "empty-note route-nothing", text: NOTHING_TO_DRAW }));
    } else {
      const opened = openedLine(result);
      if (opened) out.push(el("p", { class: "route-opened", text: opened }));
      if (result.file) {
        const folder = folderOf(result.file.path);
        out.push(
          el(
            "p",
            { class: "small folder-line route-folder" },
            "Folder: ",
            el("span", { class: "mono folder-path", text: folder }),
            " ",
            copyButton(el, copyCtx, folder, `the folder of ${id}`, `copy-route-folder:${id}`),
          ),
        );
      }
      out.push(el("p", { class: "small route-steps", text: stepsLine(result) }));
      const cdxml = result.cdxml;
      if (cdxml !== null) {
        const save = el("button", {
          class: "btn small-btn route-save",
          key: `route-save:${id}`,
          attrs: { type: "button", "aria-label": `Save a copy of the route of ${id}` },
          text: "Save a copy",
        });
        const name = copyFileName(result);
        save.addEventListener("click", () => {
          try {
            downloadText(name, cdxml, CDXML_TYPE);
            announce(`${name} saved.`);
          } catch (error) {
            announce(`The copy could not be saved: ${String(error)}`);
          }
        });
        out.push(el("div", { class: "row" }, save));
      }
    }
    if (result.missing.length > 0) {
      out.push(
        el("h6", { class: "route-missing-head", text: MISSING_HEADING }),
        el(
          "ul",
          { class: "list route-missing" },
          ...result.missing.map((m) =>
            el(
              "li",
              { attrs: { "data-ref": m.experiment_ref } },
              experimentAnchor(el, missingExperimentText(m), m.experiment_url),
              missingTail(m),
            ),
          ),
        ),
      );
    }
    if (result.notes.length > 0) {
      out.push(el("ul", { class: "list route-notes" }, ...result.notes.map((n) => el("li", { class: "small", text: n }))));
    }
    const sm = limitingBlock(id, result.choices, notice?.error ?? null);
    if (sm) out.push(sm);
    return out;
  }

  /**
   * "Starting material (SM)": a choice per run where two or more reactants
   * have 1 eq; the other runs behind "Change for other experiments". Null
   * when the answer lists no run.
   */
  function limitingBlock(id: string, choices: readonly RouteChoice[], error: HostError | null): HTMLElement | null {
    if (choices.length === 0) return null;
    const part = idPart(id);
    const line = (choice: RouteChoice, index: number): HTMLElement => {
      const controlId = `sm-${part}-${index}`;
      const key = `route-sm:${id}:${index}:${choice.experiment_ref}`;
      const selection = routes.selection(choice);
      const select = el("select", { class: "route-sm-select", key, attrs: { id: controlId } });
      const option = (value: string, label: string): HTMLOptionElement => el("option", { attrs: { value }, text: label });
      select.append(option(SM_AUTO, autoOptionLabel(choice)), ...choice.candidates.map((c) => option(String(c.row), candidateLabel(c))));
      const wanted = selectionValue(selection);
      select.value = Array.from(select.options).some((o) => o.value === wanted) ? wanted : SM_AUTO;
      const before = select.value;
      select.disabled = routes.isSaving(choice.experiment_ref);
      select.addEventListener("change", () => {
        const row = valueSelection(choice, select.value);
        if (row === undefined || select.value === before) {
          select.value = before;
          return;
        }
        routes.setLimiting(id, choice, row, key);
      });
      return el(
        "li",
        { class: "route-sm-line", attrs: { "data-ref": choice.experiment_ref } },
        el("label", { attrs: { for: controlId }, text: choiceLabel(choice) }),
        select,
      );
    };
    const ambiguous: HTMLElement[] = [];
    const others: HTMLElement[] = [];
    choices.forEach((choice, index) => (choice.ambiguous ? ambiguous : others).push(line(choice, index)));
    const block = el("div", { class: "route-sm" }, el("h6", { class: "route-sm-head", text: SM_HEADING }));
    if (error) block.append(renderErrorBox(el, error, "set_limiting"));
    if (ambiguous.length > 0) {
      block.append(
        el("p", { class: "small route-sm-hint", text: SM_AMBIGUOUS_HINT }),
        el("ul", { class: "list route-sm-list route-sm-ambiguous" }, ...ambiguous),
      );
    }
    if (others.length > 0) {
      const open = routes.othersShown(id);
      const listId = `sm-others-${part}`;
      const toggle = el("button", {
        class: "disclosure route-sm-toggle",
        key: `route-sm-others:${id}`,
        attrs: { type: "button", "aria-expanded": String(open), "aria-controls": listId },
        text: SM_OTHERS_TOGGLE,
      });
      toggle.addEventListener("click", () => routes.toggleOthers(id));
      const list = el("ul", { class: "list route-sm-list route-sm-others", attrs: { id: listId } }, ...others);
      list.hidden = !open;
      block.append(toggle, list);
    }
    return block;
  }

  /** Drops what belongs to variants no longer shown and rebuilds changed sections. */
  function renderRouteSections(shownIds: ReadonlySet<string>): void {
    for (const id of [...routeSections.keys()]) if (!shownIds.has(id)) routeSections.delete(id);
    routes.retain(shownIds);
    for (const [id, section] of routeSections) {
      const entry = routes.get(id);
      const stamp = routes.stamp(id);
      if (section.shown === entry && section.stamp === stamp) continue;
      section.shown = entry;
      section.stamp = stamp;
      const children = routeContent(id, entry).filter((c): c is HTMLElement => c instanceof HTMLElement);
      section.node.replaceChildren(...children);
    }
  }

  // ---------------------------------------------------------------- spectra and files
  /**
   * One "Spectra and files" section per stereo variant shown. The node stays
   * the same while the variant is shown (also when its entry is rebuilt); its
   * content is rebuilt when the variant's `compound_files` state changes. The
   * "Show PDF" controls inside are updated in place (progress, errors).
   */
  interface FilesSection {
    node: HTMLElement;
    /** The state the content was built from (null: never built). */
    shown: FilesEntry | undefined | null;
  }
  const filesSections = new Map<string, FilesSection>();
  /** Parameter tables the user opened (by `id|full_id|spectrum_key`), kept open across rebuilds. */
  const openParameters = new Set<string>();
  /** Variants whose files are loaded after the current render. */
  let filesToLoad: string[] = [];

  interface PdfControl {
    key: string;
    coreStereoId: string;
    update(): void;
    focusButton(): void;
  }
  const pdfControls = new Map<string, PdfControl>();
  let pdfTask: { key: string; coreStereoId: string; load: PdfLoad; received: number; size: number | null } | null = null;
  let pdfError: { key: string; message: string } | null = null;

  function findByKey(key: string): HTMLElement | null {
    for (const node of Array.from(root.querySelectorAll<HTMLElement>("[data-key]"))) if (node.dataset.key === key) return node;
    return null;
  }

  function filesSectionNode(id: string): HTMLElement {
    let section = filesSections.get(id);
    if (!section) {
      const node = el("section", {
        class: "files-section",
        attrs: { "aria-labelledby": `files-${idPart(id)}`, "data-core-stereo": id },
      });
      section = { node, shown: null };
      filesSections.set(id, section);
    }
    return section.node;
  }

  function updatePdfControls(): void {
    for (const control of pdfControls.values()) control.update();
  }

  function pdfControl(key: string, coreStereoId: string, source: PdfSource, what: string): HTMLElement {
    const show = el("button", {
      class: "btn small-btn pdf-show",
      key: `pdf-show:${key}`,
      attrs: { type: "button", "aria-label": `Show PDF ${what}` },
      text: "Show PDF",
    });
    const progress = el("span", { class: "small busy pdf-progress" });
    const cancel = el("button", {
      class: "btn small-btn pdf-cancel",
      key: `pdf-cancel:${key}`,
      attrs: { type: "button", "aria-label": "Cancel loading the PDF" },
      text: "Cancel",
    });
    const error = el("p", { class: "small problem pdf-error", attrs: { role: "alert" } });
    const control: PdfControl = {
      key,
      coreStereoId,
      update: () => {
        const loading = pdfTask?.key === key;
        show.disabled = loading;
        progress.hidden = !loading;
        progress.textContent = loading ? progressText(pdfTask!.received, pdfTask!.size) : "";
        cancel.hidden = !loading;
        const message = pdfError?.key === key ? pdfError.message : "";
        if (error.textContent !== message) error.textContent = message;
        error.hidden = message === "";
      },
      focusButton: () => {
        const current = findByKey(`pdf-show:${key}`);
        current?.focus();
      },
    };
    show.addEventListener("click", () => startPdf(key, coreStereoId, source));
    cancel.addEventListener("click", () => cancelPdf(key));
    pdfControls.set(key, control);
    control.update();
    return el("div", { class: "pdf-control" }, el("div", { class: "row inline" }, show, progress, cancel), error);
  }

  function startPdf(key: string, coreStereoId: string, source: PdfSource): void {
    pdfTask?.load.cancel(); // one PDF at a time
    pdfError = null;
    const task: NonNullable<typeof pdfTask> = { key, coreStereoId, load: null as unknown as PdfLoad, received: 0, size: null };
    pdfTask = task;
    task.load = loadPdf(controller.client, source, (received, size) => {
      if (pdfTask !== task) return;
      task.received = received;
      task.size = size;
      updatePdfControls();
    });
    updatePdfControls();
    findByKey(`pdf-cancel:${key}`)?.focus();
    announce("Loading the PDF…");
    void task.load.done.then((outcome) => {
      if (pdfTask !== task) return; // cancelled, or another PDF was asked for
      pdfTask = null;
      if (outcome.status === "ok") {
        updatePdfControls();
        try {
          viewer.open({
            name: outcome.name,
            bytes: outcome.bytes,
            returnFocus: () => findByKey(`pdf-show:${key}`),
          });
          announce(`${outcome.name} is shown.`);
          return;
        } catch (error) {
          pdfError = { key, message: `The PDF could not be shown: ${String(error)}` };
        }
      } else if (outcome.status === "error") {
        pdfError = { key, message: `The PDF could not be loaded: ${outcome.error.message}` };
      }
      updatePdfControls();
      pdfControls.get(key)?.focusButton();
      if (pdfError?.key === key) announce(pdfError.message);
    });
  }

  function cancelPdf(key: string): void {
    if (pdfTask?.key !== key) return;
    pdfTask.load.cancel();
    pdfTask = null;
    updatePdfControls();
    pdfControls.get(key)?.focusButton();
    announce("Loading the PDF was cancelled.");
  }

  function spectrumBlock(id: string, s: SpectrumInfo): HTMLElement {
    const blockKey = `${id}|${s.full_id}|${s.spectrum_key}`;
    const of: Child[] = [el("span", { class: "mono", text: s.full_id })];
    const code = s.experiment_code?.trim() ?? "";
    if (code !== "") of.push(` · ${code}`);
    if (s.updated_at) of.push(` · recorded ${shortDate(s.updated_at)}`);
    const method = [
      s.experiment?.trim() ? `Experiment: ${s.experiment.trim()}` : null,
      s.pulse_sequence?.trim() ? `Pulse sequence: ${s.pulse_sequence.trim()}` : null,
    ].filter((x): x is string => x !== null);
    const heading = spectrumHeading(s);
    const title = s.title?.trim() ?? "";

    let parameters: HTMLElement | null = null;
    if (s.parameters.length > 0) {
      const details = el(
        "details",
        { class: "spectrum-params" },
        el("summary", { text: `Parameters (${s.parameters.length})` }),
        el(
          "div",
          { class: "table-wrap" },
          el(
            "table",
            { class: "params-table" },
            el("thead", null, el("tr", null, el("th", { attrs: { scope: "col" }, text: "Name" }), el("th", { attrs: { scope: "col" }, text: "Value" }))),
            el("tbody", null, ...s.parameters.map((p) => el("tr", null, el("td", { text: p.name }), el("td", { text: p.value })))),
          ),
        ),
      );
      details.open = openParameters.has(blockKey);
      details.addEventListener("toggle", () => {
        if (details.open) openParameters.add(blockKey);
        else openParameters.delete(blockKey);
      });
      parameters = details;
    }

    const paths: Child[] = [];
    if (s.mnova_file) paths.push(el("dt", { text: "MestReNova file" }), el("dd", { class: "mono", text: s.mnova_file }));
    if (s.pdf_file) paths.push(el("dt", { text: "PDF file" }), el("dd", { class: "mono", text: s.pdf_file }));

    let pdf: Child = null;
    if (s.pdf_available) {
      pdf = pdfControl(
        `${id}:spectrum:${s.full_id}:${s.spectrum_key}`,
        id,
        { kind: "spectrum", coreStereoId: id, fullId: s.full_id, spectrumKey: s.spectrum_key },
        `of ${heading} (${s.full_id})`,
      );
    } else if (s.pdf_file) {
      pdf = el("p", { class: "small pdf-missing", text: `The PDF is not at ${s.pdf_file} any more.` });
    }

    return el(
      "li",
      { class: "spectrum", attrs: { "data-full-id": s.full_id, "data-spectrum-key": s.spectrum_key } },
      el("p", { class: "spectrum-title", text: heading }),
      title !== "" && title !== heading ? el("p", { class: "small spectrum-name", text: title }) : null,
      el("p", { class: "small spectrum-of" }, ...of),
      s.sample?.trim() ? el("p", { class: "small spectrum-sample", text: `Sample: ${s.sample.trim()}` }) : null,
      s.comment?.trim() ? el("p", { class: "small spectrum-comment", text: `Comment: ${s.comment.trim()}` }) : null,
      method.length > 0 ? el("p", { class: "small spectrum-method", text: method.join(" · ") }) : null,
      s.report?.trim()
        ? el(
            "div",
            { class: "spectrum-report" },
            el(
              "div",
              { class: "row inline" },
              el("span", { class: "small", text: "Multiplet report" }),
              copyButton(el, copyCtx, s.report, `the multiplet report of ${s.full_id}`, `copy-report:${blockKey}`),
            ),
            el("p", { class: "report-text", attrs: { tabindex: "0" }, text: s.report }),
          )
        : null,
      parameters,
      paths.length > 0 ? el("dl", { class: "spectrum-files" }, ...paths) : null,
      pdf,
    );
  }

  function folderFileItem(id: string, file: FolderFile): HTMLElement {
    return el(
      "li",
      { class: "folder-file", attrs: { "data-name": file.name } },
      el("span", { class: "file-line", text: folderFileLine(file) }),
      file.is_pdf ? pdfControl(`${id}:file:${file.name}`, id, { kind: "file", coreStereoId: id, name: file.name }, file.name) : null,
    );
  }

  function filesContent(id: string, entry: FilesEntry | undefined): Child[] {
    const headingId = `files-${idPart(id)}`;
    const refresh = el("button", {
      class: "btn small-btn files-refresh",
      key: `files-refresh:${id}`,
      attrs: { type: "button", "aria-label": `Refresh the spectra and files of ${id}` },
      text: "Refresh",
    });
    refresh.addEventListener("click", () => files.load([id]));
    const out: Child[] = [
      el("div", { class: "files-head" }, el("h5", { attrs: { id: headingId }, text: "Spectra and files" }), refresh),
    ];
    if (!entry || entry.loading) out.push(el("p", { class: "small busy", text: "Loading the spectra and files…" }));
    if (entry?.error) out.push(renderErrorBox(el, entry.error, "compound_files"));
    const result: CompoundFilesResult | null = entry?.result ?? null;
    if (!result) return out;
    if (result.warnings.length > 0) {
      out.push(
        el(
          "div",
          { class: "banner warn", attrs: { role: "note" } },
          el("strong", { text: "Registry warnings" }),
          el("ul", null, ...result.warnings.map((w) => el("li", { text: w }))),
        ),
      );
    }
    if (result.folder) {
      out.push(
        el(
          "p",
          { class: "small folder-line" },
          "Folder: ",
          el("span", { class: "mono folder-path", text: result.folder.path }),
          " ",
          copyButton(el, copyCtx, result.folder.path, `the folder of ${id}`, `copy-folder:${id}`),
        ),
      );
      if (!result.folder.exists) out.push(el("p", { class: "small folder-missing", text: FOLDER_MISSING_TEXT }));
    }
    if (result.spectra.length > 0) {
      out.push(el("ul", { class: "spectra", attrs: { "aria-label": `Spectra of ${id}` } }, ...result.spectra.map((s) => spectrumBlock(id, s))));
    } else {
      out.push(el("p", { class: "empty-note files-empty", text: NO_SPECTRA_TEXT }));
    }
    const others = otherFiles(result);
    if (others.length > 0) {
      out.push(
        el("h6", { class: "other-files-head", text: "Other files in the folder" }),
        el("ul", { class: "list folder-files" }, ...others.map((f) => folderFileItem(id, f))),
      );
    }
    if (result.files_truncated) {
      out.push(
        el("p", {
          class: "small files-truncated",
          text: `The folder holds more files: only the first ${result.files.length} are listed.`,
        }),
      );
    }
    return out;
  }

  /** Drops what belongs to variants no longer shown, rebuilds changed sections, and notes what to load. */
  function renderFilesSections(shownIds: ReadonlySet<string>): void {
    for (const id of [...filesSections.keys()]) if (!shownIds.has(id)) filesSections.delete(id);
    files.retain(shownIds);
    if (pdfTask && !shownIds.has(pdfTask.coreStereoId)) {
      pdfTask.load.cancel();
      pdfTask = null;
    }
    for (const [key, control] of [...pdfControls]) if (!shownIds.has(control.coreStereoId)) pdfControls.delete(key);
    for (const [id, section] of filesSections) {
      const entry = files.get(id);
      if (entry === undefined) filesToLoad.push(id);
      if (section.shown === entry) continue;
      section.shown = entry;
      for (const [key, control] of [...pdfControls]) if (control.coreStereoId === id) pdfControls.delete(key);
      const children = filesContent(id, entry).filter((c): c is HTMLElement => c instanceof HTMLElement);
      section.node.replaceChildren(...children);
    }
  }

  function detailsArea(item: CompoundItem, expansion: Expansion, searched: boolean): Child[] {
    if (expansion.status === "loading") return [el("p", { class: "small busy", text: "Loading the stereo variants…" })];
    if (expansion.status === "error") {
      const again = el("button", { class: "btn small-btn", key: `details-retry:${item.core_id}`, attrs: { type: "button" }, text: "Try again" });
      again.addEventListener("click", () => void controller.loadDetails(item.core_id));
      return [renderErrorBox(el, expansion.error, "find"), el("div", { class: "row" }, again)];
    }
    const result: FindResult = expansion.result;
    return [
      result.warnings.length > 0
        ? el(
            "div",
            { class: "banner warn", attrs: { role: "note" } },
            el("strong", { text: "Registry warnings" }),
            el("ul", null, ...result.warnings.map((w) => el("li", { text: w }))),
          )
        : null,
      ...result.variants.map((v) => variantBlock(item, v, searched)),
    ];
  }

  function renderEntry(item: CompoundItem, expansion: Expansion | undefined, searched: boolean): HTMLElement {
    const coreId = item.core_id;
    const headingId = `core-${idPart(coreId)}`;
    const regionId = `variants-${idPart(coreId)}`;
    const name = firstName(item);
    const formula = formulaText(item.properties?.sum_formula ?? null, item.formula);
    const facts = [formula, item.properties ? formatMolarMass(item.properties.mol_weight) : null].filter((x): x is string => !!x);
    const counts = [
      plural(item.variants.length, "stereo variant", "stereo variants"),
      plural(item.batch_count, "batch", "batches"),
      plural(item.experiment_count, "experiment", "experiments"),
    ].join(" · ");
    const experiments: Child[] = [];
    item.experiments.forEach((e, i) => {
      if (i > 0) experiments.push(", ");
      experiments.push(experimentAnchor(el, experimentText(e.experiment_ref, e.experiment_code), e.experiment_url));
    });
    const more = item.experiment_count - item.experiments.length;
    if (more > 0) experiments.push(` +${more} more`);
    const matched = searched ? matchedByText(item.matched_by) : null;

    const open = expansion !== undefined;
    const toggle = el("button", {
      class: "disclosure",
      key: `toggle:${coreId}`,
      attrs: { type: "button", "aria-expanded": String(open), "aria-controls": regionId },
      text: open ? "Hide stereo variants and details" : "Show stereo variants and details",
    });
    toggle.addEventListener("click", () => controller.toggle(coreId));

    return el(
      "li",
      { class: "compound" },
      el(
        "article",
        { attrs: { "aria-labelledby": headingId } },
        el(
          "div",
          { class: "compound-head" },
          thumbnail(el, item.svg, `Structure of ${coreId}`, "thumb"),
          el(
            "div",
            { class: "compound-main" },
            el("h3", { attrs: { id: headingId } }, el("span", { class: "mono", text: coreId })),
            name ? el("p", { class: "compound-name", text: name }) : el("p", { class: "small", text: "No name stored" }),
            facts.length > 0 ? el("p", { text: facts.join(" · ") }) : null,
            el("p", { class: "small", text: counts }),
            experiments.length > 0 ? el("p", { class: "small experiments" }, "Made in: ", ...experiments) : null,
            matched ? el("p", { class: "match-mark", text: matched }) : null,
            el("div", { class: "row" }, copyButton(el, copyCtx, coreId, `the ID ${coreId}`, `copy-core:${coreId}`)),
          ),
        ),
        toggle,
        open ? el("div", { class: "variants", attrs: { id: regionId } }, ...detailsArea(item, expansion, searched)) : null,
      ),
    );
  }

  // ---------------------------------------------------------------- render: results
  /** Entries are rebuilt only when their item, expansion or name-editing state changed. */
  const cache = new Map<string, { item: CompoundItem; expansion: Expansion | undefined; searched: boolean; nameSig: string; node: HTMLElement }>();

  function nameSignature(item: CompoundItem, expansion: Expansion | undefined): string {
    if (expansion?.status !== "ok") return "";
    const s = names.state;
    // Whether (and as what) ChemDraw is offered changes the name rows too.
    const generator = JSON.stringify(nameGeneratorOf(controller.state.ping));
    // While any name request runs, every row's buttons are disabled (one request at a time).
    const busy = s.pending !== null;
    const ids = expansion.result.variants.map((v) => v.core_stereo_id);
    const touches = (target: string | null | undefined) => target != null && ids.includes(target);
    if (
      !touches(s.editing) &&
      !touches(s.pending?.target) &&
      !touches(s.problem?.target) &&
      !touches(s.warnings?.target) &&
      !touches(s.notice?.target)
    ) {
      return `${generator}|${busy}`;
    }
    return JSON.stringify([generator, s.editing, s.pending, s.problem, s.warnings, s.notice, item.core_id]);
  }

  let lastResultLine = "";

  function renderResults(): void {
    const s = controller.state;
    const searched = s.shown ? isSearch(s.shown.query) : false;
    orderSelect.value = s.request.order;
    list.setAttribute("aria-busy", String(s.loading));

    let line = "";
    if (s.loading && !s.result) line = "Loading compounds…";
    else if (s.loading) line = "Loading…";
    else if (s.result) line = resultLine(s.result, searched);
    else if (s.error) line = "The compounds could not be loaded.";
    if (line !== lastResultLine) {
      resultLineEl.textContent = line;
      lastResultLine = line;
    }
    queryLine.textContent = (s.shown && describeQuery(s.shown.query)) ?? "";
    queryLine.hidden = queryLine.textContent === "";

    const msg: Child[] = [];
    if (s.registryChanged) {
      msg.push(
        el("div", {
          class: "banner info",
          attrs: { role: "note" },
          text: "The registry configuration changed: the list was reloaded from the registry now in use.",
        }),
      );
    }
    if (s.error) {
      const ping = s.ping;
      const sameAsPing = ping.kind === "error" && ping.error.code === s.error.code;
      if (!sameAsPing) msg.push(renderErrorBox(el, s.error, "compounds"));
      if (!(ping.kind === "error" || ping.kind === "mismatch")) {
        const retry = el("button", { class: "btn", key: "retry-list", attrs: { type: "button" }, text: "Retry" });
        retry.addEventListener("click", () => controller.retry());
        msg.push(el("div", { class: "row" }, retry));
      }
    }
    if (s.result) {
      if (s.result.notes.length > 0) {
        msg.push(el("div", { class: "banner info", attrs: { role: "note" } }, el("ul", null, ...s.result.notes.map((n) => el("li", { text: n })))));
      }
      if (s.result.warnings.length > 0) {
        msg.push(
          el(
            "div",
            { class: "banner warn", attrs: { role: "note" } },
            el("strong", { text: "Registry warnings" }),
            el("ul", null, ...s.result.warnings.map((w) => el("li", { text: w }))),
          ),
        );
      }
    }
    messages.replaceChildren(...msg.filter((c): c is HTMLElement => c instanceof HTMLElement));

    const items = s.result?.items ?? [];
    const nodes: HTMLElement[] = [];
    const seen = new Set<string>();
    for (const item of items) {
      seen.add(item.core_id);
      const expansion = s.expanded.get(item.core_id);
      const nameSig = nameSignature(item, expansion);
      const cached = cache.get(item.core_id);
      if (cached && cached.item === item && cached.expansion === expansion && cached.searched === searched && cached.nameSig === nameSig) {
        nodes.push(cached.node);
        continue;
      }
      const node = renderEntry(item, expansion, searched);
      cache.set(item.core_id, { item, expansion, searched, nameSig, node });
      nodes.push(node);
    }
    for (const key of [...cache.keys()]) if (!seen.has(key)) cache.delete(key);
    const current = Array.from(list.children);
    if (current.length !== nodes.length || current.some((n, i) => n !== nodes[i])) list.replaceChildren(...nodes);

    const shownIds = new Set<string>();
    for (const item of items) {
      const expansion = s.expanded.get(item.core_id);
      if (expansion?.status === "ok") for (const v of expansion.result.variants) shownIds.add(v.core_stereo_id);
    }
    renderFilesSections(shownIds);
    renderRouteSections(shownIds);

    pager.replaceChildren();
    if (s.result && s.result.total > s.result.limit) {
      const { page, pages } = pageInfo(s.result);
      const limit = s.result.limit;
      const offset = s.result.offset;
      const prev = el("button", { class: "btn", key: "page-prev", attrs: { type: "button" }, text: "Previous page" });
      prev.disabled = s.loading || offset <= 0;
      prev.addEventListener("click", () => controller.goToOffset(offset - limit));
      const next = el("button", { class: "btn", key: "page-next", attrs: { type: "button" }, text: "Next page" });
      next.disabled = s.loading || offset + limit >= s.result.total;
      next.addEventListener("click", () => controller.goToOffset(offset + limit));
      pager.append(prev, el("span", { class: "page-info", text: `Page ${page} of ${pages}` }), next);
    }
  }

  // ---------------------------------------------------------------- render: yields
  const DASH = "–";

  function yieldCell(value: number | null | undefined): HTMLElement {
    return value === null || value === undefined
      ? el("td", { class: "num no-yield", attrs: { "aria-label": "none" }, text: DASH })
      : el("td", { class: "num", text: `${formatPercent(value)} %` });
  }

  function yieldRowElement(row: YieldRow): HTMLElement {
    const idButton = el("button", {
      class: "link-btn mono",
      key: `yield-id:${row.full_id}`,
      attrs: { type: "button", title: `Show ${row.full_id} in the compound list` },
      text: row.full_id,
    });
    idButton.addEventListener("click", () => showCompound(row.full_id));
    return el(
      "tr",
      { class: row.retired ? "yield-row retired-batch" : "yield-row", attrs: { "data-id": row.full_id } },
      el("td", { class: "exp" }, experimentAnchor(el, yieldExperimentText(row), row.experiment_url)),
      el("td", { class: "num", text: String(row.product_index) }),
      el("td", { class: "id" }, idButton, row.retired ? el("span", { class: "retired", text: " · retired" }) : null),
      el("td", { class: "name", text: row.name ?? "" }),
      yieldCell(row.yield?.isolated),
      yieldCell(row.yield?.analytical),
    );
  }

  let shownYields: YieldsState | null = null;

  function renderYields(): void {
    const s = yields.state;
    const busy = s.loading !== null || exporting;
    copyTable.disabled = busy || s.total === null || s.total === 0;
    downloadCsv.disabled = copyTable.disabled;
    if (s === shownYields) return;
    shownYields = s;
    yieldsTable.setAttribute("aria-busy", String(s.loading === "list"));

    yieldsCount.textContent = s.loading === "list" ? "Loading yields…" : yieldsCountLine(s);

    const msg: Child[] = [];
    if (s.error) {
      msg.push(renderErrorBox(el, s.error, "yields"));
      const retry = el("button", { class: "btn", key: "yields-retry", attrs: { type: "button" }, text: "Retry" });
      retry.addEventListener("click", () => yields.load());
      msg.push(el("div", { class: "row" }, retry));
    }
    if (s.warnings.length > 0) {
      msg.push(
        el(
          "div",
          { class: "banner warn", attrs: { role: "note" } },
          el("strong", { text: "Registry warnings" }),
          el("ul", null, ...s.warnings.map((w) => el("li", { text: w }))),
        ),
      );
    }
    if (s.loading === null && s.total !== null) {
      // Without a filter and with every row loaded, "no yield anywhere" is known for sure.
      const noneStored =
        !isYieldsFiltered(s.filter) && s.rows.length === s.total && s.rows.every((r) => !hasYield(r.yield));
      let empty: string | null = null;
      if (s.total === 0 && s.filter.text !== null && s.registryTotal !== 0) empty = "No product matches this filter.";
      else if (s.total === 0 || noneStored) empty = NO_YIELDS_TEXT;
      if (empty) msg.push(el("p", { class: "empty-note", text: empty }));
    }
    yieldsMessages.replaceChildren(...msg.filter((c): c is HTMLElement => c instanceof HTMLElement));

    yieldsBody.replaceChildren(...s.rows.map(yieldRowElement));
    yieldsTable.hidden = s.rows.length === 0;

    yieldsMore.replaceChildren();
    if (yields.hasMore() || s.loading === "more") {
      const more = el("button", { class: "btn", key: "yields-more", attrs: { type: "button" }, text: "Show more" });
      more.disabled = s.loading !== null;
      more.addEventListener("click", () => yields.more());
      yieldsMore.append(more, s.loading === "more" ? el("span", { class: "small busy", text: "Loading…" }) : "");
    }
  }

  // ---------------------------------------------------------------- render
  function render(): void {
    renderOnce();
    // Spectra and files of variants just shown: requested after the render (their answers render again).
    if (filesToLoad.length > 0) {
      const ids = [...new Set(filesToLoad)];
      filesToLoad = [];
      files.load(ids);
    }
  }

  function renderOnce(): void {
    filesToLoad = [];
    const active = doc.activeElement as HTMLElement | null;
    const activeKey = active && root.contains(active) ? (active.dataset?.key ?? null) : null;
    const selection =
      active instanceof HTMLInputElement && active.type === "text" ? { start: active.selectionStart, end: active.selectionEnd } : null;
    renderRegistry();
    renderResults();
    renderYields();
    importView.render();
    const requested = names.takeFocusKey();
    // A starting-material choice whose save ended: focused again only when the focus was lost (it was disabled meanwhile).
    const routeKey = routes.takeFocusKey();
    const focusKey = requested ?? activeKey ?? routeKey;
    if (focusKey === null) return;
    if (requested === null && active && root.contains(active) && active.isConnected) return; // focus did not move
    for (const node of Array.from(root.querySelectorAll<HTMLElement>("[data-key]"))) {
      if (node.dataset.key === focusKey && !(node as HTMLButtonElement).disabled) {
        node.focus();
        if (requested === null && selection && node instanceof HTMLInputElement) node.setSelectionRange(selection.start, selection.end);
        break;
      }
    }
  }

  // Object URLs of PDFs live until the page is left.
  const onLeave = (): void => {
    importer.abandon();
    pdfTask?.load.cancel();
    pdfTask = null;
    viewer.revokeAll();
  };
  doc.defaultView?.addEventListener("pagehide", onLeave);

  render();
  controller.start();

  return {
    controller,
    names,
    yields,
    files,
    routes,
    viewer,
    importer,
    showView,
    destroy: () => {
      cancelFilter?.();
      cancelFilter = null;
      editor?.destroy();
      editor = null;
      doc.defaultView?.removeEventListener("pagehide", onLeave);
      onLeave();
      root.replaceChildren();
    },
  };
}
