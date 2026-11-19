// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { ALSO_MADE_IN_MAX, capEntries, findHeading, otherExperiments } from "../src/content/experiments";
import { controlFrame, CONTENT_FRAME_NAME } from "../src/content/frame";
import { createRuntimeHostClient } from "../src/content/host-client";
import { SearchController } from "../src/content/search-state";
import { HOST_TAG, mountPanel, type PanelHandle } from "../src/content/ui/panel";
import type { SourceState } from "../src/shared/adapter";
import { SITE_ORIGIN, type BackgroundReply, type FindResult, type FoundVariant } from "../src/shared/protocol";
import {
  FakeClient,
  StaticSource,
  analyzeResult,
  batch,
  experiment,
  flush,
  manualTimers,
  pingResult,
  product,
} from "./helpers";

const EVIL = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>`;

function variant(over: Partial<FoundVariant> = {}): FoundVariant {
  return {
    core_stereo_id: "000042_02",
    stereo_no: 2,
    stereo_mode: "abs",
    smiles: "C[C@H](N)O",
    inchikey: "KEY-A",
    batch_count: 2,
    svg: "<svg xmlns='http://www.w3.org/2000/svg'/>",
    batches: [batch({ full_id: "000042_02_001" }), batch({ full_id: "000042_02_002" })],
    ...over,
  };
}

function findResult(over: Partial<FindResult> = {}): FindResult {
  return {
    query: "000042_02",
    level: "stereo",
    core_no: 42,
    matched_id: null,
    variants: [variant()],
    other_variants: [],
    experiments: [
      {
        experiment_ref: "4125",
        experiment_code: "ABC-003",
        experiment_url: `${SITE_ORIGIN}/startUseCase?useCase=performSearch&table=ElnReaction&pk=4125&viewMode=1`,
        batches: [
          { full_id: "000042_02_001", core_stereo_id: "000042_02", product_index: 1, created_at: "2026-11-01T10:00:00+00:00", retired: false },
        ],
      },
      {
        experiment_ref: "4155",
        experiment_code: null,
        experiment_url: null,
        batches: [
          { full_id: "000042_02_002", core_stereo_id: "000042_02", product_index: 2, created_at: "2026-11-17T09:00:00+00:00", retired: true },
        ],
      },
    ],
    warnings: [],
    ...over,
  };
}

let handles: PanelHandle[] = [];
afterEach(() => {
  for (const h of handles) h.destroy();
  handles = [];
  document.documentElement.querySelectorAll(HOST_TAG).forEach((n) => n.remove());
});

function mount(state: SourceState = { kind: "idle", message: "No experiment is shown here." }) {
  const client = new FakeClient();
  const timers = manualTimers();
  const source = new StaticSource(state, { autoLookup: true, confirmBeforeCreate: false });
  const handle = mountPanel({ doc: document, source, client, schedule: timers.schedule });
  handles.push(handle);
  handle.open();
  return { client, timers, source, handle, root: handle.root };
}

const buttons = (root: ShadowRoot) => Array.from(root.querySelectorAll("button"));
function button(root: ShadowRoot, text: string | RegExp): HTMLButtonElement {
  const found = buttons(root).find((b) => (typeof text === "string" ? b.textContent === text : text.test(b.textContent ?? "")));
  if (!found) throw new Error(`no button ${text}; have: ${buttons(root).map((b) => b.textContent).join(" | ")}`);
  return found;
}
const findInput = (root: ShadowRoot) => root.querySelector<HTMLInputElement>(".search input")!;
function typeAndFind(root: ShadowRoot, text: string): void {
  findInput(root).value = text;
  button(root, "Find").click();
}

describe("background and validation", () => {
  const errorOf = (r: BackgroundReply) => (r as Extract<BackgroundReply, { ok: false }>).error;

  it("accepts a well-formed find result", async () => {
    const client = createRuntimeHostClient(async () => ({ ok: true, result: findResult() }));
    expect((await client.request("find", { id: "000042_02" })).ok).toBe(true);
  });

  it.each([
    ["unknown level", { level: "everything" }],
    ["batch level without matched_id", { level: "batch", matched_id: null }],
    ["experiments not a list", { experiments: "ABC-003" }],
    ["variant without batches", { variants: [{ ...variant(), batches: undefined }] }],
    ["experiment batch without retired flag", {
      experiments: [{ experiment_ref: "1", experiment_code: null, experiment_url: null, batches: [{ full_id: "x", core_stereo_id: "x", product_index: 1, created_at: "d" }] }],
    }],
    ["warnings not texts", { warnings: [1] }],
  ])("refuses a find result with %s", async (_name, over) => {
    const client = createRuntimeHostClient(async () => ({ ok: true, result: { ...findResult(), ...over } }));
    expect(errorOf(await client.request("find", { id: "x" })).code).toBe("host_bad_response");
  });
});

describe("Also made in", () => {
  const exp = (ref: string, code: string | null, url: string | null = null, retired = false) =>
    batch({ full_id: `B_${ref}_${code}_${retired}`, experiment_ref: ref, experiment_code: code, experiment_url: url, retired });

  it("lists other experiments only, without retired batches, one entry per experiment", () => {
    const result = analyzeResult({
      batches: [exp("4180", "ABC-008"), exp("1", "ABC-003"), exp("1", "ABC-003"), exp("2", null), exp("3", "ABC-005", null, true)],
    });
    expect(otherExperiments(result, "4180").map((e) => e.text)).toEqual(["ABC-003", "key 2"]);
  });

  it("caps the list", () => {
    expect(capEntries([1, 2, 3, 4, 5, 6, 7])).toEqual({ shown: [1, 2, 3, 4, 5], more: 2 });
    expect(ALSO_MADE_IN_MAX).toBe(5);
  });

  async function cardWith(batches: ReturnType<typeof batch>[]) {
    const ctx = mount({ kind: "ready", experiment: experiment(), products: [product({ label: "Product 1" })] });
    ctx.timers.run();
    ctx.client.last("analyze").resolve({ ok: true, result: analyzeResult({ status: "known_compound", batches }) });
    await flush();
    return ctx;
  }

  it("renders links in a new tab only for site URLs, plain text otherwise, and +N more", async () => {
    const ctx = await cardWith([
      exp("1", "ABC-001", `${SITE_ORIGIN}/startUseCase?pk=1`),
      exp("2", "ABC-002", "javascript:alert(1)"),
      exp("3", "ABC-003", "https://evil.example/x"),
      exp("4", "ABC-004"),
      exp("5", "ABC-005"),
      exp("6", "ABC-006"),
      exp("7", "ABC-007"),
    ]);
    const line = ctx.root.querySelector(".also")!;
    expect(line.textContent).toBe("Also made in: ABC-001, ABC-002, ABC-003, ABC-004, ABC-005 +2 more");
    const links = Array.from(line.querySelectorAll("a"));
    expect(links.map((a) => a.textContent)).toEqual(["ABC-001"]);
    expect(links[0]!.getAttribute("target")).toBe("_blank");
    expect(links[0]!.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("shows nothing when the compound was made nowhere else", async () => {
    const ctx = await cardWith([exp("4180", "ABC-008"), exp("9", "ABC-009", null, true)]);
    expect(ctx.root.querySelector(".also")).toBeNull();
  });

  it("keeps hostile text as text", async () => {
    const ctx = await cardWith([exp("1", EVIL, `${SITE_ORIGIN}/x"><script>alert(1)</script>`)]);
    expect(ctx.root.querySelectorAll("script")).toHaveLength(0);
    expect(ctx.root.querySelector(".also")!.textContent).toContain(EVIL);
    for (const img of Array.from(ctx.root.querySelectorAll("img"))) {
      expect(img.getAttribute("src")!.startsWith("data:image/svg+xml")).toBe(true);
    }
  });
});

