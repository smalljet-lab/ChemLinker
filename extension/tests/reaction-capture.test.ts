// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import type { MolfileResult, ReactionResult } from "../src/content/bridge-client";
import {
  MAX_SET_REACTION_BYTES,
  fitSetReaction,
  jsonBytes,
  parseAmount,
  reactionSignature,
  reactionText,
  rowHandles,
  toReactionData,
} from "../src/content/reaction";
import { MAX_STRUCTURE_RETRIES, ReactionCapture, type ReactionBridge } from "../src/content/reaction-capture";
import { HOST_TAG, mountPanel, type PanelHandle } from "../src/content/ui/panel";
import type { SourceState } from "../src/shared/adapter";
import type { PageReaction } from "../src/shared/bridge";
import {
  MAX_REACTION_ROWS,
  MAX_REACTION_TEXT,
  type ReactionData,
  type SetReactionParams,
  type SetReactionResult,
} from "../src/shared/protocol";
import { FakeClient, StaticSource, analyzeResult, batch, experiment, flush, manualTimers, product, registerResult } from "./helpers";

const here = dirname(fileURLToPath(import.meta.url));

interface PageFixture {
  experiments: Record<string, { reaction: PageReaction; molfiles: Record<string, string | null> }>;
}
interface RegistryFixture {
  experiments: Record<string, { experiment_ref: string; experiment_code: string; experiment_url: string; reaction: ReactionData }>;
}
const pages = JSON.parse(readFileSync(join(here, "fixtures", "page-reactions.json"), "utf8")) as PageFixture;
// The host's test data (tests/data at the project root): what set_reaction must receive.
const golden = JSON.parse(readFileSync(join(here, "..", "..", "tests", "data", "reactions_example.json"), "utf8")) as RegistryFixture;

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;
const page = (code: string): PageReaction => clone(pages.experiments[code]!.reaction);
const molfiles = (code: string) => pages.experiments[code]!.molfiles;

describe("toReactionData (real ELN data)", () => {
  const codes = Object.keys(golden.experiments);

  it("covers the six experiments of the fixtures", () => {
    expect(codes.sort()).toEqual(["ABC-001", "ABC-002", "ABC-003", "ABC-004", "ABC-006", "ABC-008"]);
  });

  it.each(codes)("%s gives exactly the reaction the host test data holds", (code) => {
    const data = toReactionData(page(code), molfiles(code));
    expect(data).toEqual(golden.experiments[code]!.reaction);
    // A Map works the same as a record.
    expect(toReactionData(page(code), new Map(Object.entries(molfiles(code))))).toEqual(data);
  });

  it("parses only plain numbers", () => {
    expect(parseAmount("2313")).toBe(2313);
    expect(parseAmount("0,915")).toBe(0.915);
    expect(parseAmount(" 1.000 ")).toBe(1);
    for (const text of ["", "~5", "1e3", "-1", "1.2.3", "5 mg", null]) expect(parseAmount(text)).toBeNull();
  });

  it("clips texts and lists to what the host accepts, and drops unusable molfiles", () => {
    const p = page("ABC-002");
    p.reactants[0]!.name = `a\tb\n${"x".repeat(MAX_REACTION_TEXT + 50)}`;
    const many = Array.from({ length: MAX_REACTION_ROWS + 5 }, (_, i) => ({ ...p.reactants[1]!, row: i + 1, handle: null }));
    p.reagents = many;
    p.solvents = [{ name: "  ", volume: "3", unit: "ml" }, { name: "THF", volume: "x", unit: null }];
    const data = toReactionData(p, { [p.reactants[0]!.handle!]: "bad\u0001molfile", [p.reactants[1]!.handle!]: "" });
    expect(data.reactants[0]!.name!.length).toBe(MAX_REACTION_TEXT);
    expect(data.reactants[0]!.name!.startsWith("a b x")).toBe(true);
    expect(data.reactants[0]!.molfile).toBeNull();
    expect(data.reactants[1]!.molfile).toBeNull();
    expect(data.reagents).toHaveLength(MAX_REACTION_ROWS);
    expect(data.solvents).toEqual([{ name: "THF", volume: null, unit: null }]);
    expect(reactionText("   ")).toBeNull();
  });

  it("drops a reference amount that is not a number", () => {
    const p = page("ABC-002");
    p.referenceAmount = { value: "n/a", unit: "mmol" };
    expect(toReactionData(p, {}).reference_amount).toBeNull();
  });

  it("the signature ignores row handles but not the content", () => {
    const a = page("ABC-002");
    const b = clone(a);
    b.reactants[0]!.handle = "otherId";
    expect(reactionSignature(b)).toBe(reactionSignature(a));
    b.reactants[0]!.actual.m = "1901";
    expect(reactionSignature(b)).not.toBe(reactionSignature(a));
    expect(rowHandles(a)).toEqual(["bVn8Yg21", "bVn8Ak31"]);
  });
});

