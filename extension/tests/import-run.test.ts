import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { MolfileResult, ReactionResult } from "../src/content/bridge-client";
import type { HostClient } from "../src/content/host-client";
import {
  BAD_FLAGS_MESSAGE,
  BAD_PRODUCTS_MESSAGE,
  BAD_REF_MESSAGE,
  BUSY_MESSAGE,
  ImportResponder,
  READING_MESSAGE,
  RECHECK_CHANGED_IMPORT,
  importMessageListener,
  importStatus,
  isImportSender,
  readApplyRequest,
} from "../src/content/import-run";
import { storeReactionOnce, type ReactionBridge, type ReactionStoreOutcome } from "../src/content/reaction-capture";
import { NO_STRUCTURE, READING, productToken, type RecheckResult } from "../src/content/site-source";
import type { ExperimentContext, PageYields, ProductInput, SourceState } from "../src/shared/adapter";
import type { PageReaction } from "../src/shared/bridge";
import {
  MAX_IMPORT_PRODUCTS,
  experimentPageUrl,
  type ImportApplyReply,
  type ImportApplyRequest,
  type ImportOutcome,
} from "../src/shared/import";
import {
  MAX_REF_TEXT,
  type AnalyzeParams,
  type BackgroundReply,
  type Op,
  type ParamsByOp,
  type RegisterParams,
  type SetReactionParams,
  type SetYieldParams,
  type YieldInfo,
} from "../src/shared/protocol";
import { analyzeResult, batch, flush, generateNameResult, nameInfo, properties, registerResult } from "./helpers";

const here = dirname(fileURLToPath(import.meta.url));

const RUNTIME_ID = "compound-registry@registry.local";
const PAGE_URL = "moz-extension://4b1d/compounds.html";
const SENDER = { id: RUNTIME_ID, url: `${PAGE_URL}?view=import#run` };
const REF = "4180";
const EXP: ExperimentContext = { ref: REF, code: "ABC-008", url: experimentPageUrl(REF) };

const molfileOf = (index: number) => `mol of product ${index}\nM  END\n`;
const yieldInfo = (isolated: number | null, analytical: number | null = null): YieldInfo => ({
  isolated,
  analytical,
  updated_at: "2026-11-18T10:00:00+00:00",
});

function prod(index: number, yields: PageYields | null = { isolated: null, analytical: null }): ProductInput {
  return {
    productIndex: index,
    label: `Product ${index}`,
    structure: { molfile: molfileOf(index) },
    changeToken: productToken(REF, index, molfileOf(index)),
    yields,
  };
}

function ready(products: ProductInput[], skipped?: Extract<SourceState, { kind: "ready" }>["skipped"]): SourceState {
  const state: SourceState = { kind: "ready", experiment: EXP, products };
  if (skipped) state.skipped = skipped;
  return state;
}

type Handler = (params: unknown) => BackgroundReply | Promise<BackgroundReply>;

/** A host client that answers at once through per-op handlers and records every call. */
class AutoClient implements HostClient {
  calls: { op: Op; params: unknown }[] = [];
  constructor(public handlers: Partial<Record<Op, Handler>> = {}) {}
  async request<K extends Op>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>> {
    this.calls.push({ op, params });
    const handler = this.handlers[op];
    if (!handler) return { ok: false, error: { code: "test", message: `no handler for ${op}` } };
    return (await handler(params)) as BackgroundReply<K>;
  }
  ops(): Op[] {
    return this.calls.map((c) => c.op);
  }
  paramsOf<K extends Op>(op: K): ParamsByOp[K][] {
    return this.calls.filter((c) => c.op === op).map((c) => c.params as ParamsByOp[K]);
  }
}

class FakeSource {
  rechecks: ProductInput[] = [];
  recheckResult: RecheckResult = { result: "unchanged" };
  constructor(public state: SourceState) {}
  getState(): SourceState {
    return this.state;
  }
  async recheck(product: ProductInput): Promise<RecheckResult> {
    this.rechecks.push(product);
    return this.recheckResult;
  }
}

function ok<T>(result: T): BackgroundReply {
  return { ok: true, result } as BackgroundReply;
}
function fail(message: string, code = "some_error"): BackgroundReply {
  return { ok: false, error: { code, message } };
}

/** Analyze answers by product number (default: a new compound). */
function analyzeBy(byIndex: Record<number, Parameters<typeof analyzeResult>[0]> = {}): Handler {
  return (params) => {
    const index = (params as AnalyzeParams).product_index!;
    return ok(analyzeResult({ ...byIndex[index], structure: { key_digest: `digest-${index}`, ...byIndex[index]?.structure } }));
  };
}

/** Register answers built from the request. */
function registerFrom(over: Parameters<typeof registerResult>[0] = {}): Handler {
  return (params) => {
    const p = params as RegisterParams;
    const id = `000042_01_00${p.product_index}`;
    return ok(
      registerResult({
        id,
        batch: batch({ full_id: id, experiment_ref: p.experiment_ref, product_index: p.product_index, yield: null }),
        ...over,
      }),
    );
  };
}

