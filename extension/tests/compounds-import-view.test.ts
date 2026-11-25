// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StructureEditor } from "../src/compounds/editor";
import { SKIP_ALL_REGISTERED } from "../src/compounds/import";
import { IMPORT_INTRO, IMPORT_REPORT_NAME } from "../src/compounds/import-view";
import { mountCompoundsPage, type CompoundsPageHandle } from "../src/compounds/page";
import { experimentPageUrl } from "../src/shared/import";
import type { YieldRow, YieldsResult } from "../src/shared/protocol";
import { FakeTabs, fakeTimers, FIXTURE, pageProduct, ready, yieldRow } from "./compounds-import-fakes";
import { FakeClient, compoundsResult, flush, manualTimers, pingResult } from "./helpers";

class NoEditor implements StructureEditor {
  getMolfile(): string | null {
    return null;
  }
  setMolfile(): void {}
  clear(): void {}
  destroy(): void {}
}

let handle: CompoundsPageHandle | null = null;
afterEach(() => {
  handle?.destroy();
  handle = null;
  document.body.replaceChildren();
  vi.useRealTimers();
});

function yieldsResult(items: YieldRow[]): YieldsResult {
  return { total: items.length, registry_total: items.length, offset: 0, limit: 500, items, warnings: [] };
}

async function mount(tabs = new FakeTabs()) {
  const client = new FakeClient();
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
    schedule: manualTimers().schedule,
    downloadText: (name, text, type) => downloads.push({ name, text, type }),
    tabs,
    importTimers: fakeTimers,
  });
  client.last("ping").resolve({ ok: true, result: pingResult() });
  client.last("compounds").resolve({ ok: true, result: compoundsResult() });
  await flush();
  return { client, root, tabs, copyText, downloads, handle };
}
type Ctx = Awaited<ReturnType<typeof mount>>;

const $ = <T extends HTMLElement = HTMLElement>(ctx: Ctx, selector: string): T => {
  const found = ctx.root.querySelector<T>(selector);
  if (!found) throw new Error(`no ${selector}`);
  return found;
};
function button(root: HTMLElement, text: string): HTMLButtonElement {
  const found = Array.from(root.querySelectorAll<HTMLButtonElement>("button")).find((b) => b.textContent === text);
  if (!found) throw new Error(`no button ${text}`);
  return found;
}
const yieldCalls = (ctx: Ctx) => ctx.client.callsOf("yields");

/** Waits until the file was read (FileReader / Blob in jsdom answer asynchronously). */
async function until(test: () => boolean): Promise<void> {
  for (let i = 0; i < 100 && !test(); i++) await new Promise((r) => setTimeout(r, 5));
  if (!test()) throw new Error("timed out");
}