// ------------------------------------------------------------------ capture

/** A page bridge whose page the test sets; molfile answers can be held. */
class FakeBridge implements ReactionBridge {
  current: PageReaction | null;
  reactionCalls = 0;
  molfileCalls: string[] = [];
  open = 0;
  maxOpen = 0;
  hold = false;
  held: (() => void)[] = [];
  failHandles = new Set<string>();
  /** Called after each molfile answer (to change the page meanwhile). */
  afterMolfile: (() => void) | null = null;
  constructor(
    current: PageReaction | null,
    private readonly mols: Record<string, string | null>,
  ) {
    this.current = current;
  }
  async reaction(): Promise<ReactionResult> {
    this.reactionCalls += 1;
    return this.current ? { ok: true, reaction: clone(this.current) } : { ok: false, code: "no_widget", message: "No experiment." };
  }
  async molfile(domId: string): Promise<MolfileResult> {
    this.molfileCalls.push(domId);
    this.open += 1;
    this.maxOpen = Math.max(this.maxOpen, this.open);
    if (this.hold) await new Promise<void>((resolve) => this.held.push(resolve));
    this.open -= 1;
    this.afterMolfile?.();
    if (this.failHandles.has(domId)) return { ok: false, code: "timeout", message: "No structure in time." };
    const mol = this.mols[domId];
    return mol ? { ok: true, molfile: mol } : { ok: false, code: "no_widget", message: "Gone." };
  }
  release(): void {
    for (const r of this.held.splice(0)) r();
  }
}

const ABC002 = { ref: "4120", code: "ABC-002", url: golden.experiments["ABC-002"]!.experiment_url };

function setReactionResult(over: Partial<SetReactionResult> = {}): SetReactionResult {
  return { experiment_ref: ABC002.ref, changed: true, updated_at: "2026-11-19T10:00:00+00:00", warnings: [], ...over };
}

function setupCapture(start: PageReaction | null = page("ABC-002"), mols: Record<string, string | null> = molfiles("ABC-002")) {
  const bridge = new FakeBridge(start, mols);
  const client = new FakeClient();
  const timers = manualTimers();
  let mutate: () => void = () => {};
  let changes = 0;
  const capture = new ReactionCapture({
    bridge,
    client,
    schedule: timers.schedule,
    observe: (onChange) => {
      mutate = onChange;
      return () => {
        mutate = () => {};
      };
    },
    onChange: () => {
      changes += 1;
    },
    now: () => new Date(2026, 10, 19, 10, 5),
  });
  return {
    bridge,
    client,
    timers,
    capture,
    /** A page mutation, then the debounce runs out. */
    pageChanged: async () => {
      mutate();
      timers.run();
      await flush();
      await flush();
    },
    changes: () => changes,
  };
}
type CaptureCtx = ReturnType<typeof setupCapture>;

async function settle(): Promise<void> {
  for (let i = 0; i < 4; i++) await flush();
}

async function showRegistered(ctx: CaptureCtx): Promise<void> {
  ctx.capture.setContext({ experiment: ABC002, registered: true });
  ctx.timers.run();
  await settle();
}

