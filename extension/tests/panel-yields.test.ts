// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { createRuntimeHostClient } from "../src/content/host-client";
import { HOST_TAG, mountPanel, type PanelHandle } from "../src/content/ui/panel";
import { isBatch, isFoundExperimentBatch, isSetYieldResult, isYieldInfo, isYieldRow, isYieldsResult, readYield } from "../src/content/validate";
import { firstYield, formatPercent, sameYields, shortYield, yieldLine } from "../src/content/yields";
import type { PageYields, SourceState } from "../src/shared/adapter";
import {
  SITE_ORIGIN,
  type AnalyzeResult,
  type BackgroundReply,
  type Batch,
  type FindResult,
  type SetYieldParams,
  type YieldInfo,
  type YieldRow,
} from "../src/shared/protocol";
import { FakeClient, StaticSource, analyzeResult, batch, experiment, flush, manualTimers, product } from "./helpers";

const ID = "000042_01_003";
const info = (isolated: number | null, analytical: number | null = null): YieldInfo => ({
  isolated,
  analytical,
  updated_at: "2026-11-18T09:00:00+00:00",
});
const errorOf = (r: BackgroundReply) => (r as Extract<BackgroundReply, { ok: false }>).error;

function yieldRow(over: Partial<YieldRow> = {}): YieldRow {
  return {
    experiment_ref: "4180",
    experiment_code: "ABC-006",
    experiment_url: null,
    product_index: 1,
    full_id: ID,
    core_stereo_id: "000042_01",
    name: "ethanol",
    yield: info(76.2),
    created_at: "2026-11-01T10:00:00+00:00",
    retired: false,
    ...over,
  };
}

describe("yield texts", () => {
  it.each([
    [76.2, "76.2"],
    [90, "90"],
    [73.65, "73.65"],
    [73.651, "73.65"],
    [0, "0"],
    [100.1, "100.1"],
  ])("prints %s as %s", (value, text) => {
    expect(formatPercent(value)).toBe(text);
  });

  it("the card line names which yield it is", () => {
    expect(yieldLine(info(76.2))).toBe("Yield 76.2 % (isolated)");
    expect(yieldLine(info(null, 78))).toBe("Yield 78 % (analytical)");
    expect(yieldLine(info(76.2, 78))).toBe("Yield 76.2 % isolated · 78 % analytical");
    expect(yieldLine(null)).toBeNull();
    expect(yieldLine(undefined)).toBeNull();
    expect(yieldLine(info(null, null))).toBeNull();
  });

  it("the short form is the isolated yield, else the analytical one marked as such", () => {
    expect(shortYield(info(76.2, 78))).toBe("76.2 %");
    expect(shortYield(info(null, 78))).toBe("78 % analytical");
    expect(shortYield(null)).toBeNull();
  });

  it("compares the page's values rounded to two decimals; a stored null is both null", () => {
    expect(sameYields({ isolated: 73.654, analytical: null }, info(73.65))).toBe(true);
    expect(sameYields({ isolated: 73.656, analytical: null }, info(73.65))).toBe(false);
    expect(sameYields({ isolated: null, analytical: null }, null)).toBe(true);
    expect(sameYields({ isolated: 1, analytical: null }, null)).toBe(false);
  });

  it("takes the first yield of an active batch, else of a retired one", () => {
    expect(firstYield([{ retired: true, yield: info(1) }, { retired: false, yield: null }, { retired: false, yield: info(2) }])).toEqual(info(2));
    expect(firstYield([{ retired: true, yield: info(1) }, { retired: false }])).toEqual(info(1));
    expect(firstYield([])).toBeNull();
  });
});

