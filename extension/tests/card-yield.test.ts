import { describe, expect, it } from "vitest";
import {
  CardController,
  cardYield,
  initialCardState,
  yieldNote,
  yieldToSave,
  type CardInput,
  type CardState,
} from "../src/content/card-state";
import type { PageYields } from "../src/shared/adapter";
import type { AnalyzeResult, BackgroundReply, Batch, SetYieldParams, SetYieldResult, YieldInfo } from "../src/shared/protocol";
import { FakeClient, analyzeResult, batch, experiment, flush, manualTimers, product, registerResult } from "./helpers";

const ok = <T>(result: T): BackgroundReply => ({ ok: true, result: result as never });
const fail = (code: string, message: string): BackgroundReply => ({ ok: false, error: { code, message } });

const ID = "000042_01_003";
const info = (isolated: number | null, analytical: number | null = null): YieldInfo => ({
  isolated,
  analytical,
  updated_at: "2026-11-18T09:00:00+00:00",
});

/** The batch of this card's product: experiment 4180, product 1. */
function hereBatch(over: Partial<Batch> = {}): Batch {
  return batch({ full_id: ID, batch_no: 3, experiment_ref: "4180", experiment_code: "ABC-008", product_index: 1, yield: null, ...over });
}

function registered(over: Partial<Batch> = {}, extra: Partial<AnalyzeResult> = {}): AnalyzeResult {
  return analyzeResult({
    status: "registered_here",
    id: ID,
    core_stereo_id: "000042_01",
    batches: [batch({ full_id: "000042_01_001", experiment_ref: "1", yield: info(50) }), hereBatch(over)],
    ...extra,
  });
}

const input = (yields: PageYields | null | undefined, over: Partial<CardInput["product"]> = {}, exp = experiment()): CardInput => ({
  product: product({ label: "Product 1", ...(yields === undefined ? {} : { yields }), ...over }),
  experiment: exp,
});

function setup(options: { saveYields?: boolean } = {}) {
  const client = new FakeClient();
  const timers = manualTimers();
  const card = new CardController(
    client,
    { onChange: () => {} },
    { autoLookup: true, confirmBeforeCreate: false, schedule: timers.schedule, saveYields: true, ...options },
  );
  return { client, timers, card };
}

async function analysed(yields: PageYields | null | undefined, result: AnalyzeResult = registered(), options: { saveYields?: boolean } = {}) {
  const s = setup(options);
  s.card.setInput(input(yields));
  s.timers.run();
  s.client.last("analyze").resolve(ok(result));
  await flush();
  return s;
}

const setYieldResult = (params: SetYieldParams, over: Partial<SetYieldResult> = {}): SetYieldResult => ({
  id: params.id,
  yield: params.isolated === null && params.analytical === null ? null : info(params.isolated, params.analytical),
  changed: true,
  warnings: [],
  ...over,
});

async function answerYield(client: FakeClient, over: Partial<SetYieldResult> = {}) {
  const call = client.last("set_yield");
  call.resolve(ok(setYieldResult(call.params as SetYieldParams, over)));
  await flush();
}