describe("reaction capture: after Create ID", () => {
  it("sends exactly what the page shows, also while the experiment is edited", async () => {
    const editing = page("ABC-002");
    editing.readOnly = false;
    const ctx = setupCapture(editing);
    ctx.capture.setContext({ experiment: ABC002, registered: false });
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.bridge.molfileCalls).toEqual(["bVn8Yg21", "bVn8Ak31"]);
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
    const params = ctx.client.last("set_reaction").params as SetReactionParams;
    expect(params).toEqual({
      experiment_ref: "4120",
      experiment_code: "ABC-002",
      experiment_url: ABC002.url,
      reaction: golden.experiments["ABC-002"]!.reaction,
    });
    ctx.client.last("set_reaction").resolve({ ok: true, result: setReactionResult() });
    await settle();
    expect(ctx.capture.status()).toBe("Reaction of ABC-002 saved (10:05).");
  });

  it("a structure that cannot be read is sent as null and does not block", async () => {
    const ctx = setupCapture();
    ctx.capture.setContext({ experiment: ABC002, registered: false });
    ctx.bridge.failHandles.add("bVn8Ak31");
    ctx.capture.afterCreate(ABC002);
    await settle();
    const reaction = (ctx.client.last("set_reaction").params as SetReactionParams).reaction;
    expect(reaction.reactants[0]!.molfile).toBe(molfiles("ABC-002")["bVn8Yg21"]);
    expect(reaction.reactants[1]!.molfile).toBeNull();
  });

  it("reads the structures one at a time", async () => {
    const ctx = setupCapture();
    ctx.capture.setContext({ experiment: ABC002, registered: false });
    ctx.bridge.hold = true;
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.bridge.molfileCalls).toEqual(["bVn8Yg21"]);
    ctx.bridge.release();
    await settle();
    expect(ctx.bridge.molfileCalls).toEqual(["bVn8Yg21", "bVn8Ak31"]);
    ctx.bridge.release();
    await settle();
    expect(ctx.bridge.maxOpen).toBe(1);
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
  });

  it("uses the newest page when it changed while the structures were read", async () => {
    const ctx = setupCapture();
    ctx.capture.setContext({ experiment: ABC002, registered: false });
    let once = true;
    ctx.bridge.afterMolfile = () => {
      if (!once) return;
      once = false;
      ctx.bridge.current!.reactants[0]!.actual.m = "1950";
    };
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
    expect((ctx.client.last("set_reaction").params as SetReactionParams).reaction.reactants[0]!.actual!.m).toBe(1950);
  });

  it("says so in the status line when the page cannot be read; never a request", async () => {
    const ctx = setupCapture(null);
    ctx.capture.setContext({ experiment: ABC002, registered: false });
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.calls).toHaveLength(0);
    expect(ctx.capture.status()).toContain("could not be read from the page");
  });

  it("does nothing for an experiment that is no longer shown", async () => {
    const ctx = setupCapture();
    ctx.capture.setContext({ experiment: { ...ABC002, ref: "999" }, registered: false });
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.calls).toHaveLength(0);
  });
});