const setYieldEcho: Handler = (params) => {
  const p = params as SetYieldParams;
  const stored = p.isolated === null && p.analytical === null ? null : yieldInfo(p.isolated, p.analytical);
  return ok({ id: p.id, yield: stored, changed: true, warnings: [] });
};

function setup(state: SourceState, handlers: Partial<Record<Op, Handler>> = {}) {
  const source = new FakeSource(state);
  const client = new AutoClient({ analyze: analyzeBy(), register: registerFrom(), set_yield: setYieldEcho, ...handlers });
  const reactions: ExperimentContext[] = [];
  const store: { outcome: ReactionStoreOutcome } = { outcome: { result: "stored", message: null } };
  const responder = new ImportResponder({
    source,
    client,
    storeReaction: async (experiment) => {
      reactions.push(experiment);
      return store.outcome;
    },
    runtimeId: RUNTIME_ID,
    pageUrl: PAGE_URL,
  });
  return { source, client, reactions, store, responder };
}

function applyRequest(over: Partial<ImportApplyRequest> = {}): ImportApplyRequest {
  return { type: "import-apply", ref: REF, products: [1], dry_run: false, allow_warnings: false, names: false, ...over };
}

async function runApply(responder: ImportResponder, over: Partial<ImportApplyRequest> = {}) {
  const answer = responder.answer(applyRequest(over), SENDER);
  expect(answer).not.toBeNull();
  const reply = (await answer) as ImportApplyReply;
  if (!reply.ok) throw new Error(`apply refused: ${reply.message}`);
  return reply;
}

function only(reply: Extract<ImportApplyReply, { ok: true }>): ImportOutcome {
  expect(reply.products).toHaveLength(1);
  return reply.products[0]!;
}

// ------------------------------------------------------------------ status

describe("import status", () => {
  it("gives no answer while the frame shows no experiment", () => {
    expect(importStatus({ kind: "idle", message: "No experiment is shown here." }, REF)).toBeNull();
    const { responder } = setup({ kind: "idle", message: "nothing" });
    expect(responder.answer({ type: "import-status", ref: REF }, SENDER)).toBeNull();
  });

  it("says when another experiment is shown", () => {
    expect(importStatus(ready([prod(1)]), "999")).toEqual({ state: "other", ref: REF });
  });

  it("says reading while a structure is pending, also when other rows are read", () => {
    const state = ready([prod(1)], [{ productIndex: 2, label: "Product 2", reason: READING, pending: true }]);
    expect(importStatus(state, REF)).toEqual({ state: "reading" });
  });

  it("passes an error on", () => {
    expect(importStatus({ kind: "error", message: "The page layout changed." }, REF)).toEqual({
      state: "error",
      message: "The page layout changed.",
    });
  });

  it("an experiment without a key is an error", () => {
    expect(importStatus({ kind: "ready", experiment: null, products: [] }, REF)).toMatchObject({ state: "error" });
  });

  it("lists every row in page order, read ones and those without a structure", () => {
    const state = ready(
      [prod(3, { isolated: 76.2, analytical: null }), prod(1, null)],
      [
        { productIndex: 2, label: "Product 2", reason: NO_STRUCTURE },
        { productIndex: 4, label: "Product 4", reason: "The structure could not be read." },
      ],
    );
    expect(importStatus(state, REF)).toEqual({
      state: "ready",
      experiment: { ref: REF, code: "ABC-008" },
      products: [
        { index: 1, structure: true, problem: null, yields: null },
        { index: 2, structure: false, problem: NO_STRUCTURE, yields: null },
        { index: 3, structure: true, problem: null, yields: { isolated: 76.2, analytical: null } },
        { index: 4, structure: false, problem: "The structure could not be read.", yields: null },
      ],
    });
  });

  it("an experiment without products is ready with no rows", () => {
    expect(importStatus(ready([]), REF)).toEqual({ state: "ready", experiment: { ref: REF, code: "ABC-008" }, products: [] });
  });

  it("answers a status request through `answer`", async () => {
    const { responder } = setup(ready([prod(1)]));
    await expect(responder.answer({ type: "import-status", ref: REF }, SENDER)).resolves.toMatchObject({ state: "ready" });
  });

  it("does not answer a malformed status request", () => {
    const { responder } = setup(ready([prod(1)]));
    for (const message of [
      { type: "import-status" },
      { type: "import-status", ref: "" },
      { type: "import-status", ref: 4180 },
      { type: "import-status", ref: "x".repeat(MAX_REF_TEXT + 1) },
      { type: "something-else", ref: REF },
      "import-status",
      null,
      [{ type: "import-status", ref: REF }],
    ]) {
      expect(responder.answer(message, SENDER)).toBeNull();
    }
  });
});

// ------------------------------------------------------------------ senders

