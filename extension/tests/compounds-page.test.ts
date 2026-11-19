// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StructureEditor } from "../src/compounds/editor";
import { mountCompoundsPage, type CompoundsPageHandle } from "../src/compounds/page";
import { SITE_ORIGIN, type CompoundsParams, type CompoundsResult, type FindResult } from "../src/shared/protocol";
import {
  FakeClient,
  batch,
  compoundItem,
  compoundsResult,
  compoundVariant,
  coreFindResult,
  flush,
  foundVariant,
  nameInfo,
  pingResult,
  setNameResult,
} from "./helpers";

const EVIL = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>`;

class FakeEditor implements StructureEditor {
  molfile: string | null = null;
  cleared = 0;
  destroyed = false;
  getMolfile(): string | null {
    return this.molfile;
  }
  clear(): void {
    this.molfile = null;
    this.cleared += 1;
  }
  destroy(): void {
    this.destroyed = true;
  }
}

let handle: CompoundsPageHandle | null = null;
afterEach(() => {
  handle?.destroy();
  handle = null;
  document.body.replaceChildren();
});

function mount(options: { failEditor?: boolean; extensionVersion?: string | null } = {}) {
  const client = new FakeClient();
  const editor = new FakeEditor();
  const containers: HTMLElement[] = [];
  const root = document.createElement("div");
  document.body.append(root);
  const copyText = vi.fn(async () => {});
  handle = mountCompoundsPage({
    doc: document,
    root,
    client,
    copyText,
    extensionVersion: options.extensionVersion ?? null,
    createEditor: (container) => {
      containers.push(container);
      if (options.failEditor) throw new Error("no canvas");
      return editor;
    },
  });
  return { client, editor, containers, root, copyText, handle };
}
type Ctx = ReturnType<typeof mount>;

const buttons = (root: HTMLElement) => Array.from(root.querySelectorAll("button"));
function button(root: HTMLElement, text: string | RegExp, scope = ""): HTMLButtonElement {
  const all = Array.from(root.querySelectorAll<HTMLButtonElement>(`${scope} button`));
  const found = all.find((b) => (typeof text === "string" ? b.textContent === text : text.test(b.textContent ?? "")));
  if (!found) throw new Error(`no button ${text}; have: ${all.map((b) => b.textContent).join(" | ")}`);
  return found;
}
const text = (root: HTMLElement, selector: string) => root.querySelector(selector)?.textContent ?? "";

async function answerStart(ctx: Ctx, result: CompoundsResult = compoundsResult()) {
  ctx.client.last("ping").resolve({ ok: true, result: pingResult({ stats: { cores: 1, stereos: 1, batches: 3, retired: 1 } }) });
  ctx.client.last("compounds").resolve({ ok: true, result });
  await flush();
}

function searchFor(ctx: Ctx, value: string) {
  const input = ctx.root.querySelector<HTMLInputElement>("#search-text")!;
  input.value = value;
  button(ctx.root, "Search", ".search-form").click();
}

async function answerList(ctx: Ctx, result: CompoundsResult) {
  ctx.client.last("compounds").resolve({ ok: true, result });
  await flush();
}

const lastParams = (ctx: Ctx) => ctx.client.last("compounds").params as CompoundsParams;

describe("compounds page: list", () => {
  it("pings, loads the first page, and shows the registry in use", async () => {
    const ctx = mount();
    expect(ctx.client.callsOf("ping")).toHaveLength(1);
    expect(lastParams(ctx)).toEqual({ order: "asc", offset: 0, limit: 24 });
    expect(text(ctx.root, ".result-line")).toBe("Loading compounds…");
    expect(ctx.root.querySelector("ol.compounds")!.getAttribute("aria-busy")).toBe("true");
    await answerStart(ctx);
    expect(text(ctx.root, ".registry")).toContain("registry.sqlite");
    expect(text(ctx.root, ".registry")).toContain("1 compound (CORE) · 1 stereo variant · 3 batches (1 retired)");
    expect(text(ctx.root, ".result-line")).toBe("1 compound.");
    expect(ctx.root.querySelector("ol.compounds")!.getAttribute("aria-busy")).toBe("false");
    expect(text(ctx.root, "[role=status]")).toBe("1 compound.");
  });

  it("shows one entry per CORE with thumbnail, ID, name, formula, molar mass, counts and experiments", async () => {
    const ctx = mount();
    await answerStart(
      ctx,
      compoundsResult({
        items: [
          compoundItem({
            formula: "C2H8ClNO",
            properties: { sum_formula: "C2H8ClNO", mol_weight: 97.545, exact_mass: 97.02944, composition: [] },
            variants: [compoundVariant(), compoundVariant({ core_stereo_id: "000042_02", stereo_no: 2, name: nameInfo("(2R)-2-aminopropan-1-ol") })],
            batch_count: 3,
            experiment_count: 12,
            experiments: [
              { experiment_ref: "1", experiment_code: "ABC-001", experiment_url: `${SITE_ORIGIN}/experiment/1` },
              { experiment_ref: "2", experiment_code: "ABC-002", experiment_url: "https://evil.example/2" },
            ],
          }),
        ],
      }),
    );
    const entry = ctx.root.querySelector("li.compound")!;
    const img = entry.querySelector("img")!;
    expect(img.getAttribute("src")!.startsWith("data:image/svg+xml;charset=utf-8,")).toBe(true);
    expect(img.getAttribute("alt")).toBe("Structure of 000042");
    expect(entry.querySelector("h3")!.textContent).toBe("000042");
    expect(entry.textContent).toContain("(2R)-2-aminopropan-1-ol");
    expect(entry.textContent).toContain("C2H8ClNO · 97.55 g/mol");
    expect(entry.textContent).toContain("2 stereo variants · 3 batches · 12 experiments");
    expect(entry.textContent).toContain("Made in: ABC-001, ABC-002 +10 more");
    const links = Array.from(entry.querySelectorAll("a"));
    expect(links.map((a) => a.textContent)).toEqual(["ABC-001"]);
    expect(links[0]!.getAttribute("target")).toBe("_blank");
    expect(links[0]!.getAttribute("rel")).toBe("noopener noreferrer");
    expect(entry.textContent).not.toContain("Matched by"); // no search
  });

  it("says when the registry is empty", async () => {
    const ctx = mount();
    await answerStart(ctx, compoundsResult({ items: [], total: 0, registry_total: 0 }));
    expect(text(ctx.root, ".result-line")).toBe("The registry has no compounds yet.");
    expect(ctx.root.querySelectorAll("li.compound")).toHaveLength(0);
  });

  it("orders newest first on request", async () => {
    const ctx = mount();
    await answerStart(ctx);
    const select = ctx.root.querySelector<HTMLSelectElement>("#order")!;
    expect(ctx.root.querySelector(`label[for="order"]`)).not.toBeNull();
    select.value = "desc";
    select.dispatchEvent(new Event("change"));
    expect(lastParams(ctx)).toEqual({ order: "desc", offset: 0, limit: 24 });
  });

  it("pages with offset and limit", async () => {
    const ctx = mount();
    const page = (offset: number) =>
      compoundsResult({ total: 50, registry_total: 50, offset, items: Array.from({ length: offset === 48 ? 2 : 24 }, (_, i) => compoundItem({ core_id: String(offset + i).padStart(6, "0") })) });
    await answerStart(ctx, page(0));
    expect(text(ctx.root, ".pager")).toContain("Page 1 of 3");
    expect(text(ctx.root, ".result-line")).toBe("50 compounds. Showing 1–24.");
    expect(button(ctx.root, "Previous page").disabled).toBe(true);
    button(ctx.root, "Next page").click();
    expect(lastParams(ctx)).toEqual({ order: "asc", offset: 24, limit: 24 });
    expect(button(ctx.root, "Next page").disabled).toBe(true); // while loading
    await answerList(ctx, page(24));
    button(ctx.root, "Next page").click();
    await answerList(ctx, page(48));
    expect(text(ctx.root, ".pager")).toContain("Page 3 of 3");
    expect(button(ctx.root, "Next page").disabled).toBe(true);
    button(ctx.root, "Previous page").click();
    expect(lastParams(ctx).offset).toBe(24);
  });

  it("uses only the answer to the latest request", async () => {
    const ctx = mount();
    await answerStart(ctx);
    searchFor(ctx, "first");
    const first = ctx.client.last("compounds");
    searchFor(ctx, "second");
    await answerList(ctx, compoundsResult({ items: [compoundItem({ core_id: "000002" })], total: 1, registry_total: 9 }));
    first.resolve({ ok: true, result: compoundsResult({ items: [compoundItem({ core_id: "000001" })] }) });
    await flush();
    expect(Array.from(ctx.root.querySelectorAll("li.compound h3")).map((h) => h.textContent)).toEqual(["000002"]);
  });
});

describe("compounds page: text search", () => {
  it("has a labelled search field naming what it accepts", () => {
    const ctx = mount();
    const input = ctx.root.querySelector<HTMLInputElement>("#search-text")!;
    expect(ctx.root.querySelector(`label[for="search-text"]`)!.textContent).toBe("Search text");
    expect(input.placeholder).toBe("ID, SMILES, InChI, InChIKey, name, formula or ELN number");
    expect(ctx.root.querySelector("form")!.getAttribute("role")).toBe("search");
  });

  it("shows example IDs in the registry's ID format once the ping has answered", async () => {
    const ctx = mount();
    expect(text(ctx.root, "#search-hint")).toContain("An ID (000042, 000042_02, 000042_02_003)");
    ctx.client.last("ping").resolve({ ok: true, result: pingResult({ id_format: "EX{core:04d}_{stereo:02d}_{batch:03d}" }) });
    ctx.client.last("compounds").resolve({ ok: true, result: compoundsResult() });
    await flush();
    expect(text(ctx.root, "#search-hint")).toContain("An ID (EX0042, EX0042_02, EX0042_02_003)");
  });

  it("searches the trimmed text, shows why each CORE matched, notes and warnings", async () => {
    const ctx = mount();
    await answerStart(ctx, compoundsResult({ total: 340, registry_total: 340 }));
    searchFor(ctx, "  CCO  ");
    expect(lastParams(ctx)).toEqual({ text: "CCO", order: "asc", offset: 0, limit: 24 });
    await answerList(
      ctx,
      compoundsResult({
        total: 1,
        registry_total: 340,
        items: [compoundItem({ matched_by: ["structure", "name"] })],
        notes: ["Read as SMILES: CCO"],
        warnings: ["The registry is in a sync folder."],
      }),
    );
    expect(text(ctx.root, ".result-line")).toBe("1 of 340 compounds matches the search.");
    expect(text(ctx.root, ".query-line")).toBe("Search: text “CCO”");
    expect(text(ctx.root, ".messages")).toContain("Read as SMILES: CCO");
    expect(text(ctx.root, ".messages")).toContain("The registry is in a sync folder.");
    expect(text(ctx.root, "li.compound")).toContain("Matched by structure (same constitution) and name");
  });

  it("says when nothing matches, and Clear shows everything again", async () => {
    const ctx = mount();
    await answerStart(ctx);
    searchFor(ctx, "XYZ-999");
    await answerList(ctx, compoundsResult({ items: [], total: 0, registry_total: 5, notes: ["Not an ID, InChIKey, SMILES or InChI."] }));
    expect(text(ctx.root, ".result-line")).toBe("No compound matches this search.");
    expect(text(ctx.root, ".messages")).toContain("Not an ID, InChIKey, SMILES or InChI.");
    button(ctx.root, "Clear").click();
    expect(ctx.root.querySelector<HTMLInputElement>("#search-text")!.value).toBe("");
    expect(lastParams(ctx)).toEqual({ order: "asc", offset: 0, limit: 24 });
    expect(document.activeElement).toBe(ctx.root.querySelector("#search-text"));
  });
});

describe("compounds page: drawn structure", () => {
  it("creates the editor once, when the drawing area is first opened", () => {
    const ctx = mount();
    const toggle = button(ctx.root, "Draw structure");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(ctx.containers).toHaveLength(0);
    toggle.click();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(ctx.root.querySelector(`#${toggle.getAttribute("aria-controls")}`)!.hasAttribute("hidden")).toBe(false);
    expect(ctx.containers).toHaveLength(1);
    expect(ctx.containers[0]!.getAttribute("aria-label")).toMatch(/Structure editor/);
    toggle.click();
    toggle.click();
    expect(ctx.containers).toHaveLength(1);
  });

  it("sends the drawing as molfile with the chosen match, combined with the text", async () => {
    const ctx = mount();
    await answerStart(ctx);
    button(ctx.root, "Draw structure").click();
    ctx.editor.molfile = "drawn\nM  END\n";
    ctx.root.querySelector<HTMLInputElement>("#search-text")!.value = "ABC-";
    ctx.root.querySelector<HTMLInputElement>("#match-exact")!.checked = true;
    button(ctx.root, "Search", ".draw-area").click();
    expect(lastParams(ctx)).toEqual({ text: "ABC-", molfile: "drawn\nM  END\n", match: "exact", order: "asc", offset: 0, limit: 24 });
    await answerList(ctx, compoundsResult({ items: [compoundItem({ matched_by: ["experiment", "drawn"] })] }));
    expect(text(ctx.root, ".query-line")).toBe("Search: text “ABC-” and a drawn structure (exact: same constitution)");
    // The text search button also includes the drawing while the area is open.
    ctx.root.querySelector<HTMLInputElement>("#match-substructure")!.checked = true;
    button(ctx.root, "Search", ".search-form").click();
    expect(lastParams(ctx)).toMatchObject({ molfile: "drawn\nM  END\n", match: "substructure" });
  });

  it("asks for a drawing first and sends nothing without one", async () => {
    const ctx = mount();
    await answerStart(ctx);
    const before = ctx.client.callsOf("compounds").length;
    button(ctx.root, "Draw structure").click();
    button(ctx.root, "Search", ".draw-area").click();
    expect(ctx.client.callsOf("compounds")).toHaveLength(before);
    expect(text(ctx.root, ".editor-message")).toBe("Draw a structure first.");
  });

  it("Clear drawing clears the editor and searches again without it", async () => {
    const ctx = mount();
    await answerStart(ctx);
    button(ctx.root, "Draw structure").click();
    ctx.editor.molfile = "M";
    button(ctx.root, "Search", ".draw-area").click();
    await answerList(ctx, compoundsResult());
    button(ctx.root, "Clear drawing").click();
    expect(ctx.editor.cleared).toBe(1);
    expect(lastParams(ctx)).toEqual({ order: "asc", offset: 0, limit: 24 });
  });

  it("leaves a hidden drawing out of the text search", async () => {
    const ctx = mount();
    await answerStart(ctx);
    button(ctx.root, "Draw structure").click();
    ctx.editor.molfile = "M";
    button(ctx.root, "Draw structure").click(); // closed again
    searchFor(ctx, "x");
    expect(lastParams(ctx)).toEqual({ text: "x", order: "asc", offset: 0, limit: 24 });
  });

  it("refuses a drawing that is too large", async () => {
    const ctx = mount();
    await answerStart(ctx);
    button(ctx.root, "Draw structure").click();
    ctx.editor.molfile = "x".repeat(500_001);
    const before = ctx.client.callsOf("compounds").length;
    button(ctx.root, "Search", ".draw-area").click();
    expect(ctx.client.callsOf("compounds")).toHaveLength(before);
    expect(text(ctx.root, ".editor-message")).toBe("The drawing is too large to search.");
  });

  it("explains an editor that cannot start, and the text search still works", async () => {
    const ctx = mount({ failEditor: true });
    await answerStart(ctx);
    button(ctx.root, "Draw structure").click();
    expect(text(ctx.root, ".editor-message")).toContain("The structure editor could not be started: Error: no canvas");
    searchFor(ctx, "CCO");
    expect(lastParams(ctx)).toEqual({ text: "CCO", order: "asc", offset: 0, limit: 24 });
  });
});

