// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StructureEditor } from "../src/compounds/editor";
import { mountCompoundsPage, type CompoundsPageHandle } from "../src/compounds/page";
import { PDF_FALLBACK_TEXT, PdfViewer, type ObjectUrls } from "../src/compounds/pdf-viewer";
import type { CompoundFilesResult, FindResult, FolderFile, ReadPdfParams, ReadPdfResult, SpectrumInfo } from "../src/shared/protocol";
import { FakeClient, batch, compoundItem, compoundsResult, compoundVariant, coreFindResult, flush, foundVariant, pingResult, spectrum } from "./helpers";

const EVIL = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>`;

class NoEditor implements StructureEditor {
  getMolfile(): string | null {
    return null;
  }
  setMolfile(): void {}
  clear(): void {}
  destroy(): void {}
}

/** Blob URLs without a real URL store: records what was made and revoked. */
class FakeUrls implements ObjectUrls {
  made: { url: string; blob: Blob }[] = [];
  revoked: string[] = [];
  create(blob: Blob): string {
    const url = `blob:moz-extension://test/${this.made.length + 1}`;
    this.made.push({ url, blob });
    return url;
  }
  revoke(url: string): void {
    this.revoked.push(url);
  }
}

let handle: CompoundsPageHandle | null = null;
afterEach(() => {
  handle?.destroy();
  handle = null;
  document.body.replaceChildren();
  document.body.className = "";
});

function mount() {
  const client = new FakeClient();
  const root = document.createElement("div");
  document.body.append(root);
  const urls = new FakeUrls();
  const copyText = vi.fn(async () => {});
  handle = mountCompoundsPage({ doc: document, root, client, copyText, objectUrls: urls, createEditor: () => new NoEditor() });
  return { client, root, urls, copyText, handle };
}
type Ctx = ReturnType<typeof mount>;

function button(scope: ParentNode, label: string | RegExp): HTMLButtonElement {
  const all = Array.from(scope.querySelectorAll<HTMLButtonElement>("button"));
  const found = all.find((b) => (typeof label === "string" ? b.textContent === label : label.test(b.textContent ?? "")));
  if (!found) throw new Error(`no button ${label}; have: ${all.map((b) => b.textContent).join(" | ")}`);
  return found;
}

function folderFile(over: Partial<FolderFile> = {}): FolderFile {
  return { name: "000042_01.mol", size: 1200, modified: "2026-11-17T14:00:00+00:00", is_pdf: false, recorded: false, ...over };
}

function filesResult(over: Partial<CompoundFilesResult> = {}): CompoundFilesResult {
  return {
    core_stereo_id: "000042_01",
    folder: { path: "C:\\Registry\\compounds\\000042_01", exists: true },
    spectra: [],
    files: [],
    files_truncated: false,
    warnings: [],
    ...over,
  };
}

function twoVariants(): FindResult {
  return coreFindResult({
    variants: [
      foundVariant(),
      foundVariant({ core_stereo_id: "000042_02", stereo_no: 2, stereo_mode: "rac", inchikey: "KEY-TWO-N", batches: [] }),
    ],
  });
}

async function start(ctx: Ctx, ping = pingResult()) {
  ctx.client.last("ping").resolve({ ok: true, result: ping });
  ctx.client.last("compounds").resolve({
    ok: true,
    result: compoundsResult({ items: [compoundItem({ variants: [compoundVariant(), compoundVariant({ core_stereo_id: "000042_02", stereo_no: 2 })] })] }),
  });
  await flush();
}

async function openDetails(ctx: Ctx, find: FindResult = twoVariants()) {
  button(ctx.root, "Show stereo variants and details").click();
  ctx.client.last("find").resolve({ ok: true, result: find });
  await flush();
}

const section = (ctx: Ctx, id = "000042_01") => ctx.root.querySelector<HTMLElement>(`.files-section[data-core-stereo="${id}"]`)!;
const filesCall = (ctx: Ctx, id: string) => {
  const calls = ctx.client.callsOf("compound_files").filter((c) => (c.params as { core_stereo_id: string }).core_stereo_id === id);
  return calls[calls.length - 1]!;
};

async function answerFiles(ctx: Ctx, id: string, result: CompoundFilesResult) {
  filesCall(ctx, id).resolve({ ok: true, result });
  await flush();
}

async function shown(ctx: Ctx, result: CompoundFilesResult = filesResult()) {
  await start(ctx);
  await openDetails(ctx);
  await answerFiles(ctx, "000042_01", result);
  await answerFiles(ctx, "000042_02", filesResult({ core_stereo_id: "000042_02", folder: null }));
  return section(ctx);
}

function pdfPiece(bytes: Uint8Array, offset: number, chunk: number, name = "000042_01_001_FC2.pdf"): ReadPdfResult {
  const data = bytes.subarray(offset, Math.min(bytes.length, offset + chunk));
  return { name, size: bytes.length, offset, data: Buffer.from(data).toString("base64"), eof: offset + data.length >= bytes.length };
}