describe("reaction capture: mirror of the saved page", () => {
  it("sends while the experiment is shown saved and has a registered product", async () => {
    const ctx = setupCapture();
    await showRegistered(ctx);
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
  });

  it("never while it is being edited", async () => {
    const editing = page("ABC-002");
    editing.readOnly = false;
    const ctx = setupCapture(editing);
    await showRegistered(ctx);
    expect(ctx.bridge.reactionCalls).toBe(1);
    expect(ctx.bridge.molfileCalls).toEqual([]);
    expect(ctx.client.calls).toHaveLength(0);
    // Saved: the page switches to view mode.
    ctx.bridge.current = page("ABC-002");
    await ctx.pageChanged();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
  });

  it("not when it switches to edit mode while the structures are read", async () => {
    const ctx = setupCapture();
    ctx.bridge.afterMolfile = () => {
      ctx.bridge.current!.readOnly = false;
    };
    await showRegistered(ctx);
    expect(ctx.client.calls).toHaveLength(0);
  });

  it("never without a product registered in this experiment (not even reads the page)", async () => {
    const ctx = setupCapture();
    ctx.capture.setContext({ experiment: ABC002, registered: false });
    ctx.timers.run();
    await ctx.pageChanged();
    expect(ctx.bridge.reactionCalls).toBe(0);
    expect(ctx.client.calls).toHaveLength(0);
  });

  it("sends a content once; again only when the page shows something else", async () => {
    const ctx = setupCapture();
    await showRegistered(ctx);
    ctx.client.last("set_reaction").resolve({ ok: true, result: setReactionResult() });
    await settle();
    await ctx.pageChanged();
    // Re-rendered rows (new handles), same content: nothing to send.
    ctx.bridge.current!.reactants[0]!.handle = "newHandle";
    await ctx.pageChanged();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
    // After Create ID with the same content: nothing either.
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
    // The user changed an amount and saved.
    ctx.bridge.current = page("ABC-002");
    ctx.bridge.current.reactants[1]!.actual.m = "890";
    await ctx.pageChanged();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(2);
  });

  it("after a failure: silent, a line only, and no retry until the content changes", async () => {
    const ctx = setupCapture();
    await showRegistered(ctx);
    ctx.client.last("set_reaction").resolve({
      ok: false,
      error: { code: "not_found", message: "No compound of the registry was made in experiment 4120." },
    });
    await settle();
    expect(ctx.capture.status()).toBe(
      "The reaction of ABC-002 could not be saved: No compound of the registry was made in experiment 4120.",
    );
    for (let i = 0; i < 5; i++) await ctx.pageChanged();
    ctx.capture.setContext({ experiment: ABC002, registered: false });
    await showRegistered(ctx);
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
    // A Create ID tries once more.
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(2);
    ctx.client.last("set_reaction").resolve({ ok: false, error: { code: "host_timeout", message: "No answer." } });
    await settle();
    await ctx.pageChanged();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(2);
    // Another content is tried.
    ctx.bridge.current!.temperature = "40";
    await ctx.pageChanged();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(3);
  });

  it("one capture at a time; what was asked meanwhile runs afterwards", async () => {
    const ctx = setupCapture();
    await showRegistered(ctx);
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
    expect(ctx.capture.isRunning()).toBe(true);
    ctx.bridge.current!.temperature = "rt";
    await ctx.pageChanged();
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
    ctx.client.last("set_reaction").resolve({ ok: true, result: setReactionResult() });
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(2);
    expect((ctx.client.last("set_reaction").params as SetReactionParams).reaction.temperature).toBe("rt");
    ctx.client.last("set_reaction").resolve({ ok: true, result: setReactionResult({ changed: false }) });
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(2);
    expect(ctx.capture.status()).toBe("Reaction of ABC-002 is up to date (10:05).");
  });

  it("stops after dispose", async () => {
    const ctx = setupCapture();
    ctx.capture.dispose();
    await showRegistered(ctx);
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.calls).toHaveLength(0);
  });
});

// ------------------------------------------------------------------ in the panel

let handles: PanelHandle[] = [];
afterEach(() => {
  for (const h of handles) h.destroy();
  handles = [];
  document.documentElement.querySelectorAll(HOST_TAG).forEach((n) => n.remove());
});