describe("import sender check", () => {
  it("serves only the extension's compounds page, query and fragment ignored", () => {
    expect(isImportSender({ id: RUNTIME_ID, url: PAGE_URL }, RUNTIME_ID, PAGE_URL)).toBe(true);
    expect(isImportSender({ id: RUNTIME_ID, url: `${PAGE_URL}?a=1#b` }, RUNTIME_ID, PAGE_URL)).toBe(true);
    expect(isImportSender({ id: RUNTIME_ID, url: `${PAGE_URL}#b` }, RUNTIME_ID, PAGE_URL)).toBe(true);
  });

  it("refuses everything else", () => {
    const others = [
      undefined,
      null,
      {},
      { id: RUNTIME_ID },
      { url: PAGE_URL },
      { id: "other@ext", url: PAGE_URL },
      { id: RUNTIME_ID, url: "moz-extension://4b1d/other.html" },
      { id: RUNTIME_ID, url: `${PAGE_URL}x` },
      { id: RUNTIME_ID, url: "https://sciformation.chemie.unibas.ch/compounds.html" },
      { id: RUNTIME_ID, url: experimentPageUrl(REF) },
    ];
    for (const sender of others) expect(isImportSender(sender, RUNTIME_ID, PAGE_URL)).toBe(false);
    expect(isImportSender({ id: RUNTIME_ID, url: PAGE_URL }, RUNTIME_ID, "")).toBe(false);
    expect(isImportSender({ id: "", url: PAGE_URL }, "", PAGE_URL)).toBe(false);
  });

  it("a message from another sender gets no answer and starts nothing", async () => {
    const { responder, client } = setup(ready([prod(1)]));
    expect(responder.answer({ type: "import-status", ref: REF }, { id: RUNTIME_ID, url: experimentPageUrl(REF) })).toBeNull();
    expect(responder.answer(applyRequest(), { id: "evil", url: PAGE_URL })).toBeNull();
    expect(responder.answer(applyRequest(), undefined)).toBeNull();
    await flush();
    expect(client.calls).toEqual([]);
  });
});

// ------------------------------------------------------------------ malformed apply

describe("malformed apply requests", () => {
  it("reads a well-formed request into a clean copy", () => {
    const message = { ...applyRequest({ products: [2, 1] }), extra: "dropped" };
    expect(readApplyRequest(message)).toEqual(applyRequest({ products: [2, 1] }));
  });

  it.each([
    ["no ref", { ref: undefined }, BAD_REF_MESSAGE],
    ["empty ref", { ref: "" }, BAD_REF_MESSAGE],
    ["numeric ref", { ref: 4180 }, BAD_REF_MESSAGE],
    ["long ref", { ref: "1".repeat(MAX_REF_TEXT + 1) }, BAD_REF_MESSAGE],
    ["products not a list", { products: "1,2" }, BAD_PRODUCTS_MESSAGE],
    ["product 0", { products: [0] }, BAD_PRODUCTS_MESSAGE],
    ["product 1000", { products: [1000] }, BAD_PRODUCTS_MESSAGE],
    ["fraction", { products: [1.5] }, BAD_PRODUCTS_MESSAGE],
    ["text number", { products: ["1"] }, BAD_PRODUCTS_MESSAGE],
    ["twice", { products: [1, 2, 1] }, BAD_PRODUCTS_MESSAGE],
    ["too many", { products: Array.from({ length: MAX_IMPORT_PRODUCTS + 1 }, (_, i) => i + 1) }, BAD_PRODUCTS_MESSAGE],
    ["dry_run as text", { dry_run: "true" }, BAD_FLAGS_MESSAGE],
    ["allow_warnings missing", { allow_warnings: undefined }, BAD_FLAGS_MESSAGE],
    ["names as number", { names: 1 }, BAD_FLAGS_MESSAGE],
  ])("%s: refused with ok false, nothing sent", async (_name, over, message) => {
    const { responder, client } = setup(ready([prod(1)]));
    const reply = await responder.answer({ ...applyRequest(), ...over }, SENDER);
    expect(reply).toEqual({ ok: false, message });
    expect(client.calls).toEqual([]);
  });

  it("accepts the largest allowed list", () => {
    const products = Array.from({ length: MAX_IMPORT_PRODUCTS }, (_, i) => 999 - i);
    expect(readApplyRequest(applyRequest({ products }))).toEqual(applyRequest({ products }));
  });

  it("a frame without an experiment does not answer an apply, well-formed or not", () => {
    const { responder } = setup({ kind: "idle", message: "nothing" });
    expect(responder.answer(applyRequest(), SENDER)).toBeNull();
    expect(responder.answer({ ...applyRequest(), products: "x" }, SENDER)).toBeNull();
  });
});

// ------------------------------------------------------------------ refusals