describe("storing the page's yields with the ID", () => {
  it("sends set_yield with both values after a lookup that finds the ID here", async () => {
    const { client } = await analysed({ isolated: 76.2, analytical: null });
    expect(client.callsOf("set_yield")).toHaveLength(1);
    expect(client.last("set_yield").params).toEqual({
      id: ID,
      experiment_ref: "4180",
      product_index: 1,
      isolated: 76.2,
      analytical: null,
    });
  });

  it("rounds to two decimals and sends nothing when the rounded values are what is stored", async () => {
    const a = await analysed({ isolated: 73.651, analytical: 10 }, registered({ yield: info(73.65, 10) }));
    expect(a.client.callsOf("set_yield")).toHaveLength(0);
    const b = await analysed({ isolated: 73.656, analytical: null }, registered({ yield: info(73.65) }));
    expect(b.client.last("set_yield").params).toMatchObject({ isolated: 73.66, analytical: null });
  });

  it("mirrors the page: both fields empty removes a stored yield, and nothing is sent when none is stored", async () => {
    const a = await analysed({ isolated: null, analytical: null }, registered({ yield: info(76.2) }));
    expect(a.client.last("set_yield").params).toMatchObject({ isolated: null, analytical: null });
    const b = await analysed({ isolated: null, analytical: null }, registered({ yield: null }));
    expect(b.client.callsOf("set_yield")).toHaveLength(0);
  });

  it.each<[string, PageYields | null | undefined]>([
    ["the fields cannot be used now (edit mode)", null],
    ["the source has no yields (manual form)", undefined],
  ])("sends nothing when %s", async (_name, yields) => {
    const { client } = await analysed(yields);
    expect(client.callsOf("set_yield")).toHaveLength(0);
  });

  it.each<[string, AnalyzeResult]>([
    ["the batch is retired", registered({ retired: true, retired_at: "2026-11-02T00:00:00+00:00" })],
    ["the batch belongs to another experiment", registered({ experiment_ref: "4181" })],
    ["the batch belongs to another product row", registered({ product_index: 2 })],
    ["the host does not report yields (older host)", registered({ yield: undefined })],
    ["the ID is not in the batches", registered({ full_id: "000042_01_009" })],
    ["the compound has no ID in this experiment", analyzeResult({ status: "known_compound", core_stereo_id: "000042_01", batches: [hereBatch({ experiment_ref: "1" })] })],
  ])("sends nothing when %s", async (_name, result) => {
    const { client } = await analysed({ isolated: 76.2, analytical: null }, result);
    expect(client.callsOf("set_yield")).toHaveLength(0);
  });

  it("an absent yield key means an older host: nothing is sent", async () => {
    const old = registered();
    const b = old.batches[1] as Partial<Batch>;
    delete b.yield;
    expect("yield" in old.batches[1]!).toBe(false);
    const { client } = await analysed({ isolated: 76.2, analytical: null }, old);
    expect(client.callsOf("set_yield")).toHaveLength(0);
  });

  it("sends nothing without an experiment", async () => {
    const s = setup();
    s.card.setInput({ product: product({ yields: { isolated: 1, analytical: null } }), experiment: null });
    s.timers.run();
    s.client.last("analyze").resolve(ok(registered()));
    await flush();
    expect(s.client.callsOf("set_yield")).toHaveLength(0);
  });

  it("never sends without the saveYields option (the manual form)", async () => {
    const { client } = await analysed({ isolated: 76.2, analytical: null }, registered(), { saveYields: false });
    expect(client.callsOf("set_yield")).toHaveLength(0);
  });

  it("remembers the stored yield: the card shows it and nothing is sent again", async () => {
    const { client, card, timers } = await analysed({ isolated: 76.2, analytical: 78 });
    await answerYield(client);
    expect(cardYield(card.state)).toMatchObject({ isolated: 76.2, analytical: 78 });
    // The page publishes the same values again, and the analysis (older than the answer) still says null.
    card.setInput(input({ isolated: 76.2, analytical: 78 }, { label: "P1" }));
    card.setInput(input({ isolated: 76.2, analytical: 78 }));
    timers.run();
    await flush();
    expect(client.callsOf("set_yield")).toHaveLength(1);
    expect(client.callsOf("analyze")).toHaveLength(1);
    expect(card.view().stale).toBe(false);
  });

  it("a change of the page's values sends one request per change, one at a time", async () => {
    const { client, card, timers } = await analysed({ isolated: 10, analytical: null });
    expect(client.callsOf("set_yield")).toHaveLength(1);
    card.setInput(input({ isolated: 20, analytical: null }));
    card.setInput(input({ isolated: 30, analytical: null }));
    timers.run();
    // Never analysed again for a yield change, and nothing else is sent while one runs.
    expect(client.callsOf("analyze")).toHaveLength(1);
    expect(client.callsOf("set_yield")).toHaveLength(1);
    await answerYield(client);
    expect(client.callsOf("set_yield")).toHaveLength(2);
    expect(client.last("set_yield").params).toMatchObject({ isolated: 30 });
    await answerYield(client);
    expect(client.callsOf("set_yield")).toHaveLength(2);
    expect(cardYield(card.state)).toMatchObject({ isolated: 30 });
  });

  it("a yield change keeps an open confirmation and the analysis as they are", async () => {
    const client = new FakeClient();
    const card = new CardController(client, { onChange: () => {} }, { confirmBeforeCreate: true, saveYields: true });
    card.setInput(input({ isolated: 1, analytical: null }));
    const l = card.lookUp();
    client.last("analyze").resolve(ok(analyzeResult()));
    await l;
    card.requestCreate();
    expect(card.state.confirming).not.toBeNull();
    card.setInput(input({ isolated: 2, analytical: null }));
    expect(card.state.confirming).not.toBeNull();
    expect(card.view().stale).toBe(false);
    expect(client.callsOf("set_yield")).toHaveLength(0); // no ID here yet
  });

  it("after a failure shows a short note and does not send the same values again", async () => {
    const { client, card } = await analysed({ isolated: 76.2, analytical: null });
    client.last("set_yield").resolve(fail("storage_error", "The database is locked."));
    await flush();
    expect(yieldNote(card.state)).toBe("The yield could not be saved: The database is locked.");
    card.setInput(input({ isolated: 76.2, analytical: null }, { label: "again" }));
    expect(client.callsOf("set_yield")).toHaveLength(1);
    // Other values on the page: sent once.
    card.setInput(input({ isolated: 82, analytical: null }));
    expect(client.callsOf("set_yield")).toHaveLength(2);
    expect(yieldNote(card.state)).toBeNull();
    await answerYield(client);
    expect(yieldNote(card.state)).toBeNull();
  });

  it("after a failure sends the same values again only once the product was analysed again", async () => {
    const { client, card } = await analysed({ isolated: 76.2, analytical: null });
    client.last("set_yield").resolve(fail("host_timeout", "late"));
    await flush();
    expect(client.callsOf("set_yield")).toHaveLength(1);
    const l = card.lookUp();
    client.last("analyze").resolve(ok(registered()));
    await l;
    expect(client.callsOf("set_yield")).toHaveLength(2);
  });

  it("never retries by itself: no timer is left behind after a failure", async () => {
    const { client, timers } = await analysed({ isolated: 76.2, analytical: null });
    client.last("set_yield").resolve(fail("yield_mismatch", "This ID was not registered for product 1 of experiment 4180."));
    await flush();
    expect(timers.pending()).toBe(0);
    timers.run();
    await flush();
    expect(client.calls.map((c) => c.op)).toEqual(["analyze", "set_yield"]);
  });

  it("ignores the answer when the card shows another product row meanwhile", async () => {
    const { client, card, timers } = await analysed({ isolated: 76.2, analytical: null });
    card.setInput(input({ isolated: 76.2, analytical: null }, { productIndex: 2 }));
    await answerYield(client);
    expect(card.state.yieldSync.stored).toBeNull();
    expect(card.state.yieldSync.sending).toBeNull();
    timers.run();
    client.last("analyze").resolve(ok(registered({ product_index: 2 })));
    await flush();
    // The new place is stored once, with the new product no.
    expect(client.callsOf("set_yield")).toHaveLength(2);
    expect(client.last("set_yield").params).toMatchObject({ product_index: 2 });
  });

  it("ignores the answer when the structure changed meanwhile, and sends nothing for a stale analysis", async () => {
    const { client, card, timers } = await analysed({ isolated: 76.2, analytical: null });
    card.setInput(input({ isolated: 90, analytical: null }, { changeToken: "t2" }));
    client.last("set_yield").resolve(fail("storage_error", "x"));
    await flush();
    expect(card.state.yieldSync.failed).toBeNull();
    expect(client.callsOf("set_yield")).toHaveLength(1);
    timers.run();
    client.last("analyze").resolve(ok(registered({ yield: info(76.2) })));
    await flush();
    expect(client.callsOf("set_yield")).toHaveLength(2);
    expect(client.last("set_yield").params).toMatchObject({ isolated: 90 });
  });

  it("an analysis requested before the answer does not undo it; a later one is used as it is", async () => {
    const { client, card } = await analysed({ isolated: 76.2, analytical: null });
    const l = card.lookUp(); // sent while set_yield runs
    await answerYield(client);
    client.last("analyze").resolve(ok(registered({ yield: null })));
    await l;
    expect(client.callsOf("set_yield")).toHaveLength(1);
    expect(cardYield(card.state)).toMatchObject({ isolated: 76.2 });
    const l2 = card.lookUp(); // requested after the answer: what it says counts
    client.last("analyze").resolve(ok(registered({ yield: info(76.2) })));
    await l2;
    expect(client.callsOf("set_yield")).toHaveLength(1);
  });

  it("does not loop when the registry stores other values than were sent", async () => {
    const { client, card } = await analysed({ isolated: 76.2, analytical: null });
    await answerYield(client, { yield: info(80) });
    expect(client.callsOf("set_yield")).toHaveLength(1);
    expect(yieldNote(card.state)).toBe("The yield could not be saved: The registry program stored other values.");
  });

  it("ignores an answer that arrives after the card was removed", async () => {
    const { client, card } = await analysed({ isolated: 76.2, analytical: null });
    card.dispose();
    await answerYield(client);
    card.setInput(input({ isolated: 1, analytical: null }));
    expect(client.callsOf("set_yield")).toHaveLength(1);
  });
});