describe("panel: reaction capture", () => {
  function mount() {
    const client = new FakeClient();
    const timers = manualTimers();
    const state: SourceState = {
      kind: "ready",
      experiment: experiment({ ref: ABC002.ref, code: "ABC-002", url: ABC002.url }),
      products: [product({ productIndex: 1, label: "Product 1", changeToken: "k1" })],
    };
    const source = new StaticSource(state, { autoLookup: true, confirmBeforeCreate: false });
    const bridge = new FakeBridge(page("ABC-002"), molfiles("ABC-002"));
    const handle = mountPanel({
      doc: document,
      source,
      client,
      schedule: timers.schedule,
      reactions: { bridge, observe: () => () => {} },
    });
    handles.push(handle);
    return { client, timers, bridge, handle, root: handle.root };
  }

  async function lookUp(ctx: ReturnType<typeof mount>, result = analyzeResult()) {
    ctx.timers.run();
    ctx.client.last("analyze").resolve({ ok: true, result });
    await settle();
    ctx.timers.run();
    await settle();
  }

  it("stores the reaction right after Create ID, and reports it under Advanced only", async () => {
    const ctx = mount();
    await lookUp(ctx);
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(0);
    const create = Array.from(ctx.root.querySelectorAll("button")).find((b) => b.textContent === "Create ID (new compound)")!;
    create.click();
    await settle();
    ctx.client.last("register").resolve({
      ok: true,
      result: registerResult({ id: "000042_01_002", batch: batch({ full_id: "000042_01_002", experiment_ref: ABC002.ref }) }),
    });
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
    expect(ctx.client.last("set_reaction").params).toMatchObject({ experiment_ref: ABC002.ref, experiment_code: "ABC-002" });
    ctx.client.last("set_reaction").resolve({ ok: false, error: { code: "storage_error", message: "Disk full." } });
    await settle();
    const line = ctx.root.querySelector<HTMLElement>(".reaction-line")!;
    expect(line.textContent).toBe("The reaction of ABC-002 could not be saved: Disk full.");
    expect(line.closest<HTMLElement>(".advanced")!.hidden).toBe(true); // no popup: inside the closed Advanced section
    expect(ctx.root.querySelector(".banner.error")).toBeNull();
  });

  it("mirrors the saved page once a lookup shows a batch made in this experiment", async () => {
    const ctx = mount();
    await lookUp(ctx, analyzeResult({ batches: [batch({ experiment_ref: "111" })] }));
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(0);
    // Same compound, made in this experiment: registered here.
    ctx.handle.destroy();
    handles = [];
    const again = mount();
    await lookUp(
      again,
      analyzeResult({ status: "registered_here", id: "000042_01_001", core_stereo_id: "000042_01", batches: [batch({ experiment_ref: ABC002.ref })] }),
    );
    expect(again.client.callsOf("set_reaction")).toHaveLength(1);
  });
});

// ------------------------------------------------------------------ structures not read, size

describe("reaction capture: structures that could not be read", () => {
  const OK = (code = "ABC-002") => molfiles(code);

  it("a content stored while the server was slow is not counted as stored: the missing structure is read again", async () => {
    const ctx = setupCapture();
    ctx.bridge.failHandles.add("bVn8Ak31");
    await showRegistered(ctx);
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
    expect((ctx.client.last("set_reaction").params as SetReactionParams).reaction.reactants[1]!.molfile).toBeNull();
    ctx.client.last("set_reaction").resolve({ ok: true, result: setReactionResult() });
    await settle();
    expect(ctx.capture.status()).toBe(
      "Reaction of ABC-002 saved (10:05). 1 structure could not be read from the page; trying again shortly.",
    );
    // The server answers again; the retry timer runs out.
    ctx.bridge.failHandles.clear();
    ctx.timers.run();
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(2);
    // Only the missing structure was read again.
    expect(ctx.bridge.molfileCalls).toEqual(["bVn8Yg21", "bVn8Ak31", "bVn8Ak31"]);
    const params = ctx.client.last("set_reaction").params as SetReactionParams;
    expect(params.reaction).toEqual(golden.experiments["ABC-002"]!.reaction);
    ctx.client.last("set_reaction").resolve({ ok: true, result: setReactionResult() });
    await settle();
    expect(ctx.capture.status()).toBe("Reaction of ABC-002 saved (10:05).");
    // Now it is stored: neither a page change nor a Create ID sends it again.
    await ctx.pageChanged();
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(2);
    expect(OK()["bVn8Ak31"]).toBe(params.reaction.reactants[1]!.molfile);
  });

  it("the mirror tries a bounded number of times; a Create ID and showing the experiment again try once more", async () => {
    const ctx = setupCapture();
    ctx.bridge.failHandles.add("bVn8Ak31");
    await showRegistered(ctx);
    for (let i = 1; i <= MAX_STRUCTURE_RETRIES; i++) {
      expect(ctx.client.callsOf("set_reaction")).toHaveLength(i);
      ctx.client.last("set_reaction").resolve({ ok: true, result: setReactionResult({ changed: i === 1 }) });
      await settle();
      await ctx.pageChanged();
    }
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(MAX_STRUCTURE_RETRIES);
    expect(ctx.capture.status()).toContain("tried again when the experiment is shown again or on Create ID");
    for (let i = 0; i < 3; i++) await ctx.pageChanged();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(MAX_STRUCTURE_RETRIES);
    // Create ID (same content) tries again.
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(MAX_STRUCTURE_RETRIES + 1);
    ctx.client.last("set_reaction").resolve({ ok: true, result: setReactionResult({ changed: false }) });
    await settle();
    // Another experiment, then this one again: tried again.
    ctx.capture.setContext({ experiment: { ...ABC002, ref: "999", code: "ABC-999" }, registered: false });
    await showRegistered(ctx);
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(MAX_STRUCTURE_RETRIES + 2);
    // The structure read the first time was kept, never read again.
    expect(ctx.bridge.molfileCalls.filter((h) => h === "bVn8Yg21")).toHaveLength(1);
  });

  it("a structure that cannot be stored (too large) does not make the content incomplete", async () => {
    const ctx = setupCapture();
    const read = ctx.bridge.molfile.bind(ctx.bridge);
    ctx.bridge.molfile = async (domId: string) =>
      domId === "bVn8Ak31" ? { ok: false, code: "too_large", message: "The structure is too large." } : read(domId);
    await showRegistered(ctx);
    ctx.client.last("set_reaction").resolve({ ok: true, result: setReactionResult() });
    await settle();
    expect(ctx.capture.status()).toBe("Reaction of ABC-002 saved (10:05).");
    await ctx.pageChanged();
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
  });
});