describe("apply refusals", () => {
  it("refuses an apply for another experiment than the one shown", async () => {
    const { responder, client } = setup(ready([prod(1)]));
    const reply = await responder.answer(applyRequest({ ref: "111" }), SENDER);
    expect(reply).toMatchObject({ ok: false });
    expect((reply as { message: string }).message).toContain(REF);
    expect(client.calls).toEqual([]);
  });

  it("refuses while products are still being read", async () => {
    const { responder, client } = setup(ready([prod(1)], [{ productIndex: 2, label: "Product 2", reason: READING, pending: true }]));
    await expect(responder.answer(applyRequest(), SENDER)).resolves.toEqual({ ok: false, message: READING_MESSAGE });
    expect(client.calls).toEqual([]);
  });

  it("refuses when the page cannot be read", async () => {
    const { responder } = setup({ kind: "error", message: "The page layout changed." });
    await expect(responder.answer(applyRequest(), SENDER)).resolves.toEqual({ ok: false, message: "The page layout changed." });
  });

  it("refuses a second apply while one runs, then accepts one again", async () => {
    const { responder, client } = setup(ready([prod(1)]));
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    client.handlers.analyze = async (params) => {
      await held;
      return analyzeBy()(params);
    };
    const first = responder.answer(applyRequest({ dry_run: true }), SENDER)!;
    await flush();
    expect(responder.isRunning()).toBe(true);
    await expect(responder.answer(applyRequest({ dry_run: true }), SENDER)).resolves.toEqual({ ok: false, message: BUSY_MESSAGE });
    // Status requests are still answered meanwhile.
    await expect(responder.answer({ type: "import-status", ref: REF }, SENDER)).resolves.toMatchObject({ state: "ready" });
    release();
    await expect(first).resolves.toMatchObject({ ok: true });
    expect(responder.isRunning()).toBe(false);
    expect(client.paramsOf("analyze")).toHaveLength(1);
    await expect(responder.answer(applyRequest({ dry_run: true }), SENDER)).resolves.toMatchObject({ ok: true });
  });

  it("an empty list is ok with no outcomes and no reaction", async () => {
    const { responder, client, reactions } = setup(ready([prod(1)]));
    const reply = await runApply(responder, { products: [] });
    expect(reply).toEqual({
      ok: true,
      experiment: { ref: REF, code: "ABC-008" },
      products: [],
      reaction: "none",
      reaction_message: null,
    });
    expect(client.calls).toEqual([]);
    expect(reactions).toEqual([]);
  });
});

// ------------------------------------------------------------------ exists

describe("apply: the product already has an ID here", () => {
  const ID = "000042_01_001";
  function existing(stored: YieldInfo | null | undefined, index = 1) {
    const b = batch({ full_id: ID, experiment_ref: REF, product_index: index });
    if (stored !== undefined) b.yield = stored;
    return analyzeBy({
      [index]: {
        status: "registered_here",
        id: ID,
        core_stereo_id: "000042_01",
        name: nameInfo("ethanol"),
        batches: [batch({ full_id: "000042_01_000", experiment_ref: "1" }), b],
      },
    });
  }

  it("creates nothing and stores a different yield (mirror)", async () => {
    const { responder, client, source } = setup(ready([prod(1, { isolated: 81.456, analytical: 70 })]), {
      analyze: existing(yieldInfo(80)),
    });
    const reply = await runApply(responder);
    expect(client.ops()).toEqual(["analyze", "set_yield"]);
    expect(source.rechecks).toEqual([]);
    expect(client.paramsOf("set_yield")[0]).toEqual({
      id: ID,
      experiment_ref: REF,
      product_index: 1,
      isolated: 81.46,
      analytical: 70,
    });
    expect(only(reply)).toMatchObject({
      index: 1,
      kind: "exists",
      id: ID,
      core_stereo_id: "000042_01",
      status: "registered_here",
      compound_key: "digest-1",
      name: "ethanol",
      yields: { isolated: 81.456, analytical: 70 },
      yield_result: "saved",
      message: null,
      notes: [],
    });
  });

  it("removes the stored yield when the page shows both empty", async () => {
    const { responder, client } = setup(ready([prod(1, { isolated: null, analytical: null })]), {
      analyze: existing(yieldInfo(80)),
    });
    const reply = await runApply(responder);
    expect(client.paramsOf("set_yield")).toEqual([
      { id: ID, experiment_ref: REF, product_index: 1, isolated: null, analytical: null },
    ]);
    expect(only(reply).yield_result).toBe("saved");
  });

  it("sends nothing when the yields are equal (also both empty)", async () => {
    for (const [page, stored] of [
      [{ isolated: 76.2, analytical: null }, yieldInfo(76.2)],
      [{ isolated: null, analytical: null }, null],
    ] as const) {
      const { responder, client } = setup(ready([prod(1, page)]), { analyze: existing(stored) });
      const reply = await runApply(responder);
      expect(client.ops()).toEqual(["analyze"]);
      expect(only(reply).yield_result).toBe("unchanged");
    }
  });

  it("sends nothing when the page's yields are not usable or the host reports no yields", async () => {
    for (const [page, stored] of [
      [null, yieldInfo(80)],
      [{ isolated: 50, analytical: null }, undefined],
    ] as const) {
      const { responder, client } = setup(ready([prod(1, page)]), { analyze: existing(stored) });
      const reply = await runApply(responder);
      expect(client.ops()).toEqual(["analyze"]);
      expect(only(reply).yield_result).toBe("none");
    }
  });

  it("does not store a yield on a batch registered for another product row", async () => {
    const analyze: Handler = () =>
      ok(
        analyzeResult({
          status: "registered_here",
          id: ID,
          batches: [batch({ full_id: ID, experiment_ref: REF, product_index: 2, yield: yieldInfo(10) })],
        }),
      );
    const { responder, client } = setup(ready([prod(1, { isolated: 50, analytical: null })]), { analyze });
    const reply = await runApply(responder);
    expect(client.ops()).toEqual(["analyze"]);
    expect(only(reply)).toMatchObject({ kind: "exists", yield_result: "none" });
  });

  it("a failed yield save is a note", async () => {
    const { responder } = setup(ready([prod(1, { isolated: 50, analytical: null })]), {
      analyze: existing(null),
      set_yield: () => fail("Batch not registered there.", "yield_mismatch"),
    });
    const outcome = only(await runApply(responder));
    expect(outcome.yield_result).toBe("failed");
    expect(outcome.notes).toEqual(["The yield could not be saved: Batch not registered there."]);
  });

  it("set_yield answering 'not changed' gives unchanged", async () => {
    const { responder } = setup(ready([prod(1, { isolated: 50, analytical: null })]), {
      analyze: existing(null),
      set_yield: (p) => ok({ id: (p as SetYieldParams).id, yield: yieldInfo(50), changed: false, warnings: [] }),
    });
    expect(only(await runApply(responder)).yield_result).toBe("unchanged");
  });
});