describe("search by ID", () => {
  it("sends the trimmed text once, with the button or Enter", async () => {
    const ctx = mount();
    typeAndFind(ctx.root, "  000042_02  ");
    button(ctx.root, "Find").click(); // disabled while running
    expect(ctx.client.callsOf("find")).toHaveLength(1);
    expect(ctx.client.last("find").params).toEqual({ id: "000042_02" });
    ctx.client.last("find").resolve({ ok: true, result: findResult() });
    await flush();
    findInput(ctx.root).value = "000042";
    findInput(ctx.root).dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(ctx.client.callsOf("find")).toHaveLength(2);
    expect(ctx.client.last("find").params).toEqual({ id: "000042" });
  });

  it("sends nothing for an empty text", () => {
    const ctx = mount();
    typeAndFind(ctx.root, "   ");
    expect(ctx.client.callsOf("find")).toHaveLength(0);
  });

  it.each([
    ["stereo", findResult(), "Compound 000042_02"],
    ["batch", findResult({ level: "batch", matched_id: "000042_02_003" }), "Compound 000042_02 (from 000042_02_003)"],
    ["core", findResult({ level: "core", variants: [variant({ core_stereo_id: "000042_01" }), variant()] }), "CORE 000042, all stereo variants"],
  ])("renders the %s level", async (_level, result, heading) => {
    expect(findHeading(result)).toBe(heading);
    const ctx = mount();
    typeAndFind(ctx.root, result.query);
    ctx.client.last("find").resolve({ ok: true, result });
    await flush();
    const found = ctx.root.querySelector(".found")!;
    expect(found.querySelector("h3")!.textContent).toBe(heading);
    expect(found.textContent).toContain("Absolute (as drawn) · 2 batches");
    expect(found.querySelectorAll("img.thumb.small").length).toBe(result.variants.length);
  });

  it("lists experiments oldest first, links in a new tab, retired batches marked", async () => {
    const ctx = mount();
    typeAndFind(ctx.root, "000042_02");
    ctx.client.last("find").resolve({ ok: true, result: findResult() });
    await flush();
    const items = Array.from(ctx.root.querySelectorAll(".found-experiment"));
    expect(items.map((i) => i.querySelector(".found-code")!.textContent)).toEqual(["ABC-003", "key 4155"]);
    const link = items[0]!.querySelector("a")!;
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(items[1]!.querySelector("a")).toBeNull();
    expect(items[0]!.textContent).toContain("000042_02_001 · product 1 · 2026-11-01");
    const retired = items[1]!.querySelector(".retired-batch")!;
    expect(retired.textContent).toBe("000042_02_002 · product 2 · 2026-11-17 · retired");
  });

  it("other stereo variants search that variant", async () => {
    const ctx = mount();
    typeAndFind(ctx.root, "000042_02");
    ctx.client.last("find").resolve({
      ok: true,
      result: findResult({ other_variants: [{ core_stereo_id: "000042_01", stereo_no: 1, stereo_mode: "rac", smiles: "C", inchikey: "K", batch_count: 2 }] }),
    });
    await flush();
    expect(ctx.root.textContent).toContain("Other stereo variants of this CORE:");
    button(ctx.root, "000042_01 · Racemic · 2 batches").click();
    expect(ctx.client.last("find").params).toEqual({ id: "000042_01" });
    expect(findInput(ctx.root).value).toBe("000042_01");
  });

  it("shows warnings and the not_found error with its message", async () => {
    const ctx = mount();
    typeAndFind(ctx.root, "000042_02");
    ctx.client.last("find").resolve({ ok: true, result: findResult({ warnings: ["Registry in a sync folder."] }) });
    await flush();
    expect(ctx.root.textContent).toContain("Registry in a sync folder.");
    typeAndFind(ctx.root, "999999");
    ctx.client.last("find").resolve({ ok: false, error: { code: "not_found", message: "Nothing is registered with CORE number 999999." } });
    await flush();
    expect(ctx.root.textContent).toContain("Nothing found");
    expect(ctx.root.textContent).toContain("Nothing is registered with CORE number 999999.");
    expect(ctx.root.querySelector(".found")).toBeNull();
    button(ctx.root, "Clear").click();
    expect(ctx.root.textContent).not.toContain("Nothing found");
  });

  it("an older answer never replaces a newer one", async () => {
    const client = new FakeClient();
    const search = new SearchController(client, { onChange: () => {} });
    const first = search.search("000042_01");
    const second = search.search("000042_02");
    const [a, b] = client.callsOf("find");
    b!.resolve({ ok: true, result: findResult({ query: "000042_02" }) });
    await second;
    a!.resolve({ ok: true, result: findResult({ query: "000042_01" }) });
    await first;
    expect(search.state.result?.query).toBe("000042_02");
    expect(search.state.query).toBe("000042_02");
    expect(search.state.pending).toBeNull();
  });

  it("never looks up or registers anything", async () => {
    const ctx = mount({ kind: "ready", experiment: experiment(), products: [product({ label: "Product 1" })] });
    ctx.timers.run();
    ctx.client.last("analyze").resolve({ ok: true, result: analyzeResult() });
    await flush();
    const before = ctx.client.calls.map((c) => c.op);
    typeAndFind(ctx.root, "000042_02");
    ctx.client.last("find").resolve({ ok: true, result: findResult() });
    await flush();
    ctx.timers.run();
    const after = ctx.client.calls.map((c) => c.op);
    expect(after).toEqual([...before, "find"]);
    expect(button(ctx.root, "Create ID (new compound)").disabled).toBe(false);
  });

  it("registry_changed on a search pings again and invalidates the cards", async () => {
    const ctx = mount({ kind: "ready", experiment: experiment(), products: [product({ label: "Product 1" })] });
    ctx.client.last("ping").resolve({ ok: true, result: pingResult() });
    ctx.timers.run();
    ctx.client.last("analyze").resolve({ ok: true, result: analyzeResult() });
    await flush();
    typeAndFind(ctx.root, "000042_02");
    ctx.client.last("find").resolve({ ok: false, error: { code: "registry_changed", message: "changed" } });
    await flush();
    expect(ctx.client.callsOf("ping")).toHaveLength(2);
    expect(ctx.root.querySelector(".analysis.stale")).not.toBeNull();
    expect(ctx.root.textContent).toContain("The registry configuration changed");
  });

  it("keeps hostile result text as text", async () => {
    const ctx = mount();
    typeAndFind(ctx.root, "x");
    ctx.client.last("find").resolve({
      ok: true,
      result: findResult({
        query: EVIL,
        variants: [variant({ core_stereo_id: EVIL, svg: EVIL })],
        other_variants: [{ core_stereo_id: EVIL, stereo_no: 1, stereo_mode: EVIL as never, smiles: EVIL, inchikey: EVIL, batch_count: 1 }],
        experiments: [{ experiment_ref: EVIL, experiment_code: EVIL, experiment_url: "javascript:alert(1)", batches: [{ full_id: EVIL, core_stereo_id: EVIL, product_index: 1, created_at: EVIL, retired: false }] }],
        warnings: [EVIL],
      }),
    });
    await flush();
    expect(ctx.root.querySelectorAll("script")).toHaveLength(0);
    expect(ctx.root.querySelectorAll(".found a")).toHaveLength(0);
    for (const img of Array.from(ctx.root.querySelectorAll("img"))) {
      expect(img.getAttribute("src")!.startsWith("data:image/svg+xml")).toBe(true);
    }
    expect(ctx.root.querySelector(".found")!.textContent!.split(EVIL).length - 1).toBeGreaterThanOrEqual(5);
  });
});