describe("yields right after Create ID", () => {
  async function created(yields: PageYields | null, batchOver: Partial<Batch> = {}) {
    const s = setup();
    s.card.setInput(input(yields));
    s.timers.run();
    s.client.last("analyze").resolve(ok(analyzeResult()));
    await flush();
    const p = s.card.create();
    s.client
      .last("register")
      .resolve(ok(registerResult({ id: ID, batch: hereBatch(batchOver) })));
    await p;
    return s;
  }

  it("stores the page's yields with the new ID before the lookup that follows", async () => {
    const { client, card } = await created({ isolated: 76.2, analytical: 78 });
    expect(client.calls.map((c) => c.op)).toEqual(["analyze", "register", "set_yield", "analyze"]);
    expect(client.last("set_yield").params).toEqual({ id: ID, experiment_ref: "4180", product_index: 1, isolated: 76.2, analytical: 78 });
    client.last("analyze").resolve(ok(registered({ yield: null })));
    await flush();
    await answerYield(client);
    expect(client.callsOf("set_yield")).toHaveLength(1);
    expect(cardYield(card.state)).toMatchObject({ isolated: 76.2, analytical: 78 });
  });

  it("sends nothing after Create ID when the page shows no yields or the host is older", async () => {
    const a = await created({ isolated: null, analytical: null });
    expect(a.client.callsOf("set_yield")).toHaveLength(0);
    const b = await created({ isolated: 5, analytical: null }, { yield: undefined });
    expect(b.client.callsOf("set_yield")).toHaveLength(0);
    const c = await created(null);
    expect(c.client.callsOf("set_yield")).toHaveLength(0);
  });
});

describe("yieldToSave (pure)", () => {
  function state(over: Partial<CardState>): CardState {
    return { ...initialCardState(), ...over };
  }
  const snapshot = { structure: { molfile: product().structure.molfile! }, mode: null, experimentRef: "4180", productIndex: 1, changeToken: "t1" };

  it("is null while a request runs on the card", () => {
    const base = state({
      input: input({ isolated: 1, analytical: null }),
      analysis: { seq: 1, snapshot, result: registered() },
      latestSeq: 1,
    });
    expect(yieldToSave(base)).not.toBeNull();
    expect(yieldToSave({ ...base, pending: { op: "analyze", seq: 2 } })).toBeNull();
    expect(yieldToSave({ ...base, needsRefresh: true })).toBeNull();
  });
});