// ------------------------------------------------------------------ created

describe("apply: creating an ID", () => {
  it("rechecks, registers once with confirm from the analysis, then stores the yield", async () => {
    const analyze = analyzeBy({
      2: {
        status: "known_core",
        core_stereo_id: null,
        structure: { inchikey: "KEY-2", stereo_mode: "rac", key_digest: "digest-2" },
      },
    });
    const { responder, client, source } = setup(ready([prod(1), prod(2, { isolated: 64.2, analytical: null })]), {
      analyze,
      register: registerFrom({ core_created: false, stereo_created: true }),
    });
    const reply = await runApply(responder, { products: [2] });
    expect(client.ops()).toEqual(["analyze", "register", "set_yield"]);
    expect(client.paramsOf("analyze")[0]).toEqual({
      molfile: molfileOf(2),
      mode: null,
      experiment_ref: REF,
      product_index: 2,
    });
    expect(source.rechecks).toEqual([
      { productIndex: 2, label: "Product 2", structure: { molfile: molfileOf(2) }, changeToken: productToken(REF, 2, molfileOf(2)) },
    ]);
    expect(client.paramsOf("register")).toEqual([
      {
        molfile: molfileOf(2),
        mode: null,
        experiment_ref: REF,
        experiment_code: "ABC-008",
        experiment_url: experimentPageUrl(REF),
        product_index: 2,
        confirm: { inchikey: "KEY-2", stereo_mode: "rac", key_digest: "digest-2" },
      },
    ]);
    expect(client.paramsOf("set_yield")).toEqual([
      { id: "000042_01_002", experiment_ref: REF, product_index: 2, isolated: 64.2, analytical: null },
    ]);
    expect(only(reply)).toMatchObject({
      index: 2,
      kind: "created",
      id: "000042_01_002",
      core_stereo_id: "000042_01",
      status: "known_core",
      compound_key: "digest-2",
      inchikey: "KEY-2",
      stereo_mode: "rac",
      core_created: false,
      stereo_created: true,
      message: null,
      yield_result: "saved",
    });
  });

  it("no set_yield after create when the page shows no yield", async () => {
    const { responder, client } = setup(ready([prod(1, { isolated: null, analytical: null })]));
    const outcome = only(await runApply(responder));
    expect(client.ops()).toEqual(["analyze", "register"]);
    expect(outcome).toMatchObject({ kind: "created", yield_result: "unchanged" });
  });

  it("asks for a name only for a new stereo variant and only with `names`", async () => {
    const cases = [
      { names: true, stereo: true, named: true },
      { names: true, stereo: false, named: false },
      { names: false, stereo: true, named: false },
    ];
    for (const c of cases) {
      const { responder, client } = setup(ready([prod(1, { isolated: 50, analytical: null })]), {
        register: registerFrom({ stereo_created: c.stereo, core_created: c.stereo }),
        generate_name: (p) =>
          ok(generateNameResult({ core_stereo_id: (p as { core_stereo_id: string }).core_stereo_id })),
      });
      const outcome = only(await runApply(responder, { names: c.names }));
      if (c.named) {
        expect(client.ops()).toEqual(["analyze", "register", "set_yield", "generate_name"]);
        expect(client.paramsOf("generate_name")).toEqual([{ core_stereo_id: "000042_01", replace: false }]);
        expect(outcome.name).toBe("(2S)-2-aminopropan-1-ol");
      } else {
        expect(client.ops()).toEqual(["analyze", "register", "set_yield"]);
        expect(outcome.name).toBeNull();
      }
    }
  });

  it("no name is not a note; an error reply to the name request is", async () => {
    const quiet = setup(ready([prod(1)]), {
      register: registerFrom({ stereo_created: true }),
      generate_name: () => ok(generateNameResult({ name: null, generated: false, changed: false, message: "ChemDraw has no name." })),
    });
    const a = only(await runApply(quiet.responder, { names: true }));
    expect(a.notes).toEqual([]);
    expect(a.name).toBeNull();

    const loud = setup(ready([prod(1)]), {
      register: registerFrom({ stereo_created: true }),
      generate_name: () => fail("The naming program did not answer in time.", "host_timeout"),
    });
    const b = only(await runApply(loud.responder, { names: true }));
    expect(b.kind).toBe("created");
    expect(b.notes).toEqual(["No name could be got from the naming program: The naming program did not answer in time."]);
  });

  it("register warnings (folder) and analysis warnings are notes", async () => {
    const { responder } = setup(ready([prod(1)]), {
      analyze: analyzeBy({ 1: { structure: { warnings: ["Unusual valence"] } } }),
      register: registerFrom({ warnings: ["The compound's folder could not be made: access denied."] }),
    });
    const outcome = only(await runApply(responder, { allow_warnings: true }));
    expect(outcome.kind).toBe("created");
    expect(outcome.notes).toEqual(["Unusual valence", "The compound's folder could not be made: access denied."]);
  });

  it("the formula is the sum formula of everything drawn when the host gives it (a salt)", async () => {
    const { responder } = setup(ready([prod(1), prod(2)]), {
      analyze: analyzeBy({
        1: { structure: { formula: "C14H15N", properties: properties({ sum_formula: "C14H16ClN" }) } },
        2: { structure: { formula: "C2H6O", properties: null } },
      }),
    });
    const reply = await runApply(responder, { products: [1, 2], dry_run: true });
    expect(reply.products.map((o) => o.formula)).toEqual(["C14H16ClN", "C2H6O"]);
  });

  it.each([
    [{ result: "changed" } as RecheckResult, RECHECK_CHANGED_IMPORT],
    [
      { result: "unavailable", message: "The page did not answer in time." } as RecheckResult,
      "The structure could not be re-checked on the page, so no ID was created: The page did not answer in time.",
    ],
  ])("recheck %j: failed, nothing registered", async (recheck, message) => {
    const { responder, client, source, reactions } = setup(ready([prod(1)]));
    source.recheckResult = recheck;
    const outcome = only(await runApply(responder));
    expect(client.ops()).toEqual(["analyze"]);
    expect(outcome).toMatchObject({ kind: "failed", id: null, status: "new", compound_key: "digest-1", message });
    expect(reactions).toEqual([]);
  });

  it("a failing register does not stop the next product", async () => {
    const register: Handler = (p) =>
      (p as RegisterParams).product_index === 1 ? fail("The registry is locked.") : registerFrom()(p);
    const { responder, client } = setup(ready([prod(1), prod(2)]), { register });
    const reply = await runApply(responder, { products: [1, 2] });
    expect(client.paramsOf("register").map((p) => p.product_index)).toEqual([1, 2]);
    expect(reply.products.map((o) => [o.index, o.kind, o.message])).toEqual([
      [1, "failed", "The registry is locked."],
      [2, "created", null],
    ]);
  });

  it("a failing analysis gives failed with no analysis values", async () => {
    const { responder, client } = setup(ready([prod(1)]), { analyze: () => fail("Unreadable molfile.") });
    const outcome = only(await runApply(responder));
    expect(client.ops()).toEqual(["analyze"]);
    expect(outcome).toMatchObject({ kind: "failed", status: null, compound_key: null, formula: null, message: "Unreadable molfile." });
  });
});