describe("validators", () => {
  it("accepts a stored yield and tolerates an absent one; refuses a malformed one", () => {
    expect(isYieldInfo(info(76.2, 78))).toBe(true);
    expect(isYieldInfo(info(null, 1000))).toBe(true);
    expect(readYield(undefined)).toBe(true);
    expect(readYield(null)).toBe(true);
    for (const bad of [
      { isolated: "76.2", analytical: null, updated_at: "x" },
      { isolated: -1, analytical: null, updated_at: "x" },
      { isolated: 1001, analytical: null, updated_at: "x" },
      { isolated: Number.NaN, analytical: null, updated_at: "x" },
      { isolated: 1, updated_at: "x" },
      { isolated: 1, analytical: null },
      "76.2",
      [],
    ]) {
      expect(readYield(bad)).toBe(false);
    }
  });

  it("checks the yield on batches: absent ok, malformed refused", () => {
    expect(isBatch(batch())).toBe(true);
    expect(isBatch(batch({ yield: null }))).toBe(true);
    expect(isBatch(batch({ yield: info(5) }))).toBe(true);
    expect(isBatch({ ...batch(), yield: { isolated: "5" } })).toBe(false);
    const found = { full_id: "x", core_stereo_id: "x", product_index: 1, created_at: "d", retired: false };
    expect(isFoundExperimentBatch(found)).toBe(true);
    expect(isFoundExperimentBatch({ ...found, yield: info(1) })).toBe(true);
    expect(isFoundExperimentBatch({ ...found, yield: 76.2 })).toBe(false);
  });

  it("an analyze answer with a malformed batch yield is refused", async () => {
    const result = analyzeResult({ batches: [{ ...batch(), yield: "76.2 %" } as unknown as Batch] });
    const client = createRuntimeHostClient(async () => ({ ok: true, result }));
    expect(errorOf(await client.request("analyze", { smiles: "C" })).code).toBe("host_bad_response");
  });

  it("checks set_yield answers", async () => {
    const good = { id: ID, yield: info(76.2), changed: true, warnings: [] };
    expect(isSetYieldResult(good)).toBe(true);
    expect(isSetYieldResult({ ...good, yield: null })).toBe(true);
    for (const bad of [{ ...good, id: "" }, { ...good, yield: undefined }, { ...good, changed: "yes" }, { ...good, warnings: null }, { ...good, yield: { isolated: 1 } }]) {
      expect(isSetYieldResult(bad)).toBe(false);
    }
    const params: SetYieldParams = { id: ID, experiment_ref: "4180", product_index: 1, isolated: 76.2, analytical: null };
    expect((await createRuntimeHostClient(async () => ({ ok: true, result: good })).request("set_yield", params)).ok).toBe(true);
    const refused = await createRuntimeHostClient(async () => ({ ok: true, result: { id: ID } })).request("set_yield", params);
    expect(errorOf(refused).code).toBe("host_bad_response");
  });

  it("checks yields answers and their rows", async () => {
    const good = { total: 1, registry_total: 3, offset: 0, limit: 200, items: [yieldRow()], warnings: [] };
    expect(isYieldsResult(good)).toBe(true);
    expect(isYieldRow(yieldRow({ yield: null, name: null, experiment_code: null }))).toBe(true);
    for (const bad of [
      { ...yieldRow(), full_id: "" },
      { ...yieldRow(), product_index: "1" },
      { ...yieldRow(), yield: undefined },
      { ...yieldRow(), retired: undefined },
      { ...yieldRow(), name: 5 },
    ]) {
      expect(isYieldRow(bad)).toBe(false);
    }
    expect(isYieldsResult({ ...good, items: [{}] })).toBe(false);
    expect(isYieldsResult({ ...good, total: "1" })).toBe(false);
    expect((await createRuntimeHostClient(async () => ({ ok: true, result: good })).request("yields", {})).ok).toBe(true);
    const refused = await createRuntimeHostClient(async () => ({ ok: true, result: { ...good, warnings: undefined } })).request("yields", {});
    expect(errorOf(refused).code).toBe("host_bad_response");
  });
});

// ---------------------------------------------------------------- the panel

let handles: PanelHandle[] = [];
afterEach(() => {
  for (const h of handles) h.destroy();
  handles = [];
  document.documentElement.querySelectorAll(HOST_TAG).forEach((n) => n.remove());
});

function siteState(yields: PageYields | null | undefined): SourceState {
  return {
    kind: "ready",
    experiment: experiment({ url: null }),
    products: [product({ productIndex: 1, label: "Product 1", changeToken: "k1", ...(yields === undefined ? {} : { yields }) })],
  };
}

function hereBatch(over: Partial<Batch> = {}): Batch {
  return batch({ full_id: ID, batch_no: 3, experiment_ref: "4180", experiment_code: "ABC-008", product_index: 1, yield: null, ...over });
}

function registered(over: Partial<Batch> = {}, others: Batch[] = []): AnalyzeResult {
  return analyzeResult({ status: "registered_here", id: ID, core_stereo_id: "000042_01", batches: [...others, hereBatch(over)] });
}

function mountSite(yields: PageYields | null | undefined, options: { autoLookup?: boolean } = {}) {
  const client = new FakeClient();
  const timers = manualTimers();
  const source = new StaticSource(siteState(yields), { autoLookup: options.autoLookup ?? true, confirmBeforeCreate: false });
  const handle = mountPanel({ doc: document, source, client, schedule: timers.schedule });
  handles.push(handle);
  return { client, timers, source, handle, root: handle.root };
}

async function lookUp(ctx: ReturnType<typeof mountSite>, result: AnalyzeResult) {
  ctx.timers.run();
  ctx.client.last("analyze").resolve({ ok: true, result });
  await flush();
}

const card = (root: ShadowRoot) => root.querySelector<HTMLElement>(".cards > article.card")!;

