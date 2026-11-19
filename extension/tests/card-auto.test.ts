import { describe, expect, it } from "vitest";
import { CardController, type CardInput } from "../src/content/card-state";
import type { BackgroundReply, RegisterParams } from "../src/shared/protocol";
import { FakeClient, analyzeResult, experiment, flush, manualTimers, product, registerResult } from "./helpers";

const ok = <T>(result: T): BackgroundReply => ({ ok: true, result: result as never });
const input = (over: Partial<CardInput["product"]> = {}, exp = experiment()): CardInput => ({
  product: product(over),
  experiment: exp,
});

function setup(options: { autoLookup?: boolean; confirmBeforeCreate?: boolean } = {}) {
  const client = new FakeClient();
  const timers = manualTimers();
  const card = new CardController(
    client,
    { onChange: () => {} },
    { autoLookup: true, confirmBeforeCreate: false, schedule: timers.schedule, ...options },
  );
  return { client, timers, card };
}

async function analysed(result = analyzeResult()) {
  const s = setup();
  s.card.setInput(input());
  s.timers.run();
  s.client.last("analyze").resolve(ok(result));
  await flush();
  return s;
}

describe("automatic lookup", () => {
  it("looks a product up when it appears, after the debounce", () => {
    const { client, timers, card } = setup();
    card.setInput(input());
    expect(client.calls).toHaveLength(0);
    timers.run();
    expect(client.callsOf("analyze")).toHaveLength(1);
  });

  it("a burst of changes gives one request, for the latest input", () => {
    const { client, timers, card } = setup();
    card.setInput(input({ changeToken: "a" }));
    card.setInput(input({ changeToken: "b" }));
    card.setInput(input({ changeToken: "c", productIndex: 3 }));
    timers.run();
    expect(client.callsOf("analyze")).toHaveLength(1);
    expect(client.last("analyze").params).toMatchObject({ product_index: 3 });
  });

  it.each([
    ["token", input({ changeToken: "t2" })],
    ["experiment", input({}, experiment({ ref: "999" }))],
    ["product no.", input({ productIndex: 2 })],
  ])("looks up again when the %s changes", async (_n, next) => {
    const { client, timers, card } = await analysed();
    card.setInput(next);
    timers.run();
    expect(client.callsOf("analyze")).toHaveLength(2);
  });

  it("does not look up again for a change that does not matter (experiment code)", async () => {
    const { client, timers, card } = await analysed();
    card.setInput(input({}, experiment({ code: "ABC-999" })));
    timers.run();
    expect(client.callsOf("analyze")).toHaveLength(1);
  });

  it("never looks up for a source without autoLookup", () => {
    const { client, timers, card } = setup({ autoLookup: false });
    card.setInput(input());
    timers.run();
    expect(timers.pending()).toBe(0);
    expect(client.calls).toHaveLength(0);
  });

  it("stops after dispose", () => {
    const { client, timers, card } = setup();
    card.setInput(input());
    card.dispose();
    timers.run();
    expect(client.calls).toHaveLength(0);
  });

  it("never creates anything by itself", async () => {
    const { client, timers } = await analysed();
    timers.run();
    await flush();
    expect(client.callsOf("register")).toHaveLength(0);
  });

  it("looks up once after an invalidation, not in a loop", async () => {
    const { client, timers, card } = await analysed();
    card.invalidate();
    timers.run();
    expect(client.callsOf("analyze")).toHaveLength(2);
    client.last("analyze").resolve({ ok: false, error: { code: "registry_changed", message: "again" } });
    await flush();
    card.invalidate();
    timers.run();
    expect(client.callsOf("analyze")).toHaveLength(2);
  });
});

describe("one-click create", () => {
  it("sends register directly with confirm from the analysis on screen", async () => {
    const { client, card } = await analysed(
      analyzeResult({ structure: { inchikey: "ZZZZZZZZZZZZZZ-ZZZZZZZZZZ-N", stereo_mode: "rac", key_digest: "ef".repeat(32) } }),
    );
    await Promise.resolve();
    void card.create();
    expect(card.state.confirming).toBeNull();
    expect(client.callsOf("register")).toHaveLength(1);
    const params = client.last("register").params as RegisterParams;
    expect(params.confirm).toEqual({
      inchikey: "ZZZZZZZZZZZZZZ-ZZZZZZZZZZ-N",
      stereo_mode: "rac",
      key_digest: "ef".repeat(32),
    });
    expect(params.molfile).toBe(product().structure.molfile);
    expect(params.mode).toBeNull();
  });

  it("sends exactly one register on a double click", async () => {
    const { client, card } = await analysed();
    void card.create();
    void card.create();
    void card.create();
    expect(client.callsOf("register")).toHaveLength(1);
  });

  it("does nothing before the analysis is current", async () => {
    const { client, card, timers } = await analysed();
    card.setInput(input({ changeToken: "t2" }));
    await card.create();
    expect(client.callsOf("register")).toHaveLength(0);
    timers.run(); // the automatic lookup for the new token
    await card.create(); // still running
    expect(client.callsOf("register")).toHaveLength(0);
    client.last("analyze").resolve(ok(analyzeResult()));
    await flush();
    void card.create();
    expect(client.callsOf("register")).toHaveLength(1);
  });

  it("with confirmBeforeCreate the same button only opens the confirmation", async () => {
    const { client, card, timers } = setup({ confirmBeforeCreate: true });
    card.setInput(input());
    timers.run();
    client.last("analyze").resolve(ok(analyzeResult()));
    await flush();
    await card.create();
    expect(card.state.confirming).not.toBeNull();
    expect(client.callsOf("register")).toHaveLength(0);
  });

  it("looks up again after a successful register and keeps showing the new ID", async () => {
    const { client, card } = await analysed();
    const p = card.create();
    client.last("register").resolve(ok(registerResult({ id: "000042_01_002", created: true })));
    await p;
    expect(client.callsOf("analyze")).toHaveLength(2);
    expect(card.view().existingId).toBe("000042_01_002");
    client.last("analyze").resolve(ok(analyzeResult({ status: "registered_here", id: "000042_01_002" })));
    await flush();
    expect(card.view().status).toBe("registered_here");
    expect(card.state.registration?.result.created).toBe(true);
    expect(card.view().stale).toBe(false);
  });

  it("looks up again after a failed register", async () => {
    const { client, card } = await analysed();
    const p = card.create();
    client.last("register").resolve({ ok: false, error: { code: "host_timeout", message: "late" } });
    await p;
    expect(client.callsOf("analyze")).toHaveLength(2);
    expect(card.view().canCreate).toBe(false);
    client.last("analyze").resolve(ok(analyzeResult({ status: "registered_here", id: "000042_01_002" })));
    await flush();
    expect(card.view().existingId).toBe("000042_01_002");
  });
});