// ------------------------------------------------------------------ attention and dry run

describe("apply: attention and dry run", () => {
  it.each([
    [
      "slot conflict",
      { slot_conflict: batch({ full_id: "EX0003_01_001", experiment_ref: REF }) },
      "This product's row already holds EX0003_01_001 for another structure. Open the experiment and decide in the panel.",
    ],
    [
      "ignored stereo",
      { structure: { ignored_stereo: ["double bond C3=C4", "atom C7"] } },
      "The drawing contains stereo information the registry does not use (double bond C3=C4; atom C7). Open the experiment and accept it in the panel.",
    ],
    [
      "warnings",
      { structure: { warnings: ["Unusual valence on N2"] } },
      "The structure has warnings (Unusual valence on N2). Open the experiment and check it in the panel.",
    ],
  ])("%s: attention, nothing created", async (_name, over, message) => {
    const { responder, client, source, reactions } = setup(ready([prod(1)]), {
      analyze: analyzeBy({ 1: over as Parameters<typeof analyzeResult>[0] }),
    });
    const reply = await runApply(responder, { names: true });
    expect(client.ops()).toEqual(["analyze"]);
    expect(source.rechecks).toEqual([]);
    expect(only(reply)).toMatchObject({ kind: "attention", id: null, message, yield_result: "none" });
    expect(reply.reaction).toBe("none");
    expect(reactions).toEqual([]);
  });

  it("allow_warnings lets a structure with warnings through, but never ignored stereo", async () => {
    const warned = setup(ready([prod(1)]), { analyze: analyzeBy({ 1: { structure: { warnings: ["w"] } } }) });
    expect(only(await runApply(warned.responder, { allow_warnings: true })).kind).toBe("created");

    const stereo = setup(ready([prod(1)]), { analyze: analyzeBy({ 1: { structure: { ignored_stereo: ["s"] } } }) });
    expect(only(await runApply(stereo.responder, { allow_warnings: true })).kind).toBe("attention");
  });

  it("attention keeps the variant the compound already has", async () => {
    const { responder } = setup(ready([prod(1)]), {
      analyze: analyzeBy({
        1: { status: "known_compound", core_stereo_id: "000042_01", name: nameInfo("ethanol"), structure: { warnings: ["w"] } },
      }),
    });
    expect(only(await runApply(responder))).toMatchObject({
      kind: "attention",
      core_stereo_id: "000042_01",
      status: "known_compound",
      name: "ethanol",
      notes: ["w"],
    });
  });

  it("a dry run only looks up", async () => {
    const analyze = analyzeBy({
      1: {
        status: "registered_here",
        id: "000042_01_001",
        batches: [batch({ full_id: "000042_01_001", experiment_ref: REF, yield: yieldInfo(10) })],
      },
      2: { status: "known_compound", core_stereo_id: "000042_02" },
      3: { slot_conflict: batch({ full_id: "000050_01_001" }) },
    });
    const { responder, client, source, reactions } = setup(
      ready([prod(1, { isolated: 90, analytical: null }), prod(2), prod(3)]),
      { analyze },
    );
    const reply = await runApply(responder, { products: [1, 2, 3], dry_run: true, names: true });
    expect(client.ops()).toEqual(["analyze", "analyze", "analyze"]);
    expect(source.rechecks).toEqual([]);
    expect(reply.products.map((o) => [o.kind, o.id, o.core_stereo_id, o.yield_result])).toEqual([
      ["exists", "000042_01_001", null, "none"],
      ["would_create", null, "000042_02", "none"],
      ["attention", null, null, "none"],
    ]);
    expect(reply.reaction).toBe("none");
    expect(reactions).toEqual([]);
  });
});