async function chooseFile(ctx: Ctx, bytes: Uint8Array = FIXTURE, name = "experiments.csv"): Promise<void> {
  const input = $<HTMLInputElement>(ctx, "#import-file");
  const before = yieldCalls(ctx).length;
  const file = new File([new Uint8Array(bytes)], name, { type: "text/csv" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new Event("change"));
  await until(() => yieldCalls(ctx).length > before || ctx.handle.importer.state.parsed !== null);
}

async function openWithPlan(ctx: Ctx, rows: YieldRow[] = []): Promise<void> {
  button(ctx.root, "Import").click();
  await chooseFile(ctx);
  ctx.client.last("yields").resolve({ ok: true, result: yieldsResult(rows) });
  await flush();
}

const bodyRows = (ctx: Ctx) => Array.from(ctx.root.querySelectorAll<HTMLTableRowElement>("table.import-table tbody tr"));
const cells = (tr: HTMLTableRowElement) => Array.from(tr.children).map((c) => c.textContent);

describe("compounds page: the Import view", () => {
  it("is the third view; opening it loads nothing and opens nothing", async () => {
    const ctx = await mount();
    const tab = button(ctx.root, "Import");
    expect(Array.from(ctx.root.querySelectorAll("nav.view-switch button")).map((b) => b.textContent)).toEqual(["Compounds", "Yields", "Import"]);
    expect(tab.getAttribute("aria-pressed")).toBe("false");
    expect($(ctx, "#view-import").hidden).toBe(true);
    tab.click();
    expect(tab.getAttribute("aria-pressed")).toBe("true");
    expect(button(ctx.root, "Compounds").getAttribute("aria-pressed")).toBe("false");
    expect($(ctx, "#view-import").hidden).toBe(false);
    expect($(ctx, "#view-compounds").hidden).toBe(true);
    expect($(ctx, "#view-yields").hidden).toBe(true);
    expect(yieldCalls(ctx)).toHaveLength(0);
    expect(ctx.tabs.events).toEqual([]);
    expect($(ctx, ".import-intro").textContent).toBe(
      "Export your experiment list from the ELN as a CSV file and choose it here. The file tells the registry which experiments exist; every structure is still read from the experiment page, so each experiment is opened once in a background tab. Stay logged in to the ELN while it runs.",
    );
    expect($(ctx, ".import-intro").textContent).toBe(IMPORT_INTRO);
    const input = $<HTMLInputElement>(ctx, "#import-file");
    expect(input.type).toBe("file");
    expect(input.getAttribute("accept")).toBe(".csv,text/csv");
    expect(ctx.root.querySelector(`label[for="import-file"]`)).not.toBeNull();
    expect($<HTMLButtonElement>(ctx, "#import-check").disabled).toBe(true);
    expect($<HTMLButtonElement>(ctx, "#import-create").disabled).toBe(true);
    expect($(ctx, "#import-stop").hidden).toBe(true);
    expect($(ctx, ".import-start-hint").textContent).toBe("Choose a file first.");
    expect($(ctx, "table.import-table").hidden).toBe(true);
  });

  it("a chosen file shows its counts and the comparison with the registry; still nothing is opened", async () => {
    const ctx = await mount();
    button(ctx.root, "Import").click();
    await chooseFile(ctx);
    expect(yieldCalls(ctx)).toHaveLength(1);
    expect(ctx.client.last("yields").params).toEqual({ offset: 0, limit: 500 });
    expect($(ctx, ".import-file-name").textContent).toBe("experiments.csv");
    expect($(ctx, ".import-file-count").textContent).toBe("9 experiments, 11 products");
    expect($(ctx, ".import-plan").textContent).toContain("Comparing the list with the registry");
    expect($<HTMLButtonElement>(ctx, "#import-check").disabled).toBe(true);
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult([yieldRow("4110", 1, "000001_01_001")]) });
    await flush();

    expect(Array.from(ctx.root.querySelectorAll("table.import-table th")).map((t) => t.textContent)).toEqual([
      "ELN number", "Product", "Formula in the list", "Yield in the list", "ID in the registry", "Result", "Notes",
    ]);
    const rows = bodyRows(ctx);
    expect(rows).toHaveLength(11);
    expect(cells(rows[0]!)).toEqual(["ABC-001", "1", "C13H10O", "52.38 %", "000001_01_001", `skipped: ${SKIP_ALL_REGISTERED}`,
      "The list has an isolated yield of 52.38 %; the registry has none stored for 000001_01_001.",
    ]);
    expect(cells(rows[1]!)).toEqual(["ABC-002", "1", "C21H19N", "17.25 %", "—", "will be opened", ""]);
    expect(cells(rows[5]!)).toEqual(["ABC-005", "1", "C22H23N", "0 %", "—", "skipped: no product with a yield in the list", ""]);
    expect(cells(rows[8]!)[3]).toBe("—");
    const link = rows[0]!.querySelector("a")!;
    expect(link.getAttribute("href")).toBe(experimentPageUrl("4110"));
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    // Experiments that will not be opened are quieter.
    const quiet = Array.from(ctx.root.querySelectorAll<HTMLElement>("tbody.import-quiet")).map((b) => b.dataset.ref);
    expect(quiet).toEqual(["4110", "4150", "4170"]);
    expect($(ctx, ".import-plan-line").textContent).toBe("6 experiments of 9 will be opened, 3 skipped. 1 listed product has an ID already.");
    expect($<HTMLButtonElement>(ctx, "#import-check").disabled).toBe(false);
    expect($<HTMLButtonElement>(ctx, "#import-create").disabled).toBe(false);
    expect(ctx.tabs.events).toEqual([]);
    expect(ctx.tabs.sent).toEqual([]);
  });

  it("the options change the preview; with no experiment to open, nothing can start", async () => {
    const ctx = await mount();
    await openWithPlan(ctx);
    const all = $<HTMLInputElement>(ctx, "#import-rule-all");
    expect($<HTMLInputElement>(ctx, "#import-rule-with_yield").checked).toBe(true);
    all.checked = true;
    all.dispatchEvent(new Event("change"));
    expect(ctx.handle.importer.state.options.rule).toBe("all");
    expect(ctx.root.querySelectorAll("tbody.import-quiet")).toHaveLength(0);
    const reopen = $<HTMLInputElement>(ctx, "#import-reopen");
    reopen.checked = true;
    reopen.dispatchEvent(new Event("change"));
    expect(ctx.handle.importer.state.options.reopenRegistered).toBe(true);
    const warnings = $<HTMLInputElement>(ctx, "#import-warnings");
    warnings.checked = true;
    warnings.dispatchEvent(new Event("change"));
    expect(ctx.handle.importer.state.options.allowWarnings).toBe(true);
    for (const id of ["import-reopen", "import-warnings", "import-rule-all"]) {
      expect(ctx.root.querySelector(`label[for="${id}"]`)).not.toBeNull();
    }
    expect(ctx.root.querySelector(`label[for="import-reopen"]`)!.textContent).toBe("Also open experiments whose products all have an ID already");
    expect(ctx.root.querySelector(`label[for="import-warnings"]`)!.textContent).toBe("Also create IDs for structures that have warnings");
    expect(ctx.tabs.events).toEqual([]);

    const none = await (async () => {
      handle!.destroy();
      const again = await mount();
      button(again.root, "Import").click();
      await chooseFile(again, new TextEncoder().encode("pk,Lab notebook entry\r\n1,X-1\r\n"));
      again.client.last("yields").resolve({ ok: true, result: yieldsResult([]) });
      await flush();
      return again;
    })();
    expect($<HTMLButtonElement>(none, "#import-check").disabled).toBe(true);
    expect($(none, ".import-start-hint").textContent).toBe("With these choices no experiment needs to be opened.");
  });

  it("shows rows that are not imported, and a file that is not an experiment list", async () => {
    const ctx = await mount();
    button(ctx.root, "Import").click();
    await chooseFile(ctx, new TextEncoder().encode("pk,Lab notebook entry\r\n1,X-1\r\nabc,X-2\r\n"));
    expect($(ctx, ".import-problems").textContent).toBe("1 row is not imported" + 'Line 3: The pk "abc" is not a number. The row is not imported.');

    await chooseFile(ctx, new TextEncoder().encode("Title\r\nx\r\n"), "other.csv");
    const alert = $(ctx, ".import-file-info .banner.error");
    expect(alert.getAttribute("role")).toBe("alert");
    expect(alert.textContent).toContain('there is no "pk" column.');
    expect(yieldCalls(ctx)).toHaveLength(1); // only for the first file
    expect($<HTMLButtonElement>(ctx, "#import-check").disabled).toBe(true);
    expect($<HTMLButtonElement>(ctx, "#import-create").disabled).toBe(true);
  });

  it("'Create IDs' asks first, naming the numbers; nothing starts before the second click", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const ctx = await mount();
    button(ctx.root, "Import").click();
    const input = $<HTMLInputElement>(ctx, "#import-file");
    Object.defineProperty(input, "files", { value: [new File([FIXTURE], "experiments.csv")], configurable: true });
    input.dispatchEvent(new Event("change"));
    for (let i = 0; i < 20 && yieldCalls(ctx).length === 0; i++) await vi.advanceTimersByTimeAsync(10);
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult([]) });
    await flush();

    const create = $<HTMLButtonElement>(ctx, "#import-create");
    const check = $<HTMLButtonElement>(ctx, "#import-check");
    create.click();
    const confirm = $(ctx, ".import-confirm");
    expect(confirm.hidden).toBe(false);
    expect($(ctx, "#import-confirm-text").textContent).toBe(
      "Create IDs for up to 9 products in 7 experiments? IDs are permanent and cannot be taken back.",
    );
    expect(create.disabled).toBe(true);
    expect(check.disabled).toBe(true);
    expect(document.activeElement).toBe($(ctx, "#import-confirm"));
    await vi.advanceTimersByTimeAsync(5_000);
    expect(yieldCalls(ctx)).toHaveLength(1);
    expect(ctx.tabs.events).toEqual([]);

    $<HTMLButtonElement>(ctx, "#import-cancel").click();
    expect(confirm.hidden).toBe(true);
    expect(create.disabled).toBe(false);
    expect(document.activeElement).toBe(create);

    create.click();
    $<HTMLButtonElement>(ctx, "#import-confirm").click();
    expect(confirm.hidden).toBe(true);
    expect(yieldCalls(ctx)).toHaveLength(2); // the plan is loaded again first
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult([]) });
    await vi.advanceTimersByTimeAsync(100);
    expect(ctx.tabs.events.map((e) => e.op)).toEqual(["create"]);
    expect(check.disabled).toBe(true);
    expect(create.disabled).toBe(true);
    expect($(ctx, "#import-stop").hidden).toBe(false);
    expect(input.disabled).toBe(true);
    expect($<HTMLInputElement>(ctx, "#import-rule-all").disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(ctx.tabs.applies()).toEqual([]);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(ctx.tabs.applies()[0]).toMatchObject({ ref: "4110", dry_run: false, products: [1] });
    handle!.importer.stop();
    await vi.advanceTimersByTimeAsync(10_000);
  });

  it("while running: progress, counters and Stop; at the end: the summary, the report and Copy table", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const tabs = new FakeTabs().page("4180", {
      status: () => ready("4180", [pageProduct(1, 60)], "ABC-080"),
    });
    const ctx = await mount(tabs);
    button(ctx.root, "Import").click();
    const input = $<HTMLInputElement>(ctx, "#import-file");
    Object.defineProperty(input, "files", { value: [new File([FIXTURE], "experiments.csv")], configurable: true });
    input.dispatchEvent(new Event("change"));
    for (let i = 0; i < 20 && yieldCalls(ctx).length === 0; i++) await vi.advanceTimersByTimeAsync(10);
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult([]) });
    await flush();

    $<HTMLButtonElement>(ctx, "#import-check").click();
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult([]) });
    await vi.advanceTimersByTimeAsync(500);
    expect($(ctx, ".import-progress").hidden).toBe(false);
    expect($(ctx, ".import-progress-line").textContent).toBe("Experiment 1 of 7: ABC-001 …");
    const bar = $<HTMLProgressElement>(ctx, "#import-progress");
    expect(bar.max).toBe(7);
    expect(bar.value).toBe(0);
    expect($(ctx, "tbody.import-busy").dataset.ref).toBe("4110");
    expect($<HTMLButtonElement>(ctx, "#import-stop").disabled).toBe(false);
    await vi.advanceTimersByTimeAsync(4_000);
    expect(bar.value).toBe(1);
    expect($(ctx, ".import-counters").textContent).toContain("1 product would get an ID");
    expect($(ctx, ".import-time-left").textContent).toBe("less than a minute left");
    expect(cells(bodyRows(ctx)[0]!)[5]).toBe("would get an ID (new compound)");
    expect($(ctx, ".import-report").hidden).toBe(true);

    for (let i = 0; i < 100 && handle!.importer.isRunning(); i++) await vi.advanceTimersByTimeAsync(1_000);
    expect(handle!.importer.isRunning()).toBe(false);
    expect($(ctx, ".import-progress").hidden).toBe(true);
    expect($(ctx, "#import-stop").hidden).toBe(true);
    expect($<HTMLButtonElement>(ctx, "#import-check").disabled).toBe(false);
    const summary = Array.from(ctx.root.querySelectorAll(".import-summary p")).map((p) => p.textContent);
    expect(summary[0]).toBe("The check is finished. Nothing was created or stored.");
    expect(summary[1]).toContain("9 products would get an ID");
    expect(summary[2]).toMatch(/^9 products would get an ID: 9 new compounds \(new CORE\)/);
    // A finding is marked by words, not only by colour.
    const finding = ctx.root.querySelector<HTMLElement>('tbody[data-ref="4180"] .import-finding')!;
    expect(finding.textContent).toBe("Check: The ELN number on the page is ABC-080; the list says ABC-008.");
    expect(finding.querySelector("strong")!.textContent).toBe("Check: ");

    expect($(ctx, ".import-report").hidden).toBe(false);
    button($(ctx, "#view-import"), "Download report").click();
    expect(ctx.downloads).toHaveLength(1);
    expect(ctx.downloads[0]!.name).toBe(IMPORT_REPORT_NAME);
    expect(ctx.downloads[0]!.name).toBe("import-report.csv");
    expect(ctx.downloads[0]!.type).toBe("text/csv;charset=utf-8");
    expect(ctx.downloads[0]!.text.startsWith("\uFEFFELN number,pk,Product,Result,")).toBe(true);
    button($(ctx, "#view-import"), "Copy table").click();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledWith(handle!.importer.reportTsv());
    expect($(ctx, ".import-report-feedback").textContent).toBe("The table was copied. Paste it into a spreadsheet.");
    expect(ctx.tabs.events.at(-1)!.op).toBe("remove");
  });

  it("Stop says it waits for the current experiment", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const ctx = await mount();
    button(ctx.root, "Import").click();
    const input = $<HTMLInputElement>(ctx, "#import-file");
    Object.defineProperty(input, "files", { value: [new File([FIXTURE], "experiments.csv")], configurable: true });
    input.dispatchEvent(new Event("change"));
    for (let i = 0; i < 20 && yieldCalls(ctx).length === 0; i++) await vi.advanceTimersByTimeAsync(10);
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult([]) });
    await flush();
    $<HTMLButtonElement>(ctx, "#import-check").click();
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult([]) });
    await vi.advanceTimersByTimeAsync(500);
    const stop = $<HTMLButtonElement>(ctx, "#import-stop");
    stop.click();
    expect(stop.disabled).toBe(true);
    expect(stop.textContent).toBe("Stopping after this experiment …");
    await vi.advanceTimersByTimeAsync(5_000);
    expect(handle!.importer.isRunning()).toBe(false);
    expect(Array.from(ctx.root.querySelectorAll(".import-summary p")).map((p) => p.textContent).slice(0, 2)).toEqual([
      "The check ended after 1 of 7 experiments. Nothing was created.",
      "The import was stopped.",
    ]);
    expect(ctx.tabs.applies()).toHaveLength(1);
  });

  it("puts file and host text in as text only", async () => {
    const evil = `<img src=x onerror="window.__pwned=1">`;
    const ctx = await mount();
    button(ctx.root, "Import").click();
    await chooseFile(ctx, new TextEncoder().encode(`pk,Lab notebook entry,Reaction equation (molecular formulas),Yisol (%),,\r\n1,"${evil.replace(/"/g, '""')}",A ---&gt; &lt;b&gt;,5%,,\r\n`));
    ctx.client.last("yields").resolve({ ok: true, result: yieldsResult([]) });
    await flush();
    expect(ctx.root.querySelector("#view-import img, #view-import b")).toBeNull();
    expect(cells(bodyRows(ctx)[0]!).slice(0, 3)).toEqual([evil, "1", "<b>"]);
  });
});