describe("yields in the panel", () => {
  it("stores the page's yields in the background, with the panel closed", async () => {
    const ctx = mountSite({ isolated: 76.2, analytical: null });
    expect(ctx.handle.isOpen()).toBe(false);
    await lookUp(ctx, registered());
    expect(ctx.client.callsOf("set_yield")).toHaveLength(1);
    expect(ctx.client.last("set_yield").params).toEqual({ id: ID, experiment_ref: "4180", product_index: 1, isolated: 76.2, analytical: null });
    expect(ctx.handle.isOpen()).toBe(false);
    ctx.client.last("set_yield").resolve({ ok: true, result: { id: ID, yield: info(76.2), changed: true, warnings: [] } });
    await flush();
    expect(card(ctx.root).querySelector(".yield-line")!.textContent).toBe("Yield 76.2 % (isolated)");
  });

  it("shows both yields in one quiet line, and nothing when none is stored", async () => {
    const ctx = mountSite(null);
    await lookUp(ctx, registered({ yield: info(76.2, 78) }));
    expect(card(ctx.root).querySelector("p.small.yield-line")!.textContent).toBe("Yield 76.2 % isolated · 78 % analytical");
    const other = mountSite(null);
    await lookUp(other, registered({ yield: null }));
    expect(card(other.root).querySelector(".yield-line")).toBeNull();
  });

  it("sends a new value published by the source without looking up again", async () => {
    const ctx = mountSite({ isolated: 76.2, analytical: null });
    await lookUp(ctx, registered({ yield: info(76.2) }));
    expect(ctx.client.callsOf("set_yield")).toHaveLength(0);
    ctx.source.set(siteState({ isolated: 90, analytical: null }));
    ctx.timers.run();
    expect(ctx.client.callsOf("analyze")).toHaveLength(1);
    expect(ctx.client.last("set_yield").params).toMatchObject({ isolated: 90 });
  });

  it("shows a short note when the yield could not be saved", async () => {
    const ctx = mountSite({ isolated: 76.2, analytical: null });
    await lookUp(ctx, registered());
    ctx.client.last("set_yield").resolve({ ok: false, error: { code: "storage_error", message: "The database is locked." } });
    await flush();
    const note = card(ctx.root).querySelector(".yield-note")!;
    expect(note.textContent).toBe("The yield could not be saved: The database is locked.");
    expect(note.getAttribute("role")).toBe("note");
    expect(ctx.handle.isOpen()).toBe(false);
  });

  it("never sends from a source that is not looked up by itself, nor from the manual form", async () => {
    const ctx = mountSite({ isolated: 76.2, analytical: null }, { autoLookup: false });
    button(ctx.root, "Look up").click();
    ctx.client.last("analyze").resolve({ ok: true, result: registered() });
    await flush();
    expect(ctx.client.callsOf("set_yield")).toHaveLength(0);
  });

  it("shows the yield of other experiments next to their ELN number in 'Also made in'", async () => {
    const ctx = mountSite(null);
    await lookUp(
      ctx,
      registered({}, [
        batch({ full_id: "000042_01_001", experiment_ref: "1", experiment_code: "ABC-006", experiment_url: `${SITE_ORIGIN}/x?pk=1`, yield: info(76.2) }),
        batch({ full_id: "000042_01_002", experiment_ref: "2", experiment_code: "ABC-007", yield: info(null, 78) }),
        batch({ full_id: "000042_01_004", experiment_ref: "4", experiment_code: "ABC-009", yield: null }),
      ]),
    );
    const also = card(ctx.root).querySelector(".also")!;
    expect(also.textContent).toBe("Also made in: ABC-006 (76.2 %), ABC-007 (78 % analytical), ABC-009");
    expect(also.querySelector("a")!.textContent).toBe("ABC-006");
  });

  it("shows each experiment's yield in the result of Find", async () => {
    const ctx = mountSite(undefined);
    ctx.handle.open();
    const input = ctx.root.querySelector<HTMLInputElement>(".search input")!;
    input.value = "000042_01";
    button(ctx.root, "Find").click();
    const found: FindResult = {
      query: "000042_01",
      level: "stereo",
      core_no: 42,
      matched_id: null,
      variants: [],
      other_variants: [],
      experiments: [
        {
          experiment_ref: "1",
          experiment_code: "ABC-006",
          experiment_url: null,
          batches: [{ full_id: "000042_01_001", core_stereo_id: "000042_01", product_index: 1, created_at: "2026-11-01", retired: false, yield: info(76.2) }],
        },
        {
          experiment_ref: "2",
          experiment_code: "ABC-007",
          experiment_url: null,
          batches: [{ full_id: "000042_01_002", core_stereo_id: "000042_01", product_index: 1, created_at: "2026-11-02", retired: false, yield: info(null, 78.25) }],
        },
        {
          experiment_ref: "3",
          experiment_code: "ABC-008",
          experiment_url: null,
          batches: [{ full_id: "000042_01_003", core_stereo_id: "000042_01", product_index: 1, created_at: "2026-11-03", retired: false }],
        },
      ],
      warnings: [],
    };
    ctx.client.last("find").resolve({ ok: true, result: found });
    await flush();
    const codes = Array.from(ctx.root.querySelectorAll(".found-code")).map((n) => n.textContent);
    expect(codes).toEqual(["ABC-006 (76.2 %)", "ABC-007 (78.25 % analytical)", "ABC-008"]);
  });
});

function button(root: ShadowRoot, text: string): HTMLButtonElement {
  const found = Array.from(root.querySelectorAll("button")).find((b) => b.textContent === text);
  if (!found) throw new Error(`no button ${text}`);
  return found;
}