describe("panel in the ELN's content frame", () => {
  it("is mounted while idle with only the search and Advanced, and keeps its state when an experiment appears", async () => {
    expect(CONTENT_FRAME_NAME).toBe("contentFrame");
    const source = new StaticSource({ kind: "idle", message: "No experiment is shown here." }, { autoLookup: true, confirmBeforeCreate: false });
    const client = new FakeClient();
    let mounts = 0;
    const frame = controlFrame(
      source,
      () => {
        mounts += 1;
        return mountPanel({ doc: document, source, client, schedule: manualTimers().schedule });
      },
      { alwaysMounted: true },
    );
    const panel = frame.panel()!;
    expect(document.documentElement.querySelectorAll(HOST_TAG)).toHaveLength(1);
    expect(panel.root.querySelector<HTMLButtonElement>("button.launcher")!.textContent).toBe("ID");
    panel.open();
    expect(panel.root.querySelectorAll(".cards > *")).toHaveLength(0);
    expect(panel.root.querySelector(".search")).not.toBeNull();
    expect(panel.root.querySelector(".source-message")!.textContent).toBe("");
    typeAndFind(panel.root, "000042_02");
    client.last("find").resolve({ ok: true, result: findResult() });
    await flush();
    source.set({ kind: "ready", experiment: experiment(), products: [product({ label: "Product 1" })] });
    source.set({ kind: "idle", message: "gone" });
    expect(mounts).toBe(1);
    expect(frame.panel()).toBe(panel);
    expect(panel.isOpen()).toBe(true);
    expect(panel.root.querySelector(".found h3")!.textContent).toBe("Compound 000042_02");
    frame.stop();
  });

  it("other idle frames get no panel", () => {
    const source = new StaticSource({ kind: "idle", message: "x" }, { autoLookup: true, confirmBeforeCreate: false });
    const frame = controlFrame(source, () => mountPanel({ doc: document, source, client: new FakeClient() }));
    expect(document.documentElement.querySelectorAll(HOST_TAG)).toHaveLength(0);
    frame.stop();
  });
});