function pdfBytes(length: number): Uint8Array {
  const bytes = new Uint8Array(length);
  for (let i = 0; i < length; i++) bytes[i] = (i * 13) & 0xff;
  bytes.set([0x25, 0x50, 0x44, 0x46, 0x2d], 0);
  return bytes;
}

const FULL_SPECTRUM = (): SpectrumInfo =>
  spectrum({
    comment: "after column",
    sample: "ABC-001 FC2",
    experiment: "PROTON",
    pulse_sequence: "zg30",
    report: "1H NMR (500 MHz, CDCl3) δ 7.26 (s, 1H), 3.70 (q, J = 7.0 Hz, 2H).",
    parameters: [
      { name: "Number of scans", value: "16" },
      { name: "Temperature", value: "298 K" },
    ],
    mnova_file: "C:\\Registry\\compounds\\000042_01\\000042_01_001_FC2.mnova",
    pdf_file: "C:\\Registry\\compounds\\000042_01\\000042_01_001_FC2.pdf",
    pdf_available: true,
  });

// ------------------------------------------------------------------ section

describe("spectra and files: loading", () => {
  it("loads lazily when the details are opened, one request per variant, with a section per variant", async () => {
    const ctx = mount();
    await start(ctx);
    expect(ctx.client.callsOf("compound_files")).toHaveLength(0);
    await openDetails(ctx);
    expect(ctx.client.callsOf("compound_files").map((c) => c.params)).toEqual([
      { core_stereo_id: "000042_01" },
      { core_stereo_id: "000042_02" },
    ]);
    const blocks = Array.from(ctx.root.querySelectorAll(".variant-block"));
    expect(blocks.map((b) => b.querySelector(".files-section")!.getAttribute("data-core-stereo"))).toEqual(["000042_01", "000042_02"]);
    expect(section(ctx).querySelector("h5")!.textContent).toBe("Spectra and files");
    expect(section(ctx).getAttribute("aria-labelledby")).toBe(section(ctx).querySelector("h5")!.id);
    expect(section(ctx).textContent).toContain("Loading the spectra and files…");
    await answerFiles(ctx, "000042_01", filesResult());
    expect(section(ctx).textContent).not.toContain("Loading");
    // re-renders (names, other answers) send nothing new
    await answerFiles(ctx, "000042_02", filesResult({ core_stereo_id: "000042_02" }));
    expect(ctx.client.callsOf("compound_files")).toHaveLength(2);
  });

  it("Refresh asks again; an older answer that arrives late is ignored", async () => {
    const ctx = mount();
    await start(ctx);
    await openDetails(ctx);
    const first = filesCall(ctx, "000042_01");
    button(section(ctx), "Refresh").click();
    expect(ctx.client.callsOf("compound_files")).toHaveLength(3);
    const second = filesCall(ctx, "000042_01");
    second.resolve({ ok: true, result: filesResult({ warnings: ["fresh"] }) });
    await flush();
    first.resolve({ ok: true, result: filesResult({ warnings: ["stale"] }) });
    await flush();
    expect(section(ctx).textContent).toContain("fresh");
    expect(section(ctx).textContent).not.toContain("stale");
    // focus stays on Refresh after the rebuild
    button(section(ctx), "Refresh").focus();
    button(section(ctx), "Refresh").click();
    expect(document.activeElement?.textContent).toBe("Refresh");
  });

  it("ignores answers for details that were closed, and asks again when they are opened again", async () => {
    const ctx = mount();
    await start(ctx);
    await openDetails(ctx);
    const late = filesCall(ctx, "000042_01");
    button(ctx.root, "Hide stereo variants and details").click();
    late.resolve({ ok: true, result: filesResult({ warnings: ["late"] }) });
    await flush();
    expect(ctx.root.querySelector(".files-section")).toBeNull();
    await openDetails(ctx);
    expect(ctx.client.callsOf("compound_files")).toHaveLength(4);
    expect(section(ctx).textContent).not.toContain("late");
  });

  it("shows a failed load with the host's message, and Refresh tries again", async () => {
    const ctx = mount();
    await start(ctx);
    await openDetails(ctx);
    filesCall(ctx, "000042_01").resolve({ ok: false, error: { code: "storage_error", message: "The registry database could not be read." } });
    await flush();
    expect(section(ctx).querySelector(".banner.error")!.textContent).toContain("The registry database could not be read.");
    button(section(ctx), "Refresh").click();
    await answerFiles(ctx, "000042_01", filesResult());
    expect(section(ctx).querySelector(".banner.error")).toBeNull();
  });
});