// ------------------------------------------------------------------ products, order, reaction

describe("apply: products, order and the reaction", () => {
  it("handles products in ascending order whatever order was asked for", async () => {
    const { responder, client } = setup(ready([prod(1), prod(2), prod(3)]));
    const reply = await runApply(responder, { products: [3, 1, 2], dry_run: true });
    expect(reply.products.map((o) => o.index)).toEqual([1, 2, 3]);
    expect(client.paramsOf("analyze").map((p) => p.product_index)).toEqual([1, 2, 3]);
  });

  it("a product number the page does not show is failed; so is a row without a structure", async () => {
    const { responder, client } = setup(ready([prod(1)], [{ productIndex: 2, label: "Product 2", reason: NO_STRUCTURE }]));
    const reply = await runApply(responder, { products: [1, 2, 7], dry_run: true });
    expect(client.paramsOf("analyze").map((p) => p.product_index)).toEqual([1]);
    expect(reply.products.map((o) => [o.index, o.kind, o.message])).toEqual([
      [1, "would_create", null],
      [2, "failed", NO_STRUCTURE],
      [7, "failed", "The experiment shows no product 7."],
    ]);
  });

  it("stores the reaction once when products exist or were created", async () => {
    const analyze = analyzeBy({
      1: { status: "registered_here", id: "000042_01_001", batches: [batch({ experiment_ref: REF })] },
    });
    const { responder, reactions, client } = setup(ready([prod(1), prod(2), prod(3)]), { analyze });
    const reply = await runApply(responder, { products: [1, 2, 3] });
    expect(reply.products.map((o) => o.kind)).toEqual(["exists", "created", "created"]);
    expect(reactions).toEqual([EXP]);
    expect(reply.reaction).toBe("stored");
    expect(reply.reaction_message).toBeNull();
    // The reaction is stored by its own function, not through these host calls.
    expect(client.ops()).not.toContain("set_reaction");
  });

  it("stores the reaction when the only product already exists", async () => {
    const analyze = analyzeBy({ 1: { status: "registered_here", id: "000042_01_001", batches: [] } });
    const { responder, reactions } = setup(ready([prod(1)]), { analyze });
    expect((await runApply(responder)).reaction).toBe("stored");
    expect(reactions).toHaveLength(1);
  });

  it("does not store the reaction when nothing has an ID", async () => {
    const { responder, reactions } = setup(ready([prod(1)]), { register: () => fail("no") });
    const reply = await runApply(responder);
    expect(only(reply).kind).toBe("failed");
    expect(reply.reaction).toBe("none");
    expect(reactions).toEqual([]);
  });

  it("reports a failed reaction store", async () => {
    const { responder, store } = setup(ready([prod(1)]));
    store.outcome = { result: "failed", message: "The reaction of ABC-008 could not be saved: refused." };
    const reply = await runApply(responder);
    expect(reply.reaction).toBe("failed");
    expect(reply.reaction_message).toBe("The reaction of ABC-008 could not be saved: refused.");
  });

  it("a product whose experiment went away meanwhile is failed, and no reaction is stored", async () => {
    const { responder, source, client, reactions } = setup(ready([prod(1), prod(2)]));
    client.handlers.register = (p) => {
      source.state = { kind: "idle", message: "gone" };
      return registerFrom()(p);
    };
    const reply = await runApply(responder, { products: [1, 2] });
    expect(reply.products.map((o) => [o.kind, o.message])).toEqual([
      ["created", null],
      ["failed", "The experiment is no longer shown in this tab."],
    ]);
    expect(reply.reaction).toBe("failed");
    expect(reactions).toEqual([]);
  });

  it("an exception from the host client is a failed product, not a broken apply", async () => {
    const { responder } = setup(ready([prod(1), prod(2)]), {
      analyze: (p) => {
        if ((p as AnalyzeParams).product_index === 1) throw new Error("boom");
        return analyzeBy()(p);
      },
    });
    const reply = await runApply(responder, { products: [1, 2], dry_run: true });
    expect(reply.products.map((o) => [o.kind, o.message])).toEqual([
      ["failed", "boom"],
      ["would_create", null],
    ]);
  });
});