describe("create with a recheck of the page", () => {
  type Outcome = { result: "unchanged" | "changed" } | { result: "unavailable"; message: string };

  async function withRecheck() {
    const client = new FakeClient();
    const timers = manualTimers();
    const rechecks: { product: unknown; resolve: (o: Outcome) => void }[] = [];
    const card = new CardController(
      client,
      { onChange: () => {} },
      {
        autoLookup: true,
        confirmBeforeCreate: false,
        schedule: timers.schedule,
        recheck: (product) => new Promise<Outcome>((resolve) => rechecks.push({ product, resolve })),
      },
    );
    card.setInput(input());
    timers.run();
    client.last("analyze").resolve(ok(analyzeResult()));
    await flush();
    return { client, timers, card, rechecks };
  }

  it("re-checks first, with the analysed product, and registers once on unchanged", async () => {
    const { client, card, rechecks } = await withRecheck();
    const p = card.create();
    expect(rechecks).toHaveLength(1);
    expect(rechecks[0]!.product).toMatchObject({ productIndex: 1, structure: { molfile: product().structure.molfile }, changeToken: "t1" });
    expect(card.view().busy).toBe("recheck");
    expect(client.callsOf("register")).toHaveLength(0);
    rechecks[0]!.resolve({ result: "unchanged" });
    await flush();
    expect(client.callsOf("register")).toHaveLength(1);
    expect((client.last("register").params as RegisterParams).molfile).toBe(product().structure.molfile);
    expect(card.view().busy).toBe("register");
    client.last("register").resolve(ok(registerResult()));
    await p;
  });

  it("sends nothing on changed, says so, and looks up again", async () => {
    const { client, card, rechecks, timers } = await withRecheck();
    const p = card.create();
    rechecks[0]!.resolve({ result: "changed" });
    await p;
    expect(client.callsOf("register")).toHaveLength(0);
    expect(card.state.recheckNotice).toEqual({
      kind: "changed",
      message:
        "The structure on the page changed, so no ID was created. It was looked up again: check the result and click Create ID again.",
    });
    expect(card.view().canCreate).toBe(false);
    timers.run();
    expect(client.callsOf("analyze")).toHaveLength(2);
    client.last("analyze").resolve(ok(analyzeResult()));
    await flush();
    expect(card.view().canCreate).toBe(true);
    expect(card.state.recheckNotice?.kind).toBe("changed"); // stays until the next Create
  });

  it("sends nothing when unavailable and keeps Create available", async () => {
    const { client, card, rechecks } = await withRecheck();
    const p = card.create();
    rechecks[0]!.resolve({ result: "unavailable", message: "The ELN server did not send the structure in time." });
    await p;
    expect(client.callsOf("register")).toHaveLength(0);
    expect(card.state.recheckNotice).toEqual({
      kind: "unavailable",
      heading: "The structure could not be re-checked",
      message: "The ELN server did not send the structure in time.",
    });
    expect(card.view().canCreate).toBe(true);
    void card.create();
    expect(rechecks).toHaveLength(2);
    expect(card.state.recheckNotice).toBeNull();
  });

  it("a double click during the recheck or the register gives one recheck and one register", async () => {
    const { client, card, rechecks } = await withRecheck();
    void card.create();
    void card.create();
    expect(rechecks).toHaveLength(1);
    rechecks[0]!.resolve({ result: "unchanged" });
    await flush();
    void card.create();
    expect(rechecks).toHaveLength(1);
    expect(client.callsOf("register")).toHaveLength(1);
  });

  it("sends nothing when the input changed during the recheck, even if the page answer was unchanged", async () => {
    const { client, card, rechecks } = await withRecheck();
    const p = card.create();
    card.setInput(input({ changeToken: "t2", structure: { molfile: "OTHER\nM  END\n" } }));
    rechecks[0]!.resolve({ result: "unchanged" });
    await p;
    expect(client.callsOf("register")).toHaveLength(0);
    expect(card.state.recheckNotice?.kind).toBe("changed");
  });

  it("the manual source has no recheck and registers as before", async () => {
    const client = new FakeClient();
    const card = new CardController(client, { onChange: () => {} }, { confirmBeforeCreate: true });
    card.setInput(input());
    const l = card.lookUp();
    client.last("analyze").resolve(ok(analyzeResult()));
    await l;
    await card.create();
    void card.confirmCreate();
    expect(client.callsOf("register")).toHaveLength(1);
    expect(card.view().busy).toBe("register");
  });
});