describe("compounds page: host problems", () => {
  it("explains an unreachable host once and retries ping and list", async () => {
    const ctx = mount();
    const down = { ok: false as const, error: { code: "host_unavailable", message: "No such native application compound_registry" } };
    ctx.client.last("ping").resolve(down);
    ctx.client.last("compounds").resolve(down);
    await flush();
    expect(text(ctx.root, ".registry")).toContain("Registry program not reachable");
    expect(text(ctx.root, ".registry")).toContain("install.ps1 on Windows, install.sh on macOS/Linux");
    expect(text(ctx.root, ".registry")).toContain("No such native application");
    expect(ctx.root.textContent!.split("Registry program not reachable").length - 1).toBe(1);
    expect(text(ctx.root, ".result-line")).toBe("The compounds could not be loaded.");
    button(ctx.root, "Retry").click();
    expect(ctx.client.callsOf("ping")).toHaveLength(2);
    expect(ctx.client.callsOf("compounds")).toHaveLength(2);
  });

  it("shows a list error with Retry when the registry answers ping", async () => {
    const ctx = mount();
    ctx.client.last("ping").resolve({ ok: true, result: pingResult() });
    ctx.client.last("compounds").resolve({ ok: false, error: { code: "storage_error", message: "database is locked" } });
    await flush();
    expect(text(ctx.root, ".messages")).toContain("The registry database could not be read or written");
    expect(text(ctx.root, ".messages")).toContain("database is locked");
    button(ctx.root, "Retry").click();
    expect(ctx.client.callsOf("compounds")).toHaveLength(2);
  });

  it("explains a protocol mismatch", async () => {
    const ctx = mount();
    ctx.client.last("ping").resolve({ ok: true, result: { protocol_version: 2, host_version: "9.0" } as never });
    await flush();
    expect(text(ctx.root, ".registry")).toMatch(/protocol version 2, this extension version 1/);
    expect(text(ctx.root, ".registry")).toContain("update.ps1 on Windows, update.sh on macOS/Linux");
  });

  it("shows the versions of the extension and the registry program", async () => {
    const ctx = mount({ extensionVersion: "0.10.0" });
    expect(text(ctx.root, ".registry .versions")).toBe("Extension 0.10.0");
    ctx.client.last("ping").resolve({ ok: true, result: pingResult({ host_version: "0.10.2" }) });
    await flush();
    expect(text(ctx.root, ".registry .versions")).toBe("Extension 0.10.0 · registry program 0.10.2");
    expect(ctx.root.querySelector(".version-notice")).toBeNull();
  });

  it("warns when the registry program is from another release, and still lists the compounds", async () => {
    const ctx = mount({ extensionVersion: "0.11.0" });
    ctx.client.last("ping").resolve({ ok: true, result: pingResult({ host_version: "0.10.2" }) });
    await flush();
    const notice = text(ctx.root, ".registry .version-notice");
    expect(notice).toContain("Extension and registry program are from different releases");
    expect(notice).toContain("The extension is version 0.11.0, the registry program version 0.10.2");
    expect(notice).toContain("update.ps1 on Windows, update.sh on macOS/Linux");
    expect(notice).toContain("reload the extension");
    expect(text(ctx.root, ".registry .versions")).toBe("Extension 0.11.0 · registry program 0.10.2");
    expect(buttons(ctx.root).some((b) => b.textContent === "Retry")).toBe(false);
    expect(ctx.client.callsOf("compounds")).toHaveLength(1);
  });

  it("after registry_changed pings again and reloads the list once", async () => {
    const ctx = mount();
    await answerStart(ctx);
    searchFor(ctx, "CCO");
    ctx.client.last("compounds").resolve({ ok: false, error: { code: "registry_changed", message: "The configuration changed." } });
    await flush();
    expect(ctx.client.callsOf("ping")).toHaveLength(2);
    expect(ctx.client.callsOf("compounds")).toHaveLength(3);
    expect(lastParams(ctx)).toEqual({ text: "CCO", order: "asc", offset: 0, limit: 24 });
    ctx.client.last("ping").resolve({ ok: true, result: pingResult({ db_path: "D:\\Other\\registry.sqlite" }) });
    await answerList(ctx, compoundsResult());
    expect(text(ctx.root, ".registry")).toContain("D:\\Other\\registry.sqlite");
    expect(text(ctx.root, ".messages")).toContain("The registry configuration changed");
    // Refused the same way again: shown with Retry, no endless reloading.
    searchFor(ctx, "x");
    ctx.client.last("compounds").resolve({ ok: false, error: { code: "registry_changed", message: "again" } });
    await flush();
    ctx.client.last("compounds").resolve({ ok: false, error: { code: "registry_changed", message: "and again" } });
    await flush();
    expect(ctx.client.callsOf("compounds")).toHaveLength(5);
    expect(text(ctx.root, ".messages")).toContain("and again");
    expect(button(ctx.root, "Retry")).toBeTruthy();
  });
});