// ------------------------------------------------------------------ listener

describe("the onMessage listener", () => {
  it("returns false at once and never answers when the frame does not answer", () => {
    const { responder } = setup({ kind: "idle", message: "nothing" });
    const listener = importMessageListener(responder);
    const sent: unknown[] = [];
    expect(listener({ type: "import-status", ref: REF }, SENDER, (r) => sent.push(r))).toBe(false);
    expect(listener({ type: "import-status", ref: REF }, { id: "x", url: PAGE_URL }, (r) => sent.push(r))).toBe(false);
    expect(sent).toEqual([]);
  });

  it("returns true (not a Promise) and answers through sendResponse", async () => {
    const { responder } = setup(ready([prod(1)]));
    const listener = importMessageListener(responder);
    const sent: unknown[] = [];
    const result = listener({ type: "import-status", ref: REF }, SENDER, (r) => sent.push(r));
    expect(result).toBe(true);
    await flush();
    expect(sent).toEqual([importStatus(ready([prod(1)]), REF)]);

    const applied: unknown[] = [];
    expect(listener(applyRequest({ dry_run: true }), SENDER, (r) => applied.push(r))).toBe(true);
    for (let i = 0; i < 5 && applied.length === 0; i++) await flush();
    expect(applied).toHaveLength(1);
    expect(applied[0]).toMatchObject({ ok: true, reaction: "none" });
  });

  it("a sendResponse that throws (page gone) is swallowed", async () => {
    const { responder } = setup(ready([prod(1)]));
    const listener = importMessageListener(responder);
    expect(
      listener({ type: "import-status", ref: REF }, SENDER, () => {
        throw new Error("Could not establish connection.");
      }),
    ).toBe(true);
    await flush();
  });
});

// ------------------------------------------------------------------ the reaction, once

describe("storeReactionOnce", () => {
  interface PageFixture {
    experiments: Record<string, { reaction: PageReaction; molfiles: Record<string, string | null> }>;
  }
  const pages = JSON.parse(readFileSync(join(here, "fixtures", "page-reactions.json"), "utf8")) as PageFixture;
  const fixture = pages.experiments["ABC-002"]!;

  class Bridge implements ReactionBridge {
    reactionCalls = 0;
    molfileCalls: string[] = [];
    constructor(private readonly page: PageReaction | null) {}
    async reaction(): Promise<ReactionResult> {
      this.reactionCalls += 1;
      return this.page
        ? { ok: true, reaction: JSON.parse(JSON.stringify(this.page)) as PageReaction }
        : { ok: false, code: "no_widget", message: "No experiment." };
    }
    async molfile(domId: string): Promise<MolfileResult> {
      this.molfileCalls.push(domId);
      const mol = fixture.molfiles[domId];
      return mol ? { ok: true, molfile: mol } : { ok: false, code: "no_widget", message: "Gone." };
    }
  }

  const ABC002: ExperimentContext = { ref: "4120", code: "ABC-002", url: experimentPageUrl("4120") };

  it("sends set_reaction once and says it was stored", async () => {
    const bridge = new Bridge(fixture.reaction);
    const client = new AutoClient({
      set_reaction: (p) => ok({ experiment_ref: (p as SetReactionParams).experiment_ref, changed: true, updated_at: "x", warnings: [] }),
    });
    const outcome = await storeReactionOnce({ bridge, client, experiment: ABC002 });
    expect(outcome).toEqual({ result: "stored", message: null });
    expect(client.ops()).toEqual(["set_reaction"]);
    const params = client.paramsOf("set_reaction")[0]!;
    expect(params.experiment_ref).toBe("4120");
    expect(params.experiment_code).toBe("ABC-002");
    expect(params.experiment_url).toBe(experimentPageUrl("4120"));
    expect(bridge.reactionCalls).toBe(2);
  });

  it("says why when the registry refuses", async () => {
    const client = new AutoClient({ set_reaction: () => fail("No product of this experiment is registered.") });
    const outcome = await storeReactionOnce({ bridge: new Bridge(fixture.reaction), client, experiment: ABC002 });
    expect(outcome).toEqual({
      result: "failed",
      message: "The reaction of ABC-002 could not be saved: No product of this experiment is registered.",
    });
  });

  it("says why when the page cannot be read, and sends nothing", async () => {
    const client = new AutoClient();
    const outcome = await storeReactionOnce({ bridge: new Bridge(null), client, experiment: ABC002 });
    expect(outcome).toEqual({ result: "failed", message: "The reaction of ABC-002 could not be read from the page: No experiment." });
    expect(client.calls).toEqual([]);
  });
});
