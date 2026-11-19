// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StructureEditor } from "../src/compounds/editor";
import { mountCompoundsPage, NO_YIELDS_TEXT, type CompoundsPageHandle } from "../src/compounds/page";
import { yieldsCsv, yieldsCountLine, yieldsTsv, YieldsController } from "../src/compounds/state";
import { SITE_ORIGIN, type CompoundsParams, type YieldInfo, type YieldRow, type YieldsParams, type YieldsResult } from "../src/shared/protocol";
import { FakeClient, compoundsResult, flush, manualTimers, pingResult } from "./helpers";

const EVIL = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>`;

const info = (isolated: number | null, analytical: number | null = null): YieldInfo => ({
  isolated,
  analytical,
  updated_at: "2026-11-18T09:00:00+00:00",
});

function row(n: number, over: Partial<YieldRow> = {}): YieldRow {
  return {
    experiment_ref: String(4120 + n),
    experiment_code: `ABC-${String(n).padStart(3, "0")}`,
    experiment_url: null,
    product_index: 1,
    full_id: `${String(n).padStart(6, "0")}_01_001`,
    core_stereo_id: `${String(n).padStart(6, "0")}_01`,
    name: null,
    yield: info(80 + (n % 10) / 2),
    created_at: "2026-11-01T10:00:00+00:00",
    retired: false,
    ...over,
  };
}

function yieldsResult(items: YieldRow[], over: Partial<YieldsResult> = {}): YieldsResult {
  return { total: items.length, registry_total: items.length, offset: 0, limit: 200, items, warnings: [], ...over };
}

class NoEditor implements StructureEditor {
  getMolfile(): string | null {
    return null;
  }
  clear(): void {}
  destroy(): void {}
}

let handle: CompoundsPageHandle | null = null;
afterEach(() => {
  handle?.destroy();
  handle = null;
  document.body.replaceChildren();
});

async function mount() {
  const client = new FakeClient();
  const timers = manualTimers();
  const root = document.createElement("div");
  document.body.append(root);
  const copyText = vi.fn(async (_text: string) => {});
  const downloads: { name: string; text: string; type: string }[] = [];
  handle = mountCompoundsPage({
    doc: document,
    root,
    client,
    copyText,
    createEditor: () => new NoEditor(),
    schedule: timers.schedule,
    downloadText: (name, text, type) => downloads.push({ name, text, type }),
  });
  client.last("ping").resolve({ ok: true, result: pingResult() });
  client.last("compounds").resolve({ ok: true, result: compoundsResult() });
  await flush();
  return { client, timers, root, copyText, downloads, handle };
}
type Ctx = Awaited<ReturnType<typeof mount>>;

function button(root: HTMLElement, text: string): HTMLButtonElement {
  const found = Array.from(root.querySelectorAll<HTMLButtonElement>("button")).find((b) => b.textContent === text);
  if (!found) throw new Error(`no button ${text}`);
  return found;
}
const yieldsParams = (ctx: Ctx) => ctx.client.last("yields").params as YieldsParams;
const tableRows = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLTableRowElement>("table.yields-table tbody tr"));
const cellTexts = (tr: HTMLTableRowElement) => Array.from(tr.children).map((c) => c.textContent);

async function openYields(ctx: Ctx, result: YieldsResult = yieldsResult([row(6)])) {
  button(ctx.root, "Yields").click();
  ctx.client.last("yields").resolve({ ok: true, result });
  await flush();
}

describe("compounds page: the view switch", () => {
  it("shows the compound list by default and loads yields only when the table is opened", async () => {
    const ctx = await mount();
    const compounds = button(ctx.root, "Compounds");
    const yields = button(ctx.root, "Yields");
    expect(compounds.getAttribute("aria-pressed")).toBe("true");
    expect(yields.getAttribute("aria-pressed")).toBe("false");
    expect(ctx.root.querySelector<HTMLElement>("#view-compounds")!.hidden).toBe(false);
    expect(ctx.root.querySelector<HTMLElement>("#view-yields")!.hidden).toBe(true);
    expect(ctx.client.callsOf("yields")).toHaveLength(0);
    yields.click();
    expect(yields.getAttribute("aria-pressed")).toBe("true");
    expect(ctx.root.querySelector<HTMLElement>("#view-compounds")!.hidden).toBe(true);
    expect(ctx.root.querySelector<HTMLElement>("#view-yields")!.hidden).toBe(false);
    expect(ctx.client.callsOf("yields")).toHaveLength(1);
    expect(yieldsParams(ctx)).toEqual({ offset: 0, limit: 200 });
    expect(ctx.root.querySelector(".yields-count")!.textContent).toBe("Loading yields…");
  });
});

describe("compounds page: the yields table", () => {
  it("lists experiment, product, ID, name and both yields; a dash where there is none", async () => {
    const ctx = await mount();
    await openYields(
      ctx,
      yieldsResult([
        row(6, { experiment_url: `${SITE_ORIGIN}/startUseCase?pk=4126`, name: "ethanol", yield: info(76.2, 78) }),
        row(7, { experiment_code: null, product_index: 2, yield: null }),
        row(8, { experiment_url: "https://evil.example/8", yield: info(null, 73.65) }),
      ]),
    );
    const rows = tableRows(ctx.root);
    expect(rows.map(cellTexts)).toEqual([
      ["ABC-006", "1", "000006_01_001", "ethanol", "76.2 %", "78 %"],
      ["[4127]", "2", "000007_01_001", "", "–", "–"],
      ["ABC-008", "1", "000008_01_001", "", "–", "73.65 %"],
    ]);
    expect(Array.from(ctx.root.querySelectorAll("table.yields-table th")).map((t) => t.textContent)).toEqual([
      "Experiment",
      "Product",
      "Compound ID",
      "Name",
      "Isolated yield",
      "Analytical yield",
    ]);
    // Links only through the page's safe-URL check, in a new tab.
    const link = rows[0]!.querySelector("a")!;
    expect(link.getAttribute("href")).toBe(`${SITE_ORIGIN}/startUseCase?pk=4126`);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(rows[2]!.querySelector("a")).toBeNull();
    expect(rows[0]!.dataset.id).toBe("000006_01_001");
    expect(ctx.root.querySelector(".yields-count")!.textContent).toBe("3 products");
  });

  it("marks retired batches and shows host text as text only", async () => {
    const ctx = await mount();
    await openYields(ctx, yieldsResult([row(6, { retired: true, name: EVIL, experiment_code: EVIL })]));
    const tr = tableRows(ctx.root)[0]!;
    expect(tr.classList.contains("retired-batch")).toBe(true);
    expect(tr.querySelector(".retired")!.textContent).toBe(" · retired");
    expect(tr.querySelector(".name")!.textContent).toBe(EVIL);
    expect(ctx.root.querySelector("#view-yields img, #view-yields script")).toBeNull();
    expect(tr.querySelector(".exp")!.textContent).toBe(EVIL);
  });

  it("filters by text after a pause in typing, and at once on Enter", async () => {
    const ctx = await mount();
    await openYields(ctx, yieldsResult([row(6)], { registry_total: 40 }));
    const filter = ctx.root.querySelector<HTMLInputElement>("#yields-filter")!;
    filter.value = "ABC";
    filter.dispatchEvent(new Event("input"));
    filter.value = "ABC-00";
    filter.dispatchEvent(new Event("input"));
    expect(ctx.client.callsOf("yields")).toHaveLength(1);
    ctx.timers.run();
    expect(ctx.client.callsOf("yields")).toHaveLength(2);
    expect(yieldsParams(ctx)).toEqual({ text: "ABC-00", offset: 0, limit: 200 });
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult([row(6)], { total: 12, registry_total: 40 }) });
    await flush();
    expect(ctx.root.querySelector(".yields-count")!.textContent).toBe("12 of 40 products (showing 1)");
    filter.value = "ABC-006";
    filter.dispatchEvent(new Event("input"));
    filter.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    expect(yieldsParams(ctx)).toEqual({ text: "ABC-006", offset: 0, limit: 200 });
    ctx.timers.run();
    expect(ctx.client.callsOf("yields")).toHaveLength(3);
  });

  it("'Only with a yield' asks for recorded rows only", async () => {
    const ctx = await mount();
    await openYields(ctx);
    const only = ctx.root.querySelector<HTMLInputElement>("#yields-only")!;
    only.checked = true;
    only.dispatchEvent(new Event("change"));
    expect(yieldsParams(ctx)).toEqual({ only_recorded: true, offset: 0, limit: 200 });
  });

  it("drops the answer for an older filter", async () => {
    const ctx = await mount();
    button(ctx.root, "Yields").click();
    const first = ctx.client.last("yields");
    const only = ctx.root.querySelector<HTMLInputElement>("#yields-only")!;
    only.checked = true;
    only.dispatchEvent(new Event("change"));
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult([row(7)]) });
    first.resolve({ ok: true, result: yieldsResult([row(6)]) });
    await flush();
    expect(tableRows(ctx.root).map((r) => r.dataset.id)).toEqual(["000007_01_001"]);
  });

  it("'Show more' loads the next page while there are more rows", async () => {
    const ctx = await mount();
    const first = Array.from({ length: 200 }, (_, i) => row(i + 1));
    await openYields(ctx, yieldsResult(first, { total: 250, registry_total: 250 }));
    expect(ctx.root.querySelector(".yields-count")!.textContent).toBe("250 products (showing 200)");
    button(ctx.root, "Show more").click();
    expect(yieldsParams(ctx)).toEqual({ offset: 200, limit: 200 });
    expect(button(ctx.root, "Show more").disabled).toBe(true);
    ctx.client.last("yields").resolve({
      ok: true,
      result: yieldsResult(Array.from({ length: 50 }, (_, i) => row(i + 201)), { total: 250, registry_total: 250, offset: 200 }),
    });
    await flush();
    expect(tableRows(ctx.root)).toHaveLength(250);
    expect(ctx.root.querySelector(".yields-count")!.textContent).toBe("250 products");
    expect(Array.from(ctx.root.querySelectorAll("button")).some((b) => b.textContent === "Show more")).toBe(false);
  });

  it("a click on a compound ID shows it in the compound list", async () => {
    const ctx = await mount();
    await openYields(ctx);
    button(ctx.root, "000006_01_001").click();
    expect(ctx.root.querySelector<HTMLElement>("#view-compounds")!.hidden).toBe(false);
    expect(ctx.root.querySelector<HTMLElement>("#view-yields")!.hidden).toBe(true);
    expect((ctx.client.last("compounds").params as CompoundsParams).text).toBe("000006_01_001");
    expect(ctx.root.querySelector<HTMLInputElement>("#search-text")!.value).toBe("000006_01_001");
  });

  it("explains an empty table in plain words", async () => {
    const empty = await mount();
    await openYields(empty, yieldsResult([], { total: 0, registry_total: 0 }));
    expect(empty.root.querySelector(".empty-note")!.textContent).toBe(NO_YIELDS_TEXT);
    expect(empty.root.querySelector<HTMLElement>("table.yields-table")!.hidden).toBe(true);
    handle!.destroy();

    const noYields = await mount();
    await openYields(noYields, yieldsResult([row(6, { yield: null }), row(7, { yield: null })]));
    expect(noYields.root.querySelector(".empty-note")!.textContent).toBe(NO_YIELDS_TEXT);
    expect(tableRows(noYields.root)).toHaveLength(2);
    handle!.destroy();

    const noMatch = await mount();
    await openYields(noMatch, yieldsResult([row(6)], { registry_total: 5 }));
    const filter = noMatch.root.querySelector<HTMLInputElement>("#yields-filter")!;
    filter.value = "zzz";
    filter.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    noMatch.client.last("yields").resolve({ ok: true, result: yieldsResult([], { total: 0, registry_total: 5 }) });
    await flush();
    expect(noMatch.root.querySelector(".empty-note")!.textContent).toBe("No product matches this filter.");
  });

  it("shows an error with Retry", async () => {
    const ctx = await mount();
    button(ctx.root, "Yields").click();
    ctx.client.last("yields").resolve({ ok: false, error: { code: "storage_error", message: "locked" } });
    await flush();
    expect(ctx.root.querySelector(".yields-messages")!.textContent).toContain("locked");
    button(ctx.root, "Retry").click();
    expect(ctx.client.callsOf("yields")).toHaveLength(2);
  });
});

describe("compounds page: exporting the yields", () => {
  const rows3 = [
    row(6, { name: 'ethanol, "abs."', yield: info(76.2, 78) }),
    row(7, { experiment_code: null, yield: null, retired: true }),
    row(8, { name: "=HYPERLINK(\"x\")", yield: info(null, 73.65) }),
  ];

  it("Copy table copies every matching row as tab-separated text with a header line", async () => {
    const ctx = await mount();
    await openYields(ctx, yieldsResult(rows3.slice(0, 2), { total: 3, registry_total: 3 }));
    button(ctx.root, "Copy table").click();
    await flush();
    // The rest is fetched first, in big pages.
    expect(yieldsParams(ctx)).toEqual({ offset: 2, limit: 500 });
    expect(ctx.copyText).not.toHaveBeenCalled();
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult(rows3.slice(2), { total: 3, registry_total: 3, offset: 2 }) });
    await flush();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledTimes(1);
    expect(ctx.copyText.mock.calls[0]![0]).toBe(
      [
        "Experiment\tProduct\tCompound ID\tName\tIsolated yield (%)\tAnalytical yield (%)\tRetired",
        'ABC-006\t1\t000006_01_001\t"ethanol, ""abs."""\t76.2\t78\t',
        "[4127]\t1\t000007_01_001\t\t\t\tyes",
        "ABC-008\t1\t000008_01_001\t\"'=HYPERLINK(\"\"x\"\")\"\t\t73.65\t",
        "",
      ].join("\n"),
    );
    expect(ctx.root.querySelector(".export-feedback")!.textContent).toBe("3 rows copied. Paste them into a spreadsheet.");
    expect(tableRows(ctx.root)).toHaveLength(3);
  });

  it("Download CSV saves yields.csv: comma-separated, quoted, UTF-8 with BOM", async () => {
    const ctx = await mount();
    await openYields(ctx, yieldsResult(rows3));
    button(ctx.root, "Download CSV").click();
    await flush();
    await flush();
    expect(ctx.client.callsOf("yields")).toHaveLength(1); // everything was loaded already
    expect(ctx.downloads).toHaveLength(1);
    const { name, text, type } = ctx.downloads[0]!;
    expect(name).toBe("yields.csv");
    expect(type).toBe("text/csv;charset=utf-8");
    expect(text).toBe(
      "\uFEFF" +
        [
          "Experiment,Product,Compound ID,Name,Isolated yield (%),Analytical yield (%),Retired",
          'ABC-006,1,000006_01_001,"ethanol, ""abs.""",76.2,78,',
          "[4127],1,000007_01_001,,,,yes",
          "ABC-008,1,000008_01_001,\"'=HYPERLINK(\"\"x\"\")\",,73.65,",
          "",
        ].join("\r\n"),
    );
    expect(ctx.root.querySelector(".export-feedback")!.textContent).toBe("3 rows saved as yields.csv.");
  });

  it("exports nothing when loading the rest fails, and says so", async () => {
    const ctx = await mount();
    await openYields(ctx, yieldsResult([row(6)], { total: 2 }));
    button(ctx.root, "Download CSV").click();
    await flush();
    ctx.client.last("yields").resolve({ ok: false, error: { code: "host_timeout", message: "No answer." } });
    await flush();
    await flush();
    expect(ctx.downloads).toHaveLength(0);
    expect(ctx.root.querySelector(".export-feedback")!.textContent).toBe("Nothing was exported: No answer.");
  });

  it("the export buttons wait for the table", async () => {
    const ctx = await mount();
    button(ctx.root, "Yields").click();
    expect(button(ctx.root, "Copy table").disabled).toBe(true);
    expect(button(ctx.root, "Download CSV").disabled).toBe(true);
  });
});

describe("yields state (pure)", () => {
  it("formats the count line", () => {
    const base = { filter: { text: null, onlyRecorded: false }, rows: [row(1)], total: 1, registryTotal: 1 };
    expect(yieldsCountLine(base)).toBe("1 product");
    expect(yieldsCountLine({ ...base, filter: { text: null, onlyRecorded: true }, registryTotal: 40 })).toBe("1 of 40 products");
    expect(yieldsCountLine({ ...base, total: null })).toBe("");
  });

  it("guards text cells a spreadsheet would read as a formula", () => {
    for (const start of ["=", "+", "-", "@"]) {
      expect(yieldsTsv([row(1, { name: `${start}1` })]).split("\n")[1]!.split("\t")[3]).toBe(`'${start}1`);
      expect(yieldsCsv([row(1, { name: `${start}1` })]).split("\r\n")[1]!.split(",")[3]).toBe(`'${start}1`);
    }
  });

  it("keeps tabs and line breaks out of the tab-separated text", () => {
    expect(yieldsTsv([row(1, { name: "a\tb\nc" })]).split("\n")[1]!.split("\t")[3]).toBe("a b c");
  });

  it("fetchAll stops when the host sends no new rows (no endless loop)", async () => {
    const client = new FakeClient();
    const yields = new YieldsController(client, { onChange: () => {} });
    yields.load();
    client.last("yields").resolve({ ok: true, result: yieldsResult([row(1)], { total: 5 }) });
    await flush();
    const all = yields.fetchAll();
    await flush();
    client.last("yields").resolve({ ok: true, result: yieldsResult([row(1)], { total: 5, offset: 1 }) });
    await flush();
    expect(await all).toEqual({ ok: true, rows: [row(1)] });
    expect(client.callsOf("yields")).toHaveLength(2);
  });
});