describe("compounds page: stereo variants", () => {
  function twoVariants(): FindResult {
    return coreFindResult({
      warnings: ["Chemistry version changed."],
      variants: [
        foundVariant({
          name: nameInfo("(2S)-2-aminopropan-1-ol"),
          batches: [
            batch({ full_id: "000042_01_001", experiment_code: "ABC-001", experiment_url: `${SITE_ORIGIN}/experiment/1` }),
            batch({ full_id: "000042_01_002", experiment_code: "ABC-002", experiment_url: "javascript:alert(1)", retired: true, retired_reason: "wrong" }),
          ],
        }),
        foundVariant({ core_stereo_id: "000042_02", stereo_no: 2, stereo_mode: "rac", name: null, inchikey: "KEY-TWO-N", batches: [] }),
      ],
    });
  }

  async function expanded(searched = false) {
    const ctx = mount();
    await answerStart(ctx);
    if (searched) {
      searchFor(ctx, "aminopropan");
      await answerList(
        ctx,
        compoundsResult({
          items: [
            compoundItem({
              matched_by: ["name"],
              variants: [compoundVariant({ matched: true }), compoundVariant({ core_stereo_id: "000042_02", stereo_no: 2, matched: false })],
            }),
          ],
        }),
      );
    }
    const toggle = button(ctx.root, "Show stereo variants and details");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    toggle.click();
    expect(ctx.client.last("find").params).toEqual({ id: "000042" });
    expect(text(ctx.root, "li.compound")).toContain("Loading the stereo variants…");
    ctx.client.last("find").resolve({ ok: true, result: twoVariants() });
    await flush();
    return ctx;
  }

  it("loads the variants of the CORE and shows each with names, identifiers, properties and batches", async () => {
    const ctx = await expanded();
    const toggle = button(ctx.root, "Hide stereo variants and details");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const region = ctx.root.querySelector(`#${toggle.getAttribute("aria-controls")}`)!;
    const blocks = Array.from(region.querySelectorAll(".variant-block"));
    expect(blocks.map((b) => b.querySelector("h4")!.textContent)).toEqual(["000042_01", "000042_02"]);
    expect(blocks[1]!.textContent).toContain("Racemic");
    expect(region.textContent).toContain("Chemistry version changed.");
    const first = blocks[0]!;
    const terms = Array.from(first.querySelectorAll("dl.details dt")).map((d) => d.textContent);
    expect(terms).toEqual(["IUPAC name", "Formula", "Molar mass", "Exact mass", "Elemental analysis", "SMILES", "InChI", "InChIKey", "Stereo"]);
    expect(first.textContent).toContain("(2S)-2-aminopropan-1-ol");
    expect(first.textContent).toContain("61.08 g/mol");
    expect(first.textContent).toContain("61.0528");
    expect(Array.from(first.querySelectorAll("a")).map((a) => a.textContent)).toEqual(["ABC-001"]);
    expect(first.textContent).toContain("ABC-002"); // unsafe link shown as text
    expect(first.textContent).toContain("retired: wrong");
    expect(blocks[1]!.textContent).toContain("No name stored");
    expect(blocks[1]!.textContent).toContain("No batch is recorded.");
    for (const img of Array.from(region.querySelectorAll("img"))) {
      expect(img.getAttribute("src")!.startsWith("data:image/svg+xml;charset=utf-8,")).toBe(true);
    }
    toggle.click();
    expect(ctx.root.querySelector(".variant-block")).toBeNull();
    expect(button(ctx.root, "Show stereo variants and details").getAttribute("aria-expanded")).toBe("false");
  });

  it("marks the variants that matched the search", async () => {
    const ctx = await expanded(true);
    const blocks = Array.from(ctx.root.querySelectorAll(".variant-block"));
    expect(blocks.map((b) => b.classList.contains("matched"))).toEqual([true, false]);
    expect(blocks[0]!.textContent).toContain("Matches the search");
    expect(blocks[1]!.textContent).not.toContain("Matches the search");
  });

  it("copies identifiers", async () => {
    const ctx = await expanded();
    const copy = buttons(ctx.root).find((b) => b.getAttribute("aria-label") === "Copy InChIKey of 000042_02")!;
    copy.click();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledWith("KEY-TWO-N");
  });

  it("stores a name with the shared editor and shows it in the entry and the variant", async () => {
    const ctx = await expanded();
    const block = () => Array.from(ctx.root.querySelectorAll(".variant-block"))[1]!;
    (Array.from(block().querySelectorAll("button")).find((b) => b.textContent === "Add name")!).click();
    const input = block().querySelector<HTMLInputElement>(".name-row input")!;
    expect(document.activeElement).toBe(input);
    input.value = "(2RS)-2-aminopropan-1-ol";
    input.dispatchEvent(new Event("input"));
    (Array.from(block().querySelectorAll("button")).find((b) => b.textContent === "Save")!).click();
    expect(ctx.client.last("set_name").params).toEqual({ core_stereo_id: "000042_02", name: "(2RS)-2-aminopropan-1-ol" });
    ctx.client.last("set_name").resolve({
      ok: true,
      result: setNameResult({ core_stereo_id: "000042_02", name: nameInfo("(2RS)-2-aminopropan-1-ol"), warnings: ["Backup is old."] }),
    });
    await flush();
    expect(block().querySelector(".name-text")!.textContent).toBe("(2RS)-2-aminopropan-1-ol");
    expect(block().textContent).toContain("Backup is old.");
    expect(document.activeElement).toBe(Array.from(block().querySelectorAll("button")).find((b) => b.textContent === "Edit"));
  });

  it("shows the collapsed entry's name from the list after a name is stored", async () => {
    const ctx = mount();
    await answerStart(ctx);
    expect(text(ctx.root, "li.compound")).toContain("No name stored");
    ctx.handle.controller.nameStored(setNameResult({ core_stereo_id: "000042_01", name: nameInfo("stored name") }));
    expect(text(ctx.root, ".compound-name")).toBe("stored name");
  });

  it("explains a failed load and tries again", async () => {
    const ctx = mount();
    await answerStart(ctx);
    button(ctx.root, "Show stereo variants and details").click();
    ctx.client.last("find").resolve({ ok: false, error: { code: "not_found", message: "No compound 000042." } });
    await flush();
    expect(text(ctx.root, "li.compound")).toContain("No compound 000042.");
    button(ctx.root, "Try again").click();
    expect(ctx.client.callsOf("find")).toHaveLength(2);
  });

  it("drops details requested before a new search", async () => {
    const ctx = mount();
    await answerStart(ctx);
    button(ctx.root, "Show stereo variants and details").click();
    const find = ctx.client.last("find");
    searchFor(ctx, "x");
    await answerList(ctx, compoundsResult());
    find.resolve({ ok: true, result: coreFindResult() });
    await flush();
    expect(ctx.root.querySelector(".variant-block")).toBeNull();
  });

  it("an older host without names shows no name row", async () => {
    const ctx = mount();
    await answerStart(ctx);
    button(ctx.root, "Show stereo variants and details").click();
    const old = foundVariant();
    delete old.name;
    delete old.inchi;
    delete old.properties;
    ctx.client.last("find").resolve({ ok: true, result: coreFindResult({ variants: [old] }) });
    await flush();
    const terms = Array.from(ctx.root.querySelectorAll(".variant-block dl.details dt")).map((d) => d.textContent);
    expect(terms).toEqual(["Formula", "SMILES", "InChIKey", "Stereo"]);
  });
});