describe("spectra and files: content", () => {
  it("shows the folder as text with Copy, and says when it does not exist yet", async () => {
    const ctx = mount();
    const s = await shown(ctx, filesResult({ folder: { path: "C:\\Registry\\compounds\\000042_01", exists: false } }));
    const line = s.querySelector(".folder-line")!;
    expect(line.querySelector(".folder-path")!.textContent).toBe("C:\\Registry\\compounds\\000042_01");
    expect(line.querySelector("a")).toBeNull();
    button(line, "Copy").click();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledWith("C:\\Registry\\compounds\\000042_01");
    expect(s.querySelector(".folder-missing")!.textContent).toBe("This folder does not exist yet.");
    // the second variant has folder null: nothing about folders
    const other = section(ctx, "000042_02");
    expect(other.querySelector(".folder-line")).toBeNull();
    expect(other.textContent).not.toContain("folder does not exist");
  });

  it("shows the empty state and the answer's warnings as notes", async () => {
    const ctx = mount();
    const s = await shown(ctx, filesResult({ warnings: ["The folder could not be listed."] }));
    expect(s.querySelector(".files-empty")!.textContent).toBe(
      "No spectra are recorded for this compound yet. Spectra saved from MestReNova with the ChemLinker add-on appear here.",
    );
    expect(s.querySelector(".banner.warn[role=note]")!.textContent).toContain("The folder could not be listed.");
    expect(s.querySelector(".folder-missing")).toBeNull();
  });

  it("shows every part of a recorded spectrum", async () => {
    const ctx = mount();
    const s = await shown(ctx, filesResult({ spectra: [FULL_SPECTRUM()] }));
    const block = s.querySelector<HTMLElement>("li.spectrum")!;
    expect(block.dataset.fullId).toBe("000042_01_001");
    expect(block.querySelector(".spectrum-title")!.textContent).toBe("1H NMR · 500 MHz · CDCl3");
    expect(block.querySelector(".spectrum-name")!.textContent).toBe("ABC-001 FC2");
    expect(block.querySelector(".spectrum-of")!.textContent).toBe("000042_01_001 · ABC-001 · recorded 2026-11-17 14:05");
    expect(block.querySelector(".spectrum-sample")!.textContent).toBe("Sample: ABC-001 FC2");
    expect(block.querySelector(".spectrum-comment")!.textContent).toBe("Comment: after column");
    expect(block.querySelector(".spectrum-method")!.textContent).toBe("Experiment: PROTON · Pulse sequence: zg30");
    expect(block.querySelector(".report-text")!.textContent).toBe(FULL_SPECTRUM().report);
    button(block.querySelector(".spectrum-report")!, "Copy").click();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledWith(FULL_SPECTRUM().report);
    const details = block.querySelector<HTMLDetailsElement>("details.spectrum-params")!;
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")!.textContent).toBe("Parameters (2)");
    const rows = Array.from(details.querySelectorAll("tbody tr")).map((r) => Array.from(r.children).map((c) => c.textContent));
    expect(rows).toEqual([
      ["Number of scans", "16"],
      ["Temperature", "298 K"],
    ]);
    expect(Array.from(details.querySelectorAll("th")).map((t) => t.textContent)).toEqual(["Name", "Value"]);
    const paths = Array.from(block.querySelectorAll(".spectrum-files dd")).map((d) => d.textContent);
    expect(paths).toEqual([FULL_SPECTRUM().mnova_file, FULL_SPECTRUM().pdf_file]);
    expect(block.querySelector("a")).toBeNull(); // paths are never links
    expect(button(block, "Show PDF").getAttribute("aria-label")).toContain("000042_01_001");
  });

  it("keeps an opened parameter table open when the section is rebuilt", async () => {
    const ctx = mount();
    const s = await shown(ctx, filesResult({ spectra: [FULL_SPECTRUM()] }));
    const details = s.querySelector<HTMLDetailsElement>("details.spectrum-params")!;
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
    button(s, "Refresh").click();
    await answerFiles(ctx, "000042_01", filesResult({ spectra: [FULL_SPECTRUM()] }));
    expect(section(ctx).querySelector<HTMLDetailsElement>("details.spectrum-params")!.open).toBe(true);
  });

  it("leaves out what was not recorded, and falls back to the title", async () => {
    const ctx = mount();
    const bare = spectrum({ nucleus: null, frequency_mhz: null, solvent: null, experiment_code: null, title: "Crude" });
    const s = await shown(ctx, filesResult({ spectra: [bare] }));
    const block = s.querySelector("li.spectrum")!;
    expect(block.querySelector(".spectrum-title")!.textContent).toBe("Crude");
    expect(block.querySelector(".spectrum-name")).toBeNull();
    expect(block.querySelector(".spectrum-of")!.textContent).toBe("000042_01_001 · recorded 2026-11-17 14:05");
    for (const cls of [".spectrum-sample", ".spectrum-comment", ".spectrum-method", ".spectrum-report", ".spectrum-params", ".spectrum-files", ".pdf-control", ".pdf-missing"]) {
      expect(block.querySelector(cls), cls).toBeNull();
    }
  });

  it("shows of a crude spectrum only the path of its document", async () => {
    const ctx = mount();
    const path = "C:\\Registry\\compounds\\000042_01\\000042_01_001_FC2_crude.mnova";
    // even if a host sent details or a PDF with a crude spectrum, none of it is shown
    const crude = (over: Partial<SpectrumInfo> = {}) => ({ ...FULL_SPECTRUM(), crude: true, mnova_file: path, ...over });
    const s = await shown(
      ctx,
      filesResult({
        spectra: [
          crude(),
          crude({ spectrum_key: "ABC-001 13C", nucleus: "13C" }), // the same document: listed once
          crude({ spectrum_key: "no file", mnova_file: null }), // nothing to show
        ],
      }),
    );
    expect(s.querySelector("li.spectrum")).toBeNull();
    expect(s.querySelector(".files-empty")).toBeNull();
    expect(s.querySelector(".crude-head")!.textContent).toBe("Crude spectrum");
    const items = Array.from(s.querySelectorAll<HTMLElement>("li.crude-spectrum"));
    expect(items).toHaveLength(1);
    expect(items[0]!.querySelector(".crude-of")!.textContent).toBe("000042_01_001 · ABC-001");
    expect(items[0]!.querySelector(".crude-path")!.textContent).toBe(path);
    expect(items[0]!.querySelector("a")).toBeNull();
    for (const cls of [".pdf-show", ".pdf-missing", ".spectrum-report", ".spectrum-params", ".report-text"]) {
      expect(s.querySelector(cls), cls).toBeNull();
    }
    expect(s.textContent).not.toContain("7.26");
    expect(s.textContent).not.toContain(".pdf");
    button(items[0]!, "Copy").click();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledWith(path);
  });

  it("shows full spectra with their details next to the crude ones", async () => {
    const ctx = mount();
    const s = await shown(
      ctx,
      filesResult({
        spectra: [
          FULL_SPECTRUM(),
          spectrum({ spectrum_key: "c1", crude: true, mnova_file: "C:\\r\\a_crude.mnova" }),
          spectrum({ spectrum_key: "c2", crude: true, mnova_file: "C:\\r\\b_crude.mnova" }),
        ],
      }),
    );
    expect(s.querySelectorAll("li.spectrum")).toHaveLength(1);
    expect(s.querySelector(".crude-head")!.textContent).toBe("Crude spectra");
    expect(Array.from(s.querySelectorAll(".crude-path")).map((p) => p.textContent)).toEqual(["C:\\r\\a_crude.mnova", "C:\\r\\b_crude.mnova"]);
    expect(s.querySelectorAll(".pdf-show")).toHaveLength(1);
  });

  it("shows of a spectrum of an undesired side product only the path of its document", async () => {
    const ctx = mount();
    const path = "C:\\Registry\\compounds\\000042_01\\000042_01_001_FC2_side.mnova";
    const crudePath = "C:\\Registry\\compounds\\000042_01\\000042_01_001_FC3_crude_side.mnova";
    // even if a host sent details or a PDF with such a spectrum, none of it is shown
    const side = (over: Partial<SpectrumInfo> = {}) => ({ ...FULL_SPECTRUM(), side_product: true, mnova_file: path, ...over });
    const s = await shown(
      ctx,
      filesResult({
        spectra: [
          side(),
          side({ spectrum_key: "ABC-001 13C", nucleus: "13C" }), // the same document: listed once
          side({ spectrum_key: "no file", mnova_file: null }), // nothing to show
          side({ spectrum_key: "both", crude: true, mnova_file: crudePath }), // crude and side product: listed here only
        ],
      }),
    );
    expect(s.querySelector("li.spectrum")).toBeNull();
    expect(s.querySelector(".files-empty")).toBeNull();
    expect(s.querySelector("li.crude-spectrum")).toBeNull();
    expect(s.querySelector(".side-head")!.textContent).toBe("Undesired side products");
    const items = Array.from(s.querySelectorAll<HTMLElement>("li.side-spectrum"));
    expect(items).toHaveLength(2);
    expect(items[0]!.querySelector(".side-of")!.textContent).toBe("000042_01_001 · ABC-001");
    expect(items[0]!.querySelector(".side-path")!.textContent).toBe(path);
    expect(items[1]!.querySelector(".side-of")!.textContent).toBe("000042_01_001 · ABC-001 · crude");
    expect(items[1]!.querySelector(".side-path")!.textContent).toBe(crudePath);
    expect(items[0]!.querySelector("a")).toBeNull();
    for (const cls of [".pdf-show", ".pdf-missing", ".spectrum-report", ".spectrum-params", ".report-text"]) {
      expect(s.querySelector(cls), cls).toBeNull();
    }
    expect(s.textContent).not.toContain("7.26");
    expect(s.textContent).not.toContain(".pdf");
    button(items[0]!, "Copy").click();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledWith(path);
  });

  it("shows full, crude and side-product spectra next to each other", async () => {
    const ctx = mount();
    const s = await shown(
      ctx,
      filesResult({
        spectra: [
          FULL_SPECTRUM(),
          spectrum({ spectrum_key: "c1", crude: true, mnova_file: "C:\\r\\a_crude.mnova" }),
          spectrum({ spectrum_key: "s1", side_product: true, mnova_file: "C:\\r\\b_side.mnova" }),
        ],
      }),
    );
    expect(s.querySelectorAll("li.spectrum")).toHaveLength(1);
    expect(Array.from(s.querySelectorAll(".crude-path")).map((p) => p.textContent)).toEqual(["C:\\r\\a_crude.mnova"]);
    expect(s.querySelector(".side-head")!.textContent).toBe("Undesired side product");
    expect(Array.from(s.querySelectorAll(".side-path")).map((p) => p.textContent)).toEqual(["C:\\r\\b_side.mnova"]);
    expect(s.querySelectorAll(".pdf-show")).toHaveLength(1);
  });

  it("says where a PDF that is no longer available was", async () => {
    const ctx = mount();
    const s = await shown(ctx, filesResult({ spectra: [spectrum({ pdf_file: "D:\\old\\a.pdf", pdf_available: false })] }));
    expect(s.querySelector(".pdf-missing")!.textContent).toBe("The PDF is not at D:\\old\\a.pdf any more.");
    expect(s.querySelector(".pdf-show")).toBeNull();
  });

  it("lists the other files with size and date; only PDFs get Show PDF; says when the list is cut", async () => {
    const ctx = mount();
    const s = await shown(
      ctx,
      filesResult({
        files: [
          folderFile(),
          folderFile({ name: "notes.pdf", size: 1536, is_pdf: true }),
          folderFile({ name: "000042_01_001_FC2.pdf", is_pdf: true, recorded: true }),
        ],
        files_truncated: true,
      }),
    );
    expect(s.querySelector("h6")!.textContent).toBe("Other files in the folder");
    const items = Array.from(s.querySelectorAll<HTMLElement>("li.folder-file"));
    expect(items.map((i) => i.querySelector(".file-line")!.textContent)).toEqual([
      "000042_01.mol · 1.2 KB · 2026-11-17 14:00",
      "notes.pdf · 1.5 KB · 2026-11-17 14:00",
    ]);
    expect(items[0]!.querySelector("button")).toBeNull();
    expect(button(items[1]!, "Show PDF")).toBeTruthy();
    expect(s.querySelector(".files-truncated")!.textContent).toBe("The folder holds more files: only the first 3 are listed.");
  });

  it("puts host text into the section only as text", async () => {
    const ctx = mount();
    const s = await shown(
      ctx,
      filesResult({
        folder: { path: EVIL, exists: true },
        warnings: [EVIL],
        spectra: [
          spectrum({
            title: EVIL,
            nucleus: EVIL,
            solvent: EVIL,
            sample: EVIL,
            comment: EVIL,
            experiment_code: EVIL,
            report: EVIL,
            parameters: [{ name: EVIL, value: EVIL }],
            mnova_file: EVIL,
            pdf_file: EVIL,
          }),
        ],
        files: [folderFile({ name: EVIL.replace(/\//g, "") })],
      }),
    );
    expect(s.querySelector("script")).toBeNull();
    expect(s.querySelector("img")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(s.textContent!.split(EVIL).length - 1).toBeGreaterThanOrEqual(10);
  });
});

describe("compound folders on the page head", () => {
  it("shows ping.files_folder once near the registry line, with Copy", async () => {
    const ctx = mount();
    await start(ctx, pingResult({ files_folder: "C:\\Registry\\compounds" }));
    const line = ctx.root.querySelectorAll(".registry .files-folder");
    expect(line).toHaveLength(1);
    expect(line[0]!.textContent).toContain("C:\\Registry\\compounds");
    expect(line[0]!.querySelector("a")).toBeNull();
    button(line[0]!, "Copy").click();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledWith("C:\\Registry\\compounds");
  });

  it("shows nothing for an older host or null", async () => {
    const ctx = mount();
    await start(ctx, pingResult({ files_folder: null }));
    expect(ctx.root.querySelector(".files-folder")).toBeNull();
  });
});

// ------------------------------------------------------------------ batches with yields

describe("batches with their yields", () => {
  async function batchesShown(batches: ReturnType<typeof batch>[]) {
    const ctx = mount();
    await start(ctx);
    await openDetails(ctx, coreFindResult({ variants: [foundVariant({ batches })] }));
    return Array.from(ctx.root.querySelectorAll<HTMLElement>("ul.batches li.batch"));
  }

  it("shows isolated only, both, analytical only, and nothing without a yield (null or absent)", async () => {
    const absent = batch({ full_id: "000042_01_005" });
    delete (absent as { yield?: unknown }).yield;
    const rows = await batchesShown([
      batch({ full_id: "000042_01_001", yield: { isolated: 76.2, analytical: null, updated_at: "2026-11-17T10:00:00+00:00" } }),
      batch({ full_id: "000042_01_002", yield: { isolated: 76.2, analytical: 78, updated_at: "2026-11-17T10:00:00+00:00" } }),
      batch({ full_id: "000042_01_003", yield: { isolated: null, analytical: 64.25, updated_at: "2026-11-17T10:00:00+00:00" } }),
      batch({ full_id: "000042_01_004", yield: null }),
      absent,
    ]);
    expect(rows.map((r) => r.dataset.id)).toEqual(["000042_01_001", "000042_01_002", "000042_01_003", "000042_01_004", "000042_01_005"]);
    expect(rows.map((r) => r.querySelector(".batch-yield")?.textContent ?? null)).toEqual([
      "76.2 % isolated",
      "76.2 % isolated · 78 % analytical",
      "64.25 % analytical",
      null,
      null,
    ]);
    expect(rows[0]!.textContent).toBe("000042_01_001 · ABC-001 · product 1 · 2026-11-01 · yield 76.2 % isolated");
    expect(rows[3]!.textContent).toBe("000042_01_004 · ABC-001 · product 1 · 2026-11-01");
    expect(rows[4]!.textContent).not.toMatch(/yield/i);
  });

  it("a retired batch still shows its yield", async () => {
    const [row] = await batchesShown([
      batch({ retired: true, retired_reason: "wrong", yield: { isolated: 50, analytical: null, updated_at: "2026-11-17T10:00:00+00:00" } }),
    ]);
    expect(row!.classList.contains("retired-batch")).toBe(true);
    expect(row!.querySelector(".batch-yield")!.textContent).toBe("50 % isolated");
    expect(row!.textContent).toContain("retired: wrong");
  });
});

// ------------------------------------------------------------------ PDF viewer

describe("Show PDF and the viewer", () => {
  async function withPdf() {
    const ctx = mount();
    const s = await shown(ctx, filesResult({ spectra: [FULL_SPECTRUM()], files: [folderFile({ name: "notes.pdf", is_pdf: true })] }));
    return { ctx, s };
  }

  async function serveAll(ctx: Ctx, bytes: Uint8Array, chunk: number, name?: string) {
    for (;;) {
      await flush();
      const call = ctx.client.callsOf("read_pdf").find((c) => !(c as { done?: boolean }).done);
      if (!call) return;
      (call as { done?: boolean }).done = true;
      call.resolve({ ok: true, result: pdfPiece(bytes, (call.params as ReadPdfParams).offset ?? 0, chunk, name) });
    }
  }

  it("loads with progress and Cancel, then shows the PDF in a dialog with a blob typed application/pdf", async () => {
    const { ctx, s } = await withPdf();
    const show = button(s.querySelector("li.spectrum")!, "Show PDF");
    show.focus();
    show.click();
    expect(ctx.client.last("read_pdf").params).toEqual({
      core_stereo_id: "000042_01",
      full_id: "000042_01_001",
      spectrum_key: "ABC-001 1H",
      offset: 0,
    });
    const control = s.querySelector("li.spectrum .pdf-control")!;
    expect(control.querySelector<HTMLElement>(".pdf-progress")!.hidden).toBe(false);
    expect(control.querySelector(".pdf-progress")!.textContent).toBe("Loading the PDF…");
    expect(show.disabled).toBe(true);
    expect(document.activeElement).toBe(button(control, "Cancel"));
    const bytes = pdfBytes(100);
    ctx.client.last("read_pdf").resolve({ ok: true, result: pdfPiece(bytes, 0, 40) });
    await flush();
    expect(control.querySelector(".pdf-progress")!.textContent).toBe("Loading the PDF… 40 %");
    await serveAll(ctx, bytes, 40);
    await flush();

    expect(ctx.urls.made).toHaveLength(1);
    const { url, blob } = ctx.urls.made[0]!;
    expect(blob.type).toBe("application/pdf");
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(bytes);
    const dialog = document.querySelector<HTMLElement>(".pdf-overlay [role=dialog]")!;
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(document.getElementById(dialog.getAttribute("aria-labelledby")!)!.textContent).toBe("000042_01_001_FC2.pdf");
    const frame = dialog.querySelector<HTMLIFrameElement>("iframe.pdf-frame")!;
    expect(frame.getAttribute("src")).toBe(url);
    const open = dialog.querySelector<HTMLAnchorElement>("a.pdf-open")!;
    expect(open.textContent).toBe("Open in a new tab");
    expect(open.getAttribute("href")).toBe(url);
    expect(open.getAttribute("target")).toBe("_blank");
    expect(open.getAttribute("rel")).toBe("noopener");
    const save = dialog.querySelector<HTMLAnchorElement>("a.pdf-save")!;
    expect(save.textContent).toBe("Save a copy");
    expect(save.getAttribute("download")).toBe("000042_01_001_FC2.pdf");
    expect(dialog.querySelector(".pdf-fallback")!.textContent).toBe(PDF_FALLBACK_TEXT);
    expect(document.activeElement).toBe(button(dialog, "Close"));
    expect(ctx.root.hasAttribute("inert")).toBe(true);
    expect(control.querySelector<HTMLElement>(".pdf-progress")!.hidden).toBe(true);
    expect(control.querySelector<HTMLElement>(".pdf-cancel")!.hidden).toBe(true);

    button(dialog, "Close").click();
    expect(document.querySelector(".pdf-overlay")).toBeNull();
    expect(ctx.urls.revoked).toEqual([url]);
    expect(ctx.root.hasAttribute("inert")).toBe(false);
    expect(document.activeElement).toBe(button(s.querySelector("li.spectrum")!, "Show PDF"));
  });

  async function openViewer(ctx: Ctx, s: HTMLElement, name = "notes.pdf") {
    button(s.querySelector("li.folder-file")!, "Show PDF").click();
    expect(ctx.client.last("read_pdf").params).toEqual({ core_stereo_id: "000042_01", name: "notes.pdf", offset: 0 });
    await serveAll(ctx, pdfBytes(30), 100, name);
    await flush();
    return document.querySelector<HTMLElement>(".pdf-overlay [role=dialog]")!;
  }

  it("Esc and a click on the backdrop close it; a click inside does not", async () => {
    const { ctx, s } = await withPdf();
    let dialog = await openViewer(ctx, s);
    dialog.click();
    expect(document.querySelector(".pdf-overlay")).not.toBeNull();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.querySelector(".pdf-overlay")).toBeNull();
    expect(document.activeElement).toBe(button(s.querySelector("li.folder-file")!, "Show PDF"));
    dialog = await openViewer(ctx, s);
    document.querySelector<HTMLElement>(".pdf-overlay")!.click();
    expect(document.querySelector(".pdf-overlay")).toBeNull();
    expect(ctx.urls.revoked).toEqual(ctx.urls.made.map((m) => m.url));
  });

  it("keeps the URL while a new tab or a download may use it, and revokes it when the page is left", async () => {
    const { ctx, s } = await withPdf();
    const dialog = await openViewer(ctx, s);
    const link = dialog.querySelector<HTMLAnchorElement>("a.pdf-open")!;
    link.addEventListener("click", (e) => e.preventDefault()); // jsdom does not open tabs
    link.click();
    button(dialog, "Close").click();
    expect(ctx.urls.revoked).toEqual([]);
    const second = await openViewer(ctx, s);
    const save = second.querySelector<HTMLAnchorElement>("a.pdf-save")!;
    save.addEventListener("click", (e) => e.preventDefault());
    save.click();
    button(second, "Close").click();
    expect(ctx.urls.revoked).toEqual([]);
    window.dispatchEvent(new Event("pagehide"));
    expect(ctx.urls.revoked.sort()).toEqual(ctx.urls.made.map((m) => m.url).sort());
  });

  it("shows only one viewer at a time", async () => {
    const { ctx, s } = await withPdf();
    await openViewer(ctx, s);
    // the page behind is inert while open, but a second open (programmatic) replaces the first
    ctx.handle.viewer.open({ name: "other.pdf", bytes: pdfBytes(10) });
    expect(document.querySelectorAll(".pdf-overlay")).toHaveLength(1);
    expect(document.querySelector(".pdf-title")!.textContent).toBe("other.pdf");
    expect(ctx.urls.revoked).toEqual([ctx.urls.made[0]!.url]);
    ctx.handle.destroy();
    handle = null;
    expect(document.querySelector(".pdf-overlay")).toBeNull();
    expect(ctx.urls.revoked).toEqual(ctx.urls.made.map((m) => m.url));
  });

  it("starting another PDF cancels the first load", async () => {
    const { ctx, s } = await withPdf();
    button(s.querySelector("li.spectrum")!, "Show PDF").click();
    const first = ctx.client.last("read_pdf");
    button(s.querySelector("li.folder-file")!, "Show PDF").click();
    expect(s.querySelector<HTMLElement>("li.spectrum .pdf-progress")!.hidden).toBe(true);
    expect(s.querySelector<HTMLElement>("li.folder-file .pdf-progress")!.hidden).toBe(false);
    first.resolve({ ok: true, result: pdfPiece(pdfBytes(10), 0, 100) });
    await flush();
    expect(ctx.client.callsOf("read_pdf")).toHaveLength(2);
    expect(ctx.urls.made).toHaveLength(0);
  });

  it("Cancel stops the load: nothing more is asked and no viewer opens", async () => {
    const { ctx, s } = await withPdf();
    const show = button(s.querySelector("li.spectrum")!, "Show PDF");
    show.click();
    const bytes = pdfBytes(100);
    ctx.client.last("read_pdf").resolve({ ok: true, result: pdfPiece(bytes, 0, 40) });
    await flush();
    expect(ctx.client.callsOf("read_pdf")).toHaveLength(2);
    button(s.querySelector("li.spectrum")!, "Cancel").click();
    expect(document.activeElement).toBe(show);
    expect(show.disabled).toBe(false);
    expect(s.querySelector<HTMLElement>("li.spectrum .pdf-progress")!.hidden).toBe(true);
    ctx.client.last("read_pdf").resolve({ ok: true, result: pdfPiece(bytes, 40, 100) });
    await flush();
    expect(ctx.client.callsOf("read_pdf")).toHaveLength(2);
    expect(document.querySelector(".pdf-overlay")).toBeNull();
    expect(ctx.urls.made).toHaveLength(0);
  });

  it("shows the host's error next to the button that was clicked", async () => {
    const { ctx, s } = await withPdf();
    button(s.querySelector("li.folder-file")!, "Show PDF").click();
    ctx.client.last("read_pdf").resolve({ ok: false, error: { code: "file_unavailable", message: "notes.pdf is not a PDF." } });
    await flush();
    const error = s.querySelector<HTMLElement>("li.folder-file .pdf-error")!;
    expect(error.hidden).toBe(false);
    expect(error.getAttribute("role")).toBe("alert");
    expect(error.textContent).toBe("The PDF could not be loaded: notes.pdf is not a PDF.");
    expect(s.querySelector<HTMLElement>("li.spectrum .pdf-error")!.hidden).toBe(true);
    expect(document.activeElement).toBe(button(s.querySelector("li.folder-file")!, "Show PDF"));
    expect(ctx.urls.made).toHaveLength(0);
    // a new try clears the error
    button(s.querySelector("li.folder-file")!, "Show PDF").click();
    expect(error.hidden).toBe(true);
  });

  it("explains a damaged answer in plain words", async () => {
    const { ctx, s } = await withPdf();
    button(s.querySelector("li.folder-file")!, "Show PDF").click();
    ctx.client.last("read_pdf").resolve({ ok: true, result: { name: "notes.pdf", size: 10, offset: 5, data: "", eof: false } });
    await flush();
    expect(s.querySelector("li.folder-file .pdf-error")!.textContent).toMatch(/^The PDF could not be loaded: The PDF did not arrive correctly/);
  });

  it("closing the details cancels a running load", async () => {
    const { ctx, s } = await withPdf();
    button(s.querySelector("li.spectrum")!, "Show PDF").click();
    button(ctx.root, "Hide stereo variants and details").click();
    ctx.client.last("read_pdf").resolve({ ok: true, result: pdfPiece(pdfBytes(10), 0, 100) });
    await flush();
    expect(ctx.urls.made).toHaveLength(0);
    expect(document.querySelector(".pdf-overlay")).toBeNull();
  });

  it("an injected viewer receives the name and bytes", async () => {
    const client = new FakeClient();
    const root = document.createElement("div");
    document.body.append(root);
    const opened: { name: string; bytes: Uint8Array }[] = [];
    handle = mountCompoundsPage({
      doc: document,
      root,
      client,
      createEditor: () => new NoEditor(),
      createViewer: () => ({ open: (o) => opened.push(o), close: () => {}, revokeAll: () => {} }),
    });
    const ctx = { client, root, urls: new FakeUrls(), copyText: vi.fn(), handle };
    await shown(ctx, filesResult({ files: [folderFile({ name: "notes.pdf", is_pdf: true })] }));
    button(section(ctx).querySelector("li.folder-file")!, "Show PDF").click();
    await serveAll(ctx, pdfBytes(12), 5, "notes.pdf");
    await flush();
    expect(opened).toHaveLength(1);
    expect(opened[0]!.name).toBe("notes.pdf");
    expect(opened[0]!.bytes).toEqual(pdfBytes(12));
  });
});

describe("PdfViewer alone", () => {
  it("makes no URL other than the blob it is given, typed PDF, and names the dialog by the file", () => {
    const urls = new FakeUrls();
    const background = document.createElement("div");
    document.body.append(background);
    const viewer = new PdfViewer({ doc: document, background, urls });
    viewer.open({ name: EVIL, bytes: pdfBytes(8) });
    expect(urls.made.map((m) => m.blob.type)).toEqual(["application/pdf"]);
    const dialog = viewer.dialog!;
    expect(dialog.querySelector("script")).toBeNull();
    expect(dialog.querySelector(".pdf-title")!.textContent).toBe(EVIL);
    expect(dialog.querySelector("a.pdf-save")!.getAttribute("download")!.endsWith(".pdf")).toBe(true);
    expect(background.getAttribute("aria-hidden")).toBe("true");
    viewer.close();
    expect(viewer.isOpen).toBe(false);
    expect(urls.revoked).toEqual([urls.made[0]!.url]);
    viewer.close(); // twice is harmless
    viewer.revokeAll();
    expect(urls.revoked).toHaveLength(1);
  });
});