describe("reaction capture: request size", () => {
  // Legal molfile texts (newlines only); each newline takes two bytes in JSON.
  const big = (c: string, lines: number) => `${c}\n`.repeat(lines);

  it("fitSetReaction leaves a small request as it is", () => {
    const params = { experiment_ref: "1", reaction: toReactionData(page("ABC-002"), molfiles("ABC-002")) };
    expect(fitSetReaction(params)).toEqual({ params, dropped: 0 });
    expect(fitSetReaction(params)!.params).toBe(params);
  });

  it("fitSetReaction drops the largest structures first, only as many as needed, without changing its input", () => {
    const reaction = toReactionData(page("ABC-002"), { bVn8Yg21: big("A", 240_000), bVn8Ak31: big("B", 200_000) });
    const params = { experiment_ref: "1", reaction };
    expect(jsonBytes(params)).toBeGreaterThan(1024 * 1024);
    const fitted = fitSetReaction(params)!;
    expect(fitted.dropped).toBe(1);
    expect(fitted.params.reaction.reactants[0]!.molfile).toBeNull();
    expect(fitted.params.reaction.reactants[1]!.molfile).toBe(big("B", 200_000));
    expect(jsonBytes(fitted.params)).toBeLessThanOrEqual(MAX_SET_REACTION_BYTES);
    expect(reaction.reactants[0]!.molfile).toBe(big("A", 240_000));
    // Too large even without any structure: null.
    expect(fitSetReaction(params, 200)).toBeNull();
  });

  it("never sends a request over the host's frame limit; says which structures were left out", async () => {
    const mols = { bVn8Yg21: big("A", 240_000), bVn8Ak31: big("B", 200_000) };
    const ctx = setupCapture(page("ABC-002"), mols);
    ctx.capture.setContext({ experiment: ABC002, registered: false });
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
    const params = ctx.client.last("set_reaction").params as SetReactionParams;
    expect(jsonBytes({ id: "x".repeat(64), v: 99, op: "set_reaction", params })).toBeLessThanOrEqual(1024 * 1024);
    expect(params.reaction.reactants[0]!.molfile).toBeNull();
    expect(params.reaction.reactants[1]!.molfile).toBe(mols.bVn8Ak31);
    ctx.client.last("set_reaction").resolve({ ok: true, result: setReactionResult() });
    await settle();
    expect(ctx.capture.status()).toBe(
      "Reaction of ABC-002 saved (10:05). 1 structure was left out: the reaction was too large to send.",
    );
    // The same content is not sent again.
    ctx.capture.afterCreate(ABC002);
    await settle();
    expect(ctx.client.callsOf("set_reaction")).toHaveLength(1);
  });
});