describe("compounds page: text only", () => {
  it("puts host text into the DOM only as text", async () => {
    (window as unknown as Record<string, unknown>).__pwned = 0;
    const ctx = mount();
    ctx.client.last("ping").resolve({ ok: true, result: pingResult({ db_path: EVIL, warnings: [EVIL] }) });
    await answerList(
      ctx,
      compoundsResult({
        notes: [EVIL],
        warnings: [EVIL],
        items: [
          compoundItem({
            core_id: "000042",
            formula: EVIL,
            svg: EVIL,
            matched_by: [EVIL as never],
            variants: [compoundVariant({ name: nameInfo(EVIL) })],
            experiments: [{ experiment_ref: EVIL, experiment_code: EVIL, experiment_url: `javascript:${EVIL}` }],
          }),
        ],
      }),
    );
    searchFor(ctx, EVIL);
    await answerList(ctx, compoundsResult({ notes: [EVIL], items: [compoundItem({ matched_by: [EVIL as never], variants: [compoundVariant({ name: nameInfo(EVIL) })] })] }));
    button(ctx.root, "Show stereo variants and details").click();
    ctx.client.last("find").resolve({
      ok: true,
      result: coreFindResult({ warnings: [EVIL], variants: [foundVariant({ name: nameInfo(EVIL), smiles: EVIL, inchi: EVIL, svg: EVIL, batches: [batch({ experiment_code: EVIL })] })] }),
    });
    await flush();
    expect(ctx.root.querySelector("script")).toBeNull();
    expect(ctx.root.querySelectorAll("a")).toHaveLength(0);
    for (const img of Array.from(ctx.root.querySelectorAll("img"))) {
      expect(img.getAttribute("src")!.startsWith("data:image/svg+xml;charset=utf-8,")).toBe(true);
      expect(img.getAttribute("onerror")).toBeNull();
    }
    expect(ctx.root.textContent!.split(EVIL).length - 1).toBeGreaterThanOrEqual(8);
    expect((window as unknown as Record<string, unknown>).__pwned).toBe(0);
  });
});
