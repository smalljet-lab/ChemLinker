// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StructureEditor } from "../src/compounds/editor";
import { mountCompoundsPage, type CompoundsPageHandle } from "../src/compounds/page";
import { autoOptionLabel, candidateLabel, CDXML_TYPE, choiceLabel, folderOf, formatEq, openedLine, stepsLine } from "../src/compounds/route";
import { createRuntimeHostClient } from "../src/content/host-client";
import { isResultOf, isRouteChoice, isRouteResult, isSetLimitingResult, isSetReactionResult } from "../src/content/validate";
import {
  MAX_ROUTE_CDXML_CHARS,
  PROTOCOL_VERSION,
  type FindResult,
  type RouteChoice,
  type RouteParams,
  type RouteResult,
  type SetLimitingParams,
  type SetLimitingResult,
} from "../src/shared/protocol";
import { FakeClient, compoundItem, compoundsResult, compoundVariant, coreFindResult, flush, foundVariant, pingResult } from "./helpers";

const EVIL = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>`;
const SITE = "https://sciformation.chemie.unibas.ch";

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

function mount(options: { defaultDownload?: boolean } = {}) {
  const client = new FakeClient();
  const root = document.createElement("div");
  document.body.append(root);
  const downloads: { name: string; text: string; type: string }[] = [];
  handle = mountCompoundsPage({
    doc: document,
    root,
    client,
    copyText: vi.fn(async () => {}),
    createEditor: () => new NoEditor(),
    ...(options.defaultDownload ? {} : { downloadText: (name: string, text: string, type: string) => downloads.push({ name, text, type }) }),
  });
  return { client, root, downloads };
}
type Ctx = ReturnType<typeof mount>;

function button(scope: ParentNode, label: string | RegExp): HTMLButtonElement {
  const all = Array.from(scope.querySelectorAll<HTMLButtonElement>("button"));
  const found = all.find((b) => (typeof label === "string" ? b.textContent === label : label.test(b.textContent ?? "")));
  if (!found) throw new Error(`no button ${label}; have: ${all.map((b) => b.textContent).join(" | ")}`);
  return found;
}

function twoVariants(): FindResult {
  return coreFindResult({
    variants: [
      foundVariant({ core_stereo_id: "EX0004_01" }),
      foundVariant({ core_stereo_id: "EX0004_02", stereo_no: 2, stereo_mode: "rac", inchikey: "KEY-TWO-N", batches: [] }),
    ],
  });
}

async function shown(ctx: Ctx) {
  ctx.client.last("ping").resolve({ ok: true, result: pingResult() });
  ctx.client.last("compounds").resolve({
    ok: true,
    result: compoundsResult({
      items: [compoundItem({ variants: [compoundVariant({ core_stereo_id: "EX0004_01" }), compoundVariant({ core_stereo_id: "EX0004_02", stereo_no: 2 })] })],
    }),
  });
  await flush();
  button(ctx.root, "Show stereo variants and details").click();
  ctx.client.last("find").resolve({ ok: true, result: twoVariants() });
  await flush();
}

const section = (ctx: Ctx, id = "EX0004_01") => ctx.root.querySelector<HTMLElement>(`.route-section[data-core-stereo="${id}"]`)!;

function routeResult(over: Partial<RouteResult> = {}): RouteResult {
  return {
    core_stereo_id: "EX0004_01",
    file: { path: "C:\\Registry\\compounds\\EX0004_01\\EX0004_01_route_2026-11-19_101500.cdxml", name: "EX0004_01_route_2026-11-19_101500.cdxml" },
    opened: true,
    open_message: null,
    steps: 4,
    experiments: ["ABC-001", "ABC-002", "ABC-004", "ABC-003", "ABC-006", "ABC-008"],
    missing: [],
    notes: ["ABC-002 product 2 (EX0001_01) is recovered starting material: left out."],
    choices: [],
    cdxml: "<?xml version=\"1.0\"?><CDXML></CDXML>",
    warnings: [],
    ...over,
  };
}

async function generate(ctx: Ctx, result: RouteResult, id = "EX0004_01") {
  button(section(ctx, id), "Generate ChemDraw tree").click();
  ctx.client.last("route").resolve({ ok: true, result });
  await flush();
}

describe("compounds page: synthesis route", () => {
  it("offers 'Generate ChemDraw tree' in every stereo variant, and asks nothing before the click", async () => {
    const ctx = mount();
    await shown(ctx);
    for (const id of ["EX0004_01", "EX0004_02"]) {
      const s = section(ctx, id);
      expect(s.querySelector("h5")!.textContent).toBe("Synthesis route");
      expect(button(s, "Generate ChemDraw tree").disabled).toBe(false);
    }
    expect(ctx.client.callsOf("route")).toHaveLength(0);
  });

  it("asks the host to draw and open it; the button waits while it runs", async () => {
    const ctx = mount();
    await shown(ctx);
    const generateButton = button(section(ctx), "Generate ChemDraw tree");
    generateButton.click();
    expect(ctx.client.callsOf("route")).toHaveLength(1);
    expect(ctx.client.last("route").params as RouteParams).toEqual({ core_stereo_id: "EX0004_01", open: true });
    const busy = button(section(ctx), "Generate ChemDraw tree");
    expect(busy.disabled).toBe(true);
    expect(section(ctx).textContent).toContain("Drawing the route…");
    busy.click();
    expect(ctx.client.callsOf("route")).toHaveLength(1);
    // The other variant is not affected.
    expect(button(section(ctx, "EX0004_02"), "Generate ChemDraw tree").disabled).toBe(false);
  });

  it("shows the file opened, its folder, the steps, the notes, and saves a copy", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, routeResult());
    const s = section(ctx);
    expect(s.textContent).not.toContain("Drawing the route…");
    expect(button(s, "Generate ChemDraw tree").disabled).toBe(false);
    expect(s.querySelector(".route-opened")!.textContent).toBe("Opened in ChemDraw: EX0004_01_route_2026-11-19_101500.cdxml");
    expect(s.querySelector(".route-folder .folder-path")!.textContent).toBe("C:\\Registry\\compounds\\EX0004_01");
    expect(s.querySelector(".route-steps")!.textContent).toBe("Steps: 4 (ABC-001, ABC-002, ABC-004, ABC-003, ABC-006, ABC-008)");
    expect(s.querySelector(".route-notes")!.textContent).toContain("recovered starting material");
    expect(s.querySelector(".route-missing")).toBeNull();
    button(s, "Save a copy").click();
    expect(ctx.downloads).toEqual([
      { name: "EX0004_01_route_2026-11-19_101500.cdxml", text: "<?xml version=\"1.0\"?><CDXML></CDXML>", type: "chemical/x-cdxml" },
    ]);
  });

  it("has no 'Save a copy' when the document text is not sent", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, routeResult({ cdxml: null }));
    expect(Array.from(section(ctx).querySelectorAll("button")).map((b) => b.textContent)).not.toContain("Save a copy");
  });

  it("says why the file was not opened", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, routeResult({ opened: false, open_message: "ChemDraw is not installed on this PC: the file was only written." }));
    expect(section(ctx).querySelector(".route-opened")!.textContent).toBe("ChemDraw is not installed on this PC: the file was only written.");
  });

  it("lists the reactions not stored yet, linking only experiments on the ELN site, in a new tab", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(
      ctx,
      routeResult({
        steps: 2,
        experiments: ["ABC-001", "ABC-008"],
        missing: [
          { experiment_ref: "4121", experiment_code: "ABC-003", experiment_url: `${SITE}/startUseCase?useCase=performSearch&table=ElnReaction&pk=4121&viewMode=1`, compound: "EX0003_01" },
          { experiment_ref: "4122", experiment_code: "ABC-006", experiment_url: "https://evil.example/x", compound: "EX0003_01" },
          { experiment_ref: "4123", experiment_code: null, experiment_url: null, compound: "EX0002_01" },
        ],
      }),
    );
    const s = section(ctx);
    expect(s.querySelector(".route-missing-head")!.textContent).toBe("Reactions not stored yet");
    const items = Array.from(s.querySelectorAll(".route-missing li"));
    expect(items.map((li) => li.textContent)).toEqual([
      "ABC-003 (makes EX0003_01) - open it once in the ELN",
      "ABC-006 (makes EX0003_01) - open it once in the ELN",
      "Experiment 4123 (makes EX0002_01) - open it once in the ELN",
    ]);
    const links = s.querySelectorAll<HTMLAnchorElement>(".route-missing a");
    expect(links).toHaveLength(1);
    expect(links[0]!.textContent).toBe("ABC-003");
    expect(links[0]!.getAttribute("href")).toBe(`${SITE}/startUseCase?useCase=performSearch&table=ElnReaction&pk=4121&viewMode=1`);
    expect(links[0]!.target).toBe("_blank");
    expect(links[0]!.rel).toContain("noopener");
  });

  it("says 'Nothing to draw yet.' with the missing list when no step can be drawn", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(
      ctx,
      routeResult({
        file: null,
        opened: false,
        steps: 0,
        experiments: [],
        cdxml: null,
        notes: [],
        missing: [{ experiment_ref: "4180", experiment_code: "ABC-008", experiment_url: null, compound: "EX0004_01" }],
      }),
    );
    const s = section(ctx);
    expect(s.querySelector(".route-nothing")!.textContent).toBe("Nothing to draw yet.");
    expect(s.querySelector(".route-opened")).toBeNull();
    expect(s.querySelector(".route-steps")).toBeNull();
    expect(s.querySelector(".route-missing li")!.textContent).toBe("ABC-008 (makes EX0004_01) - open it once in the ELN");
  });

  it("shows the registry warnings", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, routeResult({ file: null, opened: false, warnings: ["The route file could not be written: access denied."] }));
    const s = section(ctx);
    expect(s.querySelector(".banner.warn")!.textContent).toContain("The route file could not be written: access denied.");
    expect(s.querySelector(".route-opened")).toBeNull();
  });

  it("shows an error with the usual error box, and the button works again", async () => {
    const ctx = mount();
    await shown(ctx);
    button(section(ctx), "Generate ChemDraw tree").click();
    ctx.client.last("route").resolve({ ok: false, error: { code: "not_found", message: "No stereo variant EX0004_01." } });
    await flush();
    const s = section(ctx);
    const box = s.querySelector(".banner.error")!;
    expect(box.textContent).toContain("Nothing found");
    expect(box.textContent).toContain("No stereo variant EX0004_01.");
    expect(box.textContent).toContain("Code: not_found");
    button(s, "Generate ChemDraw tree").click();
    expect(ctx.client.callsOf("route")).toHaveLength(2);
  });

  it("ignores an answer about another variant", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, routeResult({ core_stereo_id: "EX0001_01" }));
    expect(section(ctx).querySelector(".banner.error")!.textContent).toContain("Code: host_bad_response");
  });

  it("puts host texts in as text only", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(
      ctx,
      routeResult({
        notes: [EVIL],
        warnings: [EVIL],
        experiments: [EVIL],
        missing: [{ experiment_ref: "1", experiment_code: EVIL, experiment_url: `javascript:alert(1)`, compound: EVIL }],
      }),
    );
    const s = section(ctx);
    expect(s.querySelector("img")).toBeNull();
    expect(s.querySelector("script")).toBeNull();
    expect(s.querySelector("a")).toBeNull();
    expect(s.textContent).toContain(EVIL);
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("the default 'Save a copy' makes a blob typed chemical/x-cdxml with the file's name", async () => {
    const made: Blob[] = [];
    const clicked: string[] = [];
    const url = window.URL as unknown as { createObjectURL?: unknown; revokeObjectURL?: unknown };
    const saved = { create: url.createObjectURL, revoke: url.revokeObjectURL };
    url.createObjectURL = (blob: Blob) => {
      made.push(blob);
      return "blob:moz-extension://test/1";
    };
    url.revokeObjectURL = () => {};
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this.download);
    });
    try {
      const ctx = mount({ defaultDownload: true });
      await shown(ctx);
      await generate(ctx, routeResult());
      button(section(ctx), "Save a copy").click();
      expect(made).toHaveLength(1);
      expect(made[0]!.type).toBe(CDXML_TYPE);
      expect(await made[0]!.text()).toBe("<?xml version=\"1.0\"?><CDXML></CDXML>");
      expect(clicked).toEqual(["EX0004_01_route_2026-11-19_101500.cdxml"]);
    } finally {
      click.mockRestore();
      url.createObjectURL = saved.create;
      url.revokeObjectURL = saved.revoke;
    }
  });
});

describe("route texts and answer checks", () => {
  it("formats the lines", () => {
    expect(folderOf("C:\\a\\b\\x.cdxml")).toBe("C:\\a\\b");
    expect(folderOf("/home/x/y.cdxml")).toBe("/home/x");
    expect(stepsLine(routeResult({ steps: 1, experiments: [] }))).toBe("Steps: 1");
    expect(openedLine(routeResult({ file: null }))).toBeNull();
    expect(openedLine(routeResult({ opened: false, open_message: null }))).toBe("Written: EX0004_01_route_2026-11-19_101500.cdxml");
  });

  it("accepts a well-formed route answer and refuses others", () => {
    expect(isRouteResult(routeResult())).toBe(true);
    expect(isResultOf("route", routeResult(), PROTOCOL_VERSION)).toBe(true);
    expect(isRouteResult(routeResult({ cdxml: "x".repeat(MAX_ROUTE_CDXML_CHARS + 1) }))).toBe(false);
    expect(isRouteResult({ ...routeResult(), file: { path: "C:\\x", name: "a\\b.cdxml" } })).toBe(false);
    expect(isRouteResult({ ...routeResult(), steps: -1 })).toBe(false);
    expect(isRouteResult({ ...routeResult(), missing: [{ experiment_ref: "1" }] })).toBe(false);
    expect(isRouteResult({ ...routeResult(), notes: [1] })).toBe(false);
  });

  it("accepts a well-formed set_reaction answer and refuses others", () => {
    const ok = { experiment_ref: "4120", changed: true, updated_at: "2026-11-19T10:00:00+00:00", warnings: [] };
    expect(isSetReactionResult(ok)).toBe(true);
    expect(isResultOf("set_reaction", ok, PROTOCOL_VERSION)).toBe(true);
    expect(isSetReactionResult({ ...ok, changed: "yes" })).toBe(false);
    expect(isSetReactionResult({ ...ok, experiment_ref: "" })).toBe(false);
  });

  it("the page's client turns a malformed route answer into host_bad_response", async () => {
    const client = createRuntimeHostClient(async () => ({ ok: true, result: { core_stereo_id: "EX0004_01" } }));
    const reply = await client.request("route", { core_stereo_id: "EX0004_01", open: true });
    expect(reply).toMatchObject({ ok: false, error: { code: "host_bad_response" } });
  });
});

describe("compounds page: synthesis route while the variant is hidden", () => {
  async function hideAndShow(ctx: Ctx, between: () => Promise<void> = async () => {}) {
    button(ctx.root, "Hide stereo variants and details").click();
    await flush();
    expect(ctx.root.querySelector(".route-section")).toBeNull();
    await between();
    const finds = ctx.client.callsOf("find").length;
    button(ctx.root, "Show stereo variants and details").click();
    if (ctx.client.callsOf("find").length > finds) ctx.client.last("find").resolve({ ok: true, result: twoVariants() });
    await flush();
  }

  it("a drawing still running when the variant is collapsed and shown again cannot be asked twice, and its answer is shown", async () => {
    const ctx = mount();
    await shown(ctx);
    button(section(ctx), "Generate ChemDraw tree").click();
    expect(ctx.client.callsOf("route")).toHaveLength(1);
    await hideAndShow(ctx);
    const busy = button(section(ctx), "Generate ChemDraw tree");
    expect(busy.disabled).toBe(true);
    expect(section(ctx).textContent).toContain("Drawing the route…");
    busy.click();
    expect(ctx.client.callsOf("route")).toHaveLength(1);
    ctx.client.last("route").resolve({ ok: true, result: routeResult() });
    await flush();
    expect(section(ctx).textContent).toContain("Opened in ChemDraw: EX0004_01_route_2026-11-19_101500.cdxml");
    expect(button(section(ctx), "Generate ChemDraw tree").disabled).toBe(false);
  });

  it("an answer that comes while the variant is hidden is shown when it is shown again", async () => {
    const ctx = mount();
    await shown(ctx);
    button(section(ctx), "Generate ChemDraw tree").click();
    await hideAndShow(ctx, async () => {
      ctx.client.last("route").resolve({ ok: true, result: routeResult() });
      await flush();
    });
    expect(section(ctx).textContent).toContain("Opened in ChemDraw: EX0004_01_route_2026-11-19_101500.cdxml");
    expect(ctx.client.callsOf("route")).toHaveLength(1);
    // Once shown, it is forgotten like any other when the variant is hidden again.
    await hideAndShow(ctx);
    expect(section(ctx).textContent).not.toContain("Opened in ChemDraw");
  });
});

// ------------------------------------------------------------------ starting material (SM)

function abc001(over: Partial<RouteChoice> = {}): RouteChoice {
  return {
    experiment_ref: "4110",
    experiment_code: "ABC-001",
    compound: "EX0001_01",
    row: 1,
    by: "auto",
    auto_row: 1,
    ambiguous: false,
    candidates: [
      { row: 1, name: "4-Bromobenzaldehyde", eq: 1, mass: "2.31 g" },
      { row: 2, name: "Phenylboronic acid", eq: 1.2, mass: "2.68 g" },
      { row: 3, name: "Palladium(II) acetate", eq: 0.05, mass: "861 mg" },
    ],
    ...over,
  };
}

function abc002(over: Partial<RouteChoice> = {}): RouteChoice {
  return {
    experiment_ref: "4120",
    experiment_code: "ABC-002",
    compound: "EX0002_01",
    row: 1,
    by: "auto",
    auto_row: 1,
    ambiguous: true,
    candidates: [
      { row: 1, name: "C13H10O", eq: 1, mass: "1.50 g" },
      { row: 2, name: "(S)-(-)-1-Phenylethylamine", eq: 1, mass: "846 mg" },
      { row: 3, name: null, eq: null, mass: null },
    ],
    ...over,
  };
}

const withChoices = (choices: RouteChoice[], over: Partial<RouteResult> = {}) => routeResult({ choices, ...over });
const sm = (ctx: Ctx, id = "EX0004_01") => section(ctx, id).querySelector<HTMLElement>(".route-sm");
const smLine = (ctx: Ctx, ref: string, id = "EX0004_01") =>
  section(ctx, id).querySelector<HTMLElement>(`.route-sm-line[data-ref="${ref}"]`)!;
const smSelect = (ctx: Ctx, ref: string, id = "EX0004_01") => smLine(ctx, ref, id).querySelector<HTMLSelectElement>("select")!;
const optionTexts = (select: HTMLSelectElement) => Array.from(select.options).map((o) => o.textContent);

function choose(select: HTMLSelectElement, value: string): void {
  select.value = value;
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

function limitingResult(over: Partial<SetLimitingResult> = {}): SetLimitingResult {
  return { experiment_ref: "4120", row: 2, name: "(S)-(-)-1-Phenylethylamine", changed: true, warnings: [], ...over };
}

describe("compounds page: starting material (SM) of a route", () => {
  it("offers a choice for each run where two reactants have 1 eq, labelled with what it makes", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc001(), abc002()]));
    const block = sm(ctx)!;
    expect(block.querySelector("h6")!.textContent).toBe("Starting material (SM)");
    const ambiguous = Array.from(block.querySelectorAll(".route-sm-ambiguous .route-sm-line"));
    expect(ambiguous.map((li) => li.getAttribute("data-ref"))).toEqual(["4120"]);
    const label = smLine(ctx, "4120").querySelector("label")!;
    expect(label.textContent).toBe("ABC-002 (makes EX0002_01)");
    const select = smSelect(ctx, "4120");
    expect(label.htmlFor).toBe(select.id);
    expect(optionTexts(select)).toEqual([
      "Automatic - C13H10O (1 eq, 1.50 g)",
      "C13H10O (1 eq, 1.50 g)",
      "(S)-(-)-1-Phenylethylamine (1 eq, 846 mg)",
      "Row 3",
    ]);
    expect(Array.from(select.options).map((o) => o.value)).toEqual(["auto", "1", "2", "3"]);
    expect(select.value).toBe("auto");
    expect(select.disabled).toBe(false);
  });

  it("keeps the other runs behind 'Change for other experiments' (closed by default)", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc001(), abc002()]));
    const toggle = button(sm(ctx)!, "Change for other experiments");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const others = sm(ctx)!.querySelector<HTMLElement>(".route-sm-others")!;
    expect(toggle.getAttribute("aria-controls")).toBe(others.id);
    expect(others.hidden).toBe(true);
    toggle.click();
    const opened = button(sm(ctx)!, "Change for other experiments");
    expect(opened.getAttribute("aria-expanded")).toBe("true");
    expect(sm(ctx)!.querySelector<HTMLElement>(".route-sm-others")!.hidden).toBe(false);
    expect(smLine(ctx, "4110").querySelector("label")!.textContent).toBe("ABC-001 (makes EX0001_01)");
    expect(optionTexts(smSelect(ctx, "4110"))).toEqual([
      "Automatic - 4-Bromobenzaldehyde (1 eq, 2.31 g)",
      "4-Bromobenzaldehyde (1 eq, 2.31 g)",
      "Phenylboronic acid (1.2 eq, 2.68 g)",
      "Palladium(II) acetate (0.05 eq, 861 mg)",
    ]);
    // It stays open while the section is rebuilt, and closes again.
    choose(smSelect(ctx, "4110"), "2");
    expect(sm(ctx)!.querySelector<HTMLElement>(".route-sm-others")!.hidden).toBe(false);
    button(sm(ctx)!, "Change for other experiments").click();
    expect(sm(ctx)!.querySelector<HTMLElement>(".route-sm-others")!.hidden).toBe(true);
  });

  it("shows only the toggle when no run is ambiguous, nothing when there is no run, and no toggle without other runs", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc001()]));
    expect(sm(ctx)!.querySelector(".route-sm-ambiguous")).toBeNull();
    expect(sm(ctx)!.querySelector(".route-sm-hint")).toBeNull();
    expect(button(sm(ctx)!, "Change for other experiments").getAttribute("aria-expanded")).toBe("false");
    await generate(ctx, withChoices([]));
    expect(sm(ctx)).toBeNull();
    await generate(ctx, withChoices([abc002()]));
    expect(sm(ctx)!.querySelector(".route-sm-toggle")).toBeNull();
    expect(sm(ctx)!.querySelector(".route-sm-hint")!.textContent).toContain("1 eq");
  });

  it("selects the user's row when the user chose it, else 'auto'; 'Automatic - none' when the rule finds none", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(
      ctx,
      withChoices([
        abc002({ row: 2, by: "user" }),
        abc001({ experiment_ref: "4125", experiment_code: null, compound: "EX0003_01", row: null, by: null, auto_row: null, ambiguous: true }),
      ]),
    );
    expect(smSelect(ctx, "4120").value).toBe("2");
    const none = smSelect(ctx, "4125");
    expect(none.value).toBe("auto");
    expect(optionTexts(none)[0]).toBe("Automatic - none");
    expect(smLine(ctx, "4125").querySelector("label")!.textContent).toBe("Experiment 4125 (makes EX0003_01)");
  });

  it("saves the choice with set_limiting (a row, or null for automatic); the select waits while it is saved", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc002()]));
    choose(smSelect(ctx, "4120"), "2");
    expect(ctx.client.callsOf("set_limiting")).toHaveLength(1);
    expect(ctx.client.last("set_limiting").params as SetLimitingParams).toEqual({ experiment_ref: "4120", row: 2 });
    const busy = smSelect(ctx, "4120");
    expect(busy.disabled).toBe(true);
    expect(busy.value).toBe("2");
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult() });
    await flush();
    const after = smSelect(ctx, "4120");
    expect(after.disabled).toBe(false);
    expect(after.value).toBe("2");
    expect(section(ctx).querySelectorAll(".route-sm-saved")).toHaveLength(1);
    expect(section(ctx).querySelector(".route-sm-saved")!.textContent).toBe('Saved. Click "Generate ChemDraw tree" to redraw.');
    // Back to automatic: row null; still one "Saved." line.
    choose(after, "auto");
    expect(ctx.client.last("set_limiting").params as SetLimitingParams).toEqual({ experiment_ref: "4120", row: null });
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult({ row: null, name: null }) });
    await flush();
    expect(smSelect(ctx, "4120").value).toBe("auto");
    expect(section(ctx).querySelectorAll(".route-sm-saved")).toHaveLength(1);
    // The "Saved." line sits right after the button row, and goes when the route is drawn again.
    expect(section(ctx).querySelector(".route-head")!.nextElementSibling!.classList.contains("route-sm-saved")).toBe(true);
    await generate(ctx, withChoices([abc002()]));
    expect(section(ctx).querySelector(".route-sm-saved")).toBeNull();
  });

  it("on failure shows the usual error box and the select returns to its previous value", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc002({ row: 2, by: "user" })]));
    choose(smSelect(ctx, "4120"), "3");
    ctx.client.last("set_limiting").resolve({ ok: false, error: { code: "invalid_argument", message: "Experiment 4120 has no reactant in row 3." } });
    await flush();
    const select = smSelect(ctx, "4120");
    expect(select.value).toBe("2");
    expect(select.disabled).toBe(false);
    const box = sm(ctx)!.querySelector(".banner.error")!;
    expect(box.textContent).toContain("Experiment 4120 has no reactant in row 3.");
    expect(box.textContent).toContain("Code: invalid_argument");
    expect(section(ctx).querySelector(".route-sm-saved")).toBeNull();
    // A later success clears the error.
    choose(smSelect(ctx, "4120"), "1");
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult({ row: 1, name: "C13H10O" }) });
    await flush();
    expect(sm(ctx)!.querySelector(".banner.error")).toBeNull();
    expect(smSelect(ctx, "4120").value).toBe("1");
  });

  it("treats an answer about another experiment as a bad answer", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc002()]));
    choose(smSelect(ctx, "4120"), "2");
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult({ experiment_ref: "1" }) });
    await flush();
    expect(sm(ctx)!.querySelector(".banner.error")!.textContent).toContain("Code: host_bad_response");
    expect(smSelect(ctx, "4120").value).toBe("auto");
  });

  it("shows the saved choice in every route that lists the experiment; an older drawing does not undo it", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc002()]));
    // The other variant's drawing is asked for before the save and answers after it.
    button(section(ctx, "EX0004_02"), "Generate ChemDraw tree").click();
    choose(smSelect(ctx, "4120"), "2");
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult() });
    await flush();
    ctx.client.last("route").resolve({ ok: true, result: withChoices([abc002()], { core_stereo_id: "EX0004_02" }) });
    await flush();
    expect(smSelect(ctx, "4120", "EX0004_02").value).toBe("2");
    expect(smSelect(ctx, "4120").value).toBe("2");
    // Only the section it was changed in says "Saved.".
    expect(section(ctx, "EX0004_02").querySelector(".route-sm-saved")).toBeNull();
    // A drawing asked for after the save is newer: what it reports is shown everywhere.
    await generate(ctx, withChoices([abc002({ row: 1, by: "auto" })], { core_stereo_id: "EX0004_02" }), "EX0004_02");
    expect(smSelect(ctx, "4120", "EX0004_02").value).toBe("auto");
    expect(smSelect(ctx, "4120").value).toBe("auto");
  });

  it("one save per experiment at a time: the same experiment in another route waits too", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc002()]));
    await generate(ctx, withChoices([abc002()], { core_stereo_id: "EX0004_02" }), "EX0004_02");
    choose(smSelect(ctx, "4120"), "2");
    expect(smSelect(ctx, "4120", "EX0004_02").disabled).toBe(true);
    expect(smSelect(ctx, "4120", "EX0004_02").value).toBe("2");
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult() });
    await flush();
    expect(smSelect(ctx, "4120", "EX0004_02").disabled).toBe(false);
    expect(ctx.client.callsOf("set_limiting")).toHaveLength(1);
  });

  it("a save still running when the variant is hidden keeps its select waiting when the route is drawn again", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc002()]));
    choose(smSelect(ctx, "4120"), "2");
    button(ctx.root, "Hide stereo variants and details").click();
    await flush();
    const finds = ctx.client.callsOf("find").length;
    button(ctx.root, "Show stereo variants and details").click();
    if (ctx.client.callsOf("find").length > finds) ctx.client.last("find").resolve({ ok: true, result: twoVariants() });
    await flush();
    expect(sm(ctx)).toBeNull(); // the answer was forgotten with the hidden variant
    await generate(ctx, withChoices([abc002()]));
    expect(smSelect(ctx, "4120").disabled).toBe(true);
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult() });
    await flush();
    expect(smSelect(ctx, "4120").disabled).toBe(false);
    expect(smSelect(ctx, "4120").value).toBe("2");
    expect(ctx.client.callsOf("set_limiting")).toHaveLength(1);
  });

  it("shows and announces the registry warnings of a save, as text only, until the route is drawn again", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc002()]));
    choose(smSelect(ctx, "4120"), "2");
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult({ warnings: ["The automatic backup failed: disk full.", EVIL] }) });
    await flush();
    const banner = section(ctx).querySelector<HTMLElement>(".route-sm-warnings")!;
    expect(banner.classList.contains("banner")).toBe(true);
    expect(banner.querySelector("strong")!.textContent).toBe("Registry warnings");
    expect(Array.from(banner.querySelectorAll("li")).map((li) => li.textContent)).toEqual(["The automatic backup failed: disk full.", EVIL]);
    expect(banner.querySelector("img")).toBeNull();
    expect(section(ctx).querySelector(".route-sm-saved")).not.toBeNull();
    const live = ctx.root.querySelector<HTMLElement>('[aria-live="polite"][role="status"]')!;
    expect(live.textContent).toContain("starting material set to (S)-(-)-1-Phenylethylamine");
    expect(live.textContent).toContain("disk full");
    // A later save without warnings drops them; so does a new drawing.
    choose(smSelect(ctx, "4120"), "1");
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult({ row: 1, name: "C13H10O" }) });
    await flush();
    expect(section(ctx).querySelector(".route-sm-warnings")).toBeNull();
    choose(smSelect(ctx, "4120"), "2");
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult({ warnings: ["The automatic backup failed: disk full."] }) });
    await flush();
    expect(section(ctx).querySelector(".route-sm-warnings")).not.toBeNull();
    await generate(ctx, withChoices([abc002({ row: 2, by: "user" })]));
    expect(section(ctx).querySelector(".route-sm-warnings")).toBeNull();
  });

  it("a drawing asked for after a save was sent already uses it: no 'redraw' line, warnings still shown", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc002()]));
    choose(smSelect(ctx, "4120"), "2");
    button(section(ctx), "Generate ChemDraw tree").click();
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult({ warnings: ["The automatic backup failed: disk full."] }) });
    await flush();
    expect(section(ctx).querySelector(".route-sm-saved")).toBeNull();
    expect(section(ctx).querySelector(".route-sm-warnings")).not.toBeNull();
    const live = ctx.root.querySelector<HTMLElement>('[aria-live="polite"][role="status"]')!;
    expect(live.textContent).toContain("starting material set to");
    expect(live.textContent).not.toContain("to redraw");
    ctx.client.last("route").resolve({ ok: true, result: withChoices([abc002({ row: 2, by: "user" })]) });
    await flush();
    expect(section(ctx).querySelector(".route-sm-saved")).toBeNull();
    expect(smSelect(ctx, "4120").value).toBe("2");
    // A save after that drawing is not in it: "redraw" again.
    choose(smSelect(ctx, "4120"), "1");
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult({ row: 1, name: "C13H10O" }) });
    await flush();
    expect(section(ctx).querySelector(".route-sm-saved")).not.toBeNull();
  });

  it("focuses the select again after its save when the focus was lost", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(ctx, withChoices([abc002()]));
    smSelect(ctx, "4120").focus();
    choose(smSelect(ctx, "4120"), "2");
    ctx.client.last("set_limiting").resolve({ ok: true, result: limitingResult() });
    await flush();
    expect(document.activeElement).toBe(smSelect(ctx, "4120"));
  });

  it("puts host texts of the runs in as text only", async () => {
    const ctx = mount();
    await shown(ctx);
    await generate(
      ctx,
      withChoices([abc002({ experiment_code: EVIL, compound: EVIL, candidates: [{ row: 1, name: EVIL, eq: 1, mass: EVIL }] })]),
    );
    const block = sm(ctx)!;
    expect(block.querySelector("img")).toBeNull();
    expect(block.querySelector("script")).toBeNull();
    expect(block.textContent).toContain(EVIL);
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });
});

describe("starting material texts and answer checks", () => {
  it("formats equivalents and candidates, leaving unknown parts out", () => {
    expect(formatEq(1)).toBe("1");
    expect(formatEq(1.2)).toBe("1.2");
    expect(formatEq(0.025)).toBe("0.025");
    expect(formatEq(1.23456)).toBe("1.23");
    expect(formatEq(150.4)).toBe("150");
    expect(candidateLabel({ row: 2, name: "THF", eq: null, mass: "1.2 g" })).toBe("THF (1.2 g)");
    expect(candidateLabel({ row: 2, name: "  ", eq: 2, mass: null })).toBe("Row 2 (2 eq)");
    expect(candidateLabel({ row: 4, name: null, eq: null, mass: null })).toBe("Row 4");
    expect(autoOptionLabel(abc002({ auto_row: 2 }))).toBe("Automatic - (S)-(-)-1-Phenylethylamine (1 eq, 846 mg)");
    expect(autoOptionLabel(abc002({ auto_row: 9 }))).toBe("Automatic - Row 9");
    expect(autoOptionLabel(abc002({ auto_row: null }))).toBe("Automatic - none");
    expect(choiceLabel(abc002({ experiment_code: " " }))).toBe("Experiment 4120 (makes EX0002_01)");
  });

  it("accepts well-formed route choices and refuses others", () => {
    expect(isRouteResult(withChoices([abc001(), abc002()]))).toBe(true);
    expect(isRouteChoice(abc002({ row: null, by: null, auto_row: null }))).toBe(true);
    expect(isRouteChoice(abc002({ row: 2, by: "user" }))).toBe(true);
    const { choices: _omit, ...withoutChoices } = routeResult();
    expect(isRouteResult(withoutChoices)).toBe(false);
    expect(isRouteResult({ ...routeResult(), choices: [{ experiment_ref: "1" }] })).toBe(false);
    expect(isRouteChoice({ ...abc002(), by: "someone" })).toBe(false);
    expect(isRouteChoice({ ...abc002(), row: 0 })).toBe(false);
    expect(isRouteChoice({ ...abc002(), row: 1.5 })).toBe(false);
    expect(isRouteChoice({ ...abc002(), auto_row: 1000 })).toBe(false);
    expect(isRouteChoice({ ...abc002(), ambiguous: "yes" })).toBe(false);
    expect(isRouteChoice({ ...abc002(), experiment_ref: "" })).toBe(false);
    expect(isRouteChoice({ ...abc002(), compound: null })).toBe(false);
    expect(isRouteChoice({ ...abc002(), candidates: null })).toBe(false);
    expect(isRouteChoice({ ...abc002(), candidates: [{ row: 1, name: "a", eq: "1", mass: null }] })).toBe(false);
    expect(isRouteChoice({ ...abc002(), candidates: [{ row: 1, name: "a", eq: 1 }] })).toBe(false);
    expect(isRouteChoice({ ...abc002(), candidates: [{ row: 1, name: "a", eq: 1, mass: null }, { row: 1, name: "b", eq: 1, mass: null }] })).toBe(false);
  });

  it("accepts a well-formed set_limiting answer and refuses others", () => {
    expect(isSetLimitingResult(limitingResult())).toBe(true);
    expect(isSetLimitingResult(limitingResult({ row: null, name: null }))).toBe(true);
    expect(isResultOf("set_limiting", limitingResult(), PROTOCOL_VERSION)).toBe(true);
    expect(isSetLimitingResult({ ...limitingResult(), row: 0 })).toBe(false);
    expect(isSetLimitingResult({ ...limitingResult(), row: "2" })).toBe(false);
    expect(isSetLimitingResult({ ...limitingResult(), experiment_ref: "" })).toBe(false);
    expect(isSetLimitingResult({ ...limitingResult(), changed: 1 })).toBe(false);
    expect(isSetLimitingResult({ ...limitingResult(), warnings: undefined })).toBe(false);
  });

  it("the page's client turns a malformed set_limiting answer into host_bad_response", async () => {
    const client = createRuntimeHostClient(async () => ({ ok: true, result: { experiment_ref: "4120", row: "two" } }));
    const reply = await client.request("set_limiting", { experiment_ref: "4120", row: 2 });
    expect(reply).toMatchObject({ ok: false, error: { code: "host_bad_response" } });
  });
});
