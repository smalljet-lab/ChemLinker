import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ElnExperiment } from "../src/compounds/eln-csv";
import {
  buildPlan,
  countRuns,
  distinctCompounds,
  distinctLine,
  ImportController,
  naturalCompare,
  OPEN_FAILURES_MESSAGE,
  planDecision,
  plannedWork,
  progressLine,
  REPORT_HEADER,
  SKIP_ALL_REGISTERED,
  SKIP_NO_YIELD,
  STOPPED_MESSAGE,
  summaryLines,
  TAB_CLOSED_MESSAGE,
  timeLeftText,
  YIELD_FALLBACK_NOTE,
  type ExperimentRun,
  type ImportOptions,
} from "../src/compounds/import";
import { experimentPageUrl, type ImportApplyReply, type ImportApplyRequest } from "../src/shared/import";
import type { YieldInfo, YieldRow } from "../src/shared/protocol";
import {
  defaultPage,
  FakeTabs,
  fakeTimers,
  FIXTURE,
  outcome,
  pageProduct,
  ready,
  refOfUrl,
  RegistryClient,
  yieldRow,
  type FakePage,
} from "./compounds-import-fakes";

const info = (isolated: number | null): YieldInfo => ({ isolated, analytical: null, updated_at: "2026-11-18T09:00:00+00:00" });

/** The experiments opened by default (rule "with_yield", empty registry): ABC-005 (yield 0) and ABC-007 (none) are not. */
const OPENED = ["4110", "4120", "4130", "4131", "4160", "4180", "4190"];

beforeEach(() => {
  vi.useFakeTimers({ now: new Date("2026-11-19T10:00:00Z") });
});
afterEach(() => {
  vi.useRealTimers();
});

async function setup(rows: YieldRow[] = [], tabs = new FakeTabs(), pageSize?: number) {
  const client = new RegistryClient(rows);
  const announced: string[] = [];
  const changes = vi.fn();
  const ctrl = new ImportController(
    { client, tabs, timers: fakeTimers, ...(pageSize ? { pageSize } : {}) },
    { onChange: changes, announce: (m) => announced.push(m) },
  );
  await ctrl.loadFile("experiments.csv", FIXTURE);
  return { client, tabs, ctrl, announced, changes };
}

/** Lets the fake clock run until the walk has ended. */
async function settle(promise: Promise<unknown>, limitMs = 3_600_000): Promise<void> {
  let done = false;
  void promise.then(() => {
    done = true;
  });
  for (let t = 0; !done && t < limitMs; t += 500) await vi.advanceTimersByTimeAsync(500);
  if (!done) throw new Error("the walk did not end");
}

async function walk(ctrl: ImportController, options: Partial<ImportOptions> = {}): Promise<void> {
  await settle(ctrl.run({ mode: "check", ...options }));
}

function runOf(ctrl: ImportController, code: string): ExperimentRun {
  const run = ctrl.state.runs.find((r) => r.plan.experiment.code === code);
  if (!run) throw new Error(`no run ${code}`);
  return run;
}

function applyFor(tabs: FakeTabs, ref: string): ImportApplyRequest | undefined {
  return tabs.applies().find((a) => a.ref === ref);
}

/** A page whose status answers follow `answers` (the last one repeats). */
function scripted(answers: ((ref: string) => unknown)[], ref: string, apply?: FakePage["apply"]): FakePage {
  return { status: (n) => answers[Math.min(n, answers.length - 1)]!(ref), ...(apply ? { apply } : {}) };
}

describe("import plan", () => {
  it("loads every batch of the registry page by page, ignores retired ones, and opens nothing", async () => {
    const rows = [
      yieldRow("4120", 1, "000002_01_001", { yield: info(16) }),
      yieldRow("4110", 1, "000001_01_001", { experiment_code: "ABC-01X", yield: info(52.4) }),
      yieldRow("4120", 3, "000009_01_001"),
      yieldRow("4130", 1, "000003_01_001", { retired: true }),
      yieldRow("4131", 1, "000004_01_001"),
    ];
    const { client, tabs, ctrl } = await setup(rows, new FakeTabs(), 2);
    expect(client.calls.map((c) => [c.op, c.params])).toEqual([
      ["yields", { offset: 0, limit: 2 }],
      ["yields", { offset: 2, limit: 2 }],
      ["yields", { offset: 4, limit: 2 }],
    ]);
    expect(tabs.events).toEqual([]);
    expect(tabs.sent).toEqual([]);

    const plan = ctrl.state.plan!;
    expect(plan.experiments.map((e) => e.experiment.code)).toEqual([
      "ABC-001", "ABC-002", "ABC-003", "ABC-004", "ABC-005", "ABC-006", "ABC-007", "ABC-008", "ABC-009",
    ]);
    const [abc1, abc2, abc3, abc4] = plan.experiments;
    expect(abc1!.products[0]).toMatchObject({
      registered: { id: "000001_01_001", code: "ABC-01X", isolated: 52.4 },
      codeMatches: false,
      yieldComparison: "equal",
    });
    expect(abc2!.products.map((p) => [p.registered?.id ?? null, p.codeMatches, p.yieldComparison])).toEqual([
      ["000002_01_001", true, "different"],
      [null, null, null],
    ]);
    expect(abc2!.extraBatches).toEqual([{ index: 3, id: "000009_01_001", code: "ABC-002", isolated: null, analytical: null }]);
    expect(abc3!.products[0]!.registered).toBeNull();
    expect(abc4!.products[0]!.yieldComparison).toBe("only_first");
    // The preview names what will happen and notes what differs.
    const preview = ctrl.state.runs;
    expect(preview[0]!.rows[0]!.notes.join(" ")).toContain("with the ELN number ABC-01X; the list says ABC-001");
    expect(preview[1]!.notes.join(" ")).toContain("000009_01_001 as product 3");
    expect(ctrl.state.status).toBe("idle");
  });

  it("orders experiments by ELN number in natural order, those without one last by pk", () => {
    const e = (ref: string, code: string | null): ElnExperiment => ({ ref, code, status: null, title: null, type: null, line: 2, products: [] });
    const plan = buildPlan([e("100", "ABC-10"), e("101", "ABC-2"), e("30", null), e("5", null), e("102", "abc-3"), e("1", "ABC-002")], []);
    expect(plan.experiments.map((x) => x.experiment.code ?? x.experiment.ref)).toEqual(["ABC-002", "ABC-2", "abc-3", "ABC-10", "5", "30"]);
    expect(naturalCompare("ABC-2", "ABC-10")).toBeLessThan(0);
    expect(naturalCompare("A-9", "A-9a")).toBeLessThan(0);
  });

  it("decides which experiments are opened, from the list and the plan", async () => {
    const { ctrl } = await setup([yieldRow("4110", 1, "000001_01_001")]);
    const plan = ctrl.state.plan!;
    const decide = (options: Pick<ImportOptions, "rule" | "reopenRegistered">) =>
      plan.experiments.map((x) => {
        const d = planDecision(x, options);
        return d.open ? "open" : d.reason;
      });
    expect(decide({ rule: "with_yield", reopenRegistered: false })).toEqual([
      SKIP_ALL_REGISTERED, "open", "open", "open", SKIP_NO_YIELD, "open", SKIP_NO_YIELD, "open", "open",
    ]);
    expect(decide({ rule: "with_yield", reopenRegistered: true })[0]).toBe("open");
    expect(decide({ rule: "all", reopenRegistered: false })).toEqual([
      SKIP_ALL_REGISTERED, "open", "open", "open", "open", "open", "open", "open", "open",
    ]);
    expect(plannedWork(plan, { rule: "with_yield", reopenRegistered: false })).toEqual({ experiments: 6, products: 8 });
    expect(plannedWork(plan, { rule: "all", reopenRegistered: true })).toEqual({ experiments: 9, products: 10 });
    expect(ctrl.planLine()).toBe("6 experiments of 9 will be opened, 3 skipped. 1 listed product has an ID already.");
  });

  it("shows a host error and opens nothing", async () => {
    const tabs = new FakeTabs();
    const client = new RegistryClient();
    client.request = (async () => ({ ok: false, error: { code: "host_timeout", message: "No answer." } })) as RegistryClient["request"];
    const ctrl = new ImportController({ client, tabs, timers: fakeTimers });
    await ctrl.loadFile("x.csv", FIXTURE);
    expect(ctrl.state.planError).toEqual({ code: "host_timeout", message: "No answer." });
    await ctrl.run({ mode: "check" });
    expect(ctrl.state.status).toBe("idle");
    expect(tabs.events).toEqual([]);
  });
});

describe("import walk: tabs", () => {
  it("opens the first experiment in a new background tab, the others in the same tab, and closes it at the end", async () => {
    const { tabs, ctrl, client } = await setup();
    await walk(ctrl);
    expect(tabs.events.map((e) => e.op)).toEqual(["create", "update", "update", "update", "update", "update", "update", "remove"]);
    expect(tabs.events[0]).toEqual({ op: "create", url: experimentPageUrl("4110"), active: false, id: 41 });
    expect(tabs.events.filter((e) => e.op !== "remove").map((e) => refOfUrl((e as { url: string }).url))).toEqual(OPENED);
    expect(tabs.events.every((e) => e.id === 41)).toBe(true);
    expect(tabs.sent.every((s) => s.tabId === 41)).toBe(true);
    expect(tabs.listenerCount()).toBe(0);
    // The plan is loaded again before the walk.
    expect(client.calls.filter((c) => c.op === "yields")).toHaveLength(2);
    expect(tabs.applies().map((a) => [a.ref, a.products, a.dry_run])).toEqual([
      ["4110", [1], true],
      ["4120", [1, 2], true],
      ["4130", [1], true],
      ["4131", [1], true],
      ["4160", [1, 2], true],
      ["4180", [1], true],
      ["4190", [1], true],
    ]);
    expect(ctrl.state.status).toBe("finished");
    expect(ctrl.state.message).toBeNull();
    expect(ctrl.counters()).toMatchObject({ wouldCreate: 9, skipped: 2, notOpened: 0, created: 0, findings: 0 });
  });

  it("passes the options on: create, warnings allowed, names", async () => {
    const { tabs, ctrl } = await setup();
    await walk(ctrl, { mode: "create", allowWarnings: true, names: true });
    expect(tabs.applies()[0]).toEqual({ type: "import-apply", ref: "4110", products: [1], dry_run: false, allow_warnings: true, names: true });
    expect(ctrl.counters()).toMatchObject({ created: 9, wouldCreate: 0 });
    expect(runOf(ctrl, "ABC-001").rows[0]!.result).toBe("ID created");
  });

  it("removes the tab only when it still exists, and never touches another tab", async () => {
    const tabs = new FakeTabs().page("4110", { status: () => ({ state: "reading" }) });
    const { ctrl } = await setup([], tabs);
    const p = ctrl.run({ mode: "check" });
    await vi.advanceTimersByTimeAsync(5_000);
    tabs.close(99); // another tab: nothing happens
    await vi.advanceTimersByTimeAsync(2_000);
    expect(ctrl.state.status).toBe("running");
    tabs.close(41);
    await settle(p);
    expect(ctrl.state.message).toBe(TAB_CLOSED_MESSAGE);
    expect(tabs.events.map((e) => e.op)).toEqual(["create"]);
    expect(runOf(ctrl, "ABC-001").phase).toBe("waiting");
    expect(runOf(ctrl, "ABC-001").rows[0]!.kind).toBe("not_reached");
    expect(tabs.listenerCount()).toBe(0);
  });

  it("stops when the user closes the tab during the work on an experiment", async () => {
    let release: (value: unknown) => void = () => {};
    const tabs = new FakeTabs().page("4120", {
      apply: () => new Promise((resolve) => (release = resolve)),
    });
    const { ctrl } = await setup([], tabs);
    const p = ctrl.run({ mode: "create" });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(applyFor(tabs, "4120")).toBeDefined();
    tabs.close(41);
    await settle(p);
    release(undefined);
    expect(ctrl.state.message).toBe(TAB_CLOSED_MESSAGE);
    const abc2 = runOf(ctrl, "ABC-002");
    expect(abc2.rows.map((r) => r.kind)).toEqual(["failed", "failed"]);
    expect(abc2.rows[0]!.result).toMatch(/tab was closed before this experiment was finished/);
    expect(tabs.events.some((e) => e.op === "remove")).toBe(false);
    expect(tabs.applies()).toHaveLength(2);
  });
});

describe("import walk: waiting for the page", () => {
  it("treats no answer, a rejection, 'other', 'reading' and another experiment as loading; trusts two equal 'ready' answers", async () => {
    const ref = "4110";
    const a = ready(ref, [pageProduct(1, 52.4)]);
    const b = ready(ref, [pageProduct(1, 52.38)]);
    let asked = -1;
    const tabs = new FakeTabs().page(
      ref,
      scripted(
        [
          () => undefined,
          () => Promise.reject(new Error("Receiving end does not exist.")),
          () => ({ state: "other", ref: "1" }),
          () => ({ state: "reading" }),
          () => ready("999999", [pageProduct(1, 52.4)]),
          () => ready("999999", [pageProduct(1, 52.4)]),
          () => a,
          () => b,
          () => b,
        ],
        ref,
        (request) => {
          asked = tabs.statusRequests(ref);
          return defaultPage(ref).apply!(request);
        },
      ),
    );
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    expect(asked).toBe(9);
    expect(runOf(ctrl, "ABC-001").phase).toBe("done");
  });

  it("asks about once a second", async () => {
    const tabs = new FakeTabs().page("4110", { status: () => ({ state: "reading" }) });
    const { ctrl } = await setup([], tabs);
    void ctrl.run({ mode: "check" });
    await vi.advanceTimersByTimeAsync(10_200);
    expect(tabs.statusRequests("4110")).toBe(10);
    expect(runOf(ctrl, "ABC-001").phase).toBe("opening");
    expect(progressLine(ctrl.state)).toBe("Experiment 1 of 7: ABC-001 …");
    ctrl.stop();
  });

  it("waits up to 10 s more when the page shows fewer products than the list", async () => {
    const ref = "4120";
    let asked = -1;
    const tabs = new FakeTabs().page(ref, {
      status: () => ready(ref, [pageProduct(1, 15)]),
      apply: (request) => {
        asked = tabs.statusRequests(ref);
        return defaultPage(ref).apply!(request);
      },
    });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    // Stable from the 2nd answer on; accepted 10 s later.
    expect(asked).toBe(12);
    expect(applyFor(tabs, ref)!.products).toEqual([1]);
    expect(runOf(ctrl, "ABC-002").findings).toEqual(["The page shows 1 product; the list 2 products."]);
  });

  it("does not wait longer when all listed products are shown", async () => {
    const ref = "4120";
    let asked = -1;
    const tabs = new FakeTabs().page(ref, {
      apply: (request) => {
        asked = tabs.statusRequests(ref);
        return defaultPage(ref).apply!(request);
      },
    });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    expect(asked).toBe(2);
  });

  it("counts an experiment as not opened after the time limit, and goes on", async () => {
    const tabs = new FakeTabs().page("4110", { status: () => ({ state: "reading" }) });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    const abc1 = runOf(ctrl, "ABC-001");
    expect(abc1.phase).toBe("not opened");
    expect(abc1.reason).toBe("the experiment did not finish loading within 2 minutes");
    expect(abc1.rows[0]!.result).toBe("not opened: the experiment did not finish loading within 2 minutes");
    expect(tabs.statusRequests("4110")).toBe(120);
    expect(applyFor(tabs, "4110")).toBeUndefined();
    expect(runOf(ctrl, "ABC-002").phase).toBe("done");
    expect(ctrl.counters().notOpened).toBe(1);
    expect(ctrl.state.message).toBeNull();
  });

  it("counts an 'error' answer as not opened at once", async () => {
    const tabs = new FakeTabs().page("4110", { status: () => ({ state: "error", message: "The layout changed." }) });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    expect(runOf(ctrl, "ABC-001").reason).toBe("the experiment page could not be read (The layout changed.)");
    expect(tabs.statusRequests("4110")).toBe(1);
  });

  it("stops by itself after three experiments in a row could not be opened", async () => {
    const tabs = new FakeTabs();
    for (const ref of OPENED) tabs.page(ref, { status: () => undefined });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    expect(ctrl.state.message).toBe(OPEN_FAILURES_MESSAGE);
    expect(tabs.events.map((e) => e.op)).toEqual(["create", "update", "update", "remove"]);
    expect(ctrl.state.runs.map((r) => r.phase)).toEqual([
      "not opened", "not opened", "not opened", "waiting", "skipped", "waiting", "skipped", "waiting", "waiting",
    ]);
    expect(runOf(ctrl, "ABC-004").rows[0]!.kind).toBe("not_reached");
    expect(summaryLines(ctrl.state)[0]).toBe("The check ended after 3 of 7 experiments. Nothing was created.");
  });

  it("counts failures in a row only: an opened experiment in between resets the count", async () => {
    const tabs = new FakeTabs();
    for (const ref of ["4110", "4120", "4131", "4160"]) tabs.page(ref, { status: () => ({ state: "error", message: "x" }) });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    expect(ctrl.state.message).toBeNull();
    expect(ctrl.counters().notOpened).toBe(4);
  });
});

describe("import walk: which products are sent", () => {
  it("rule 'with_yield': products with a yield above 0 on the page, and every product with an ID", async () => {
    const tabs = new FakeTabs()
      .page("4120", { status: () => ready("4120", [pageProduct(1, 0), pageProduct(2, 61.6)]) })
      .page("4160", { status: () => ready("4160", [pageProduct(1, 0), pageProduct(2, 76.2)]) });
    const { ctrl } = await setup([yieldRow("4160", 1, "000006_01_001", { yield: info(76.18) })], tabs);
    await walk(ctrl);
    expect(applyFor(tabs, "4120")!.products).toEqual([2]);
    expect(applyFor(tabs, "4160")!.products).toEqual([1, 2]);
    const abc2 = runOf(ctrl, "ABC-002");
    expect(abc2.rows[0]!.kind).toBe("not_handled");
    expect(abc2.rows[0]!.result).toBe("not handled: no isolated yield above 0 on the page");
  });

  it("rule 'all': every product row of the page, also without a yield or a structure", async () => {
    const tabs = new FakeTabs().page("4150", {
      status: () => ready("4150", [pageProduct(1, null, { structure: false, problem: "No structure is drawn." })]),
    });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl, { rule: "all" });
    expect(tabs.applies().map((a) => a.ref)).toEqual(["4110", "4120", "4130", "4131", "4150", "4160", "4170", "4180", "4190"]);
    expect(applyFor(tabs, "4150")!.products).toEqual([1]);
  });

  it("uses the list's yield when the page's yields cannot be read, and says so", async () => {
    const tabs = new FakeTabs().page("4130", { status: () => ready("4130", [pageProduct(1, null, { yields: null })]) });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    expect(applyFor(tabs, "4130")!.products).toEqual([1]);
    expect(runOf(ctrl, "ABC-003").rows[0]!.notes).toContain(YIELD_FALLBACK_NOTE);
    expect(YIELD_FALLBACK_NOTE).toBe("The yield could not be read from the page; the list's value was used.");
  });

  it("sends nothing when nothing is chosen: the experiment has nothing to do", async () => {
    const tabs = new FakeTabs().page("4110", { status: () => ready("4110", [pageProduct(1, 0)]) });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    expect(applyFor(tabs, "4110")).toBeUndefined();
    const abc1 = runOf(ctrl, "ABC-001");
    expect(abc1.phase).toBe("done");
    expect(abc1.reason).toBe("nothing to do");
    expect(abc1.rows[0]!.result).toBe("nothing to do: no isolated yield above 0 on the page");
    expect(abc1.rows[0]!.findings).toEqual(["The page shows an isolated yield of 0 %; the list says 52.38 %."]);
  });

  it("does not open an experiment whose products all have an ID, unless asked to", async () => {
    const rows = [yieldRow("4110", 1, "000001_01_001")];
    const first = await setup(rows);
    await walk(first.ctrl);
    expect(applyFor(first.tabs, "4110")).toBeUndefined();
    expect(runOf(first.ctrl, "ABC-001").rows[0]!.result).toBe(`skipped: ${SKIP_ALL_REGISTERED}`);

    const second = await setup(rows);
    await walk(second.ctrl, { reopenRegistered: true });
    expect(applyFor(second.tabs, "4110")!.products).toEqual([1]);
  });
});

describe("import walk: results and findings", () => {
  it("cross-checks each result against the list and the plan", async () => {
    const apply =
      (ref: string, outcomes: Parameters<typeof outcome>[]) =>
      (request: ImportApplyRequest): ImportApplyReply => ({
        ok: true,
        experiment: { ref, code: null },
        products: request.products.map((i) => outcome(...(outcomes.find((o) => o[0] === i) ?? [i]))),
        reaction: "none",
        reaction_message: null,
      });
    const tabs = new FakeTabs()
      .page("4110", { apply: apply("4110", [[1, { formula: "C13H10O", yields: { isolated: 52.4, analytical: null } }]]) })
      .page("4130", { apply: apply("4130", [[1, { kind: "exists", id: "000004_01_001", formula: "C22H23N" }]]) })
      .page("4131", {
        status: () => ready("4131", [pageProduct(1, 51)]),
        apply: apply("4131", [[1, { formula: "C21H19N-" }]]),
      })
      .page("4180", {
        status: () => ready("4180", [pageProduct(1, 70)], "ABC-080"),
        apply: apply("4180", [[1, { formula: "C14H17N" }]]),
      });
    const { ctrl } = await setup([yieldRow("4130", 1, "000003_01_001")], tabs);
    await walk(ctrl, { reopenRegistered: true });
    expect(runOf(ctrl, "ABC-001").rows[0]!.findings).toEqual([]);
    expect(runOf(ctrl, "ABC-003").rows[0]!.findings).toEqual([
      "The registry had 000003_01_001 for this product, but now 000004_01_001 was reported.",
    ]);
    // A formula with a charge cannot be compared: no finding for it.
    expect(runOf(ctrl, "ABC-004").rows[0]!.findings).toEqual(["The page shows an isolated yield of 51 %; the list says 64.55 %."]);
    const abc8 = runOf(ctrl, "ABC-008");
    expect(abc8.findings).toEqual(["The ELN number on the page is ABC-080; the list says ABC-008."]);
    expect(abc8.rows[0]!.findings).toEqual(["The structure on the page has the formula C14H17N; the list says C14H15N."]);
    expect(ctrl.counters().findings).toBe(4);
  });

  it("reports a refused request and a missing answer as failed", async () => {
    const tabs = new FakeTabs()
      .page("4110", { apply: () => ({ ok: false, message: "another import is running in this frame" }) })
      .page("4120", { apply: () => new Promise(() => {}) });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    expect(runOf(ctrl, "ABC-001").rows[0]!.result).toBe("failed: another import is running in this frame");
    expect(runOf(ctrl, "ABC-002").rows.map((r) => r.kind)).toEqual(["failed", "failed"]);
    expect(runOf(ctrl, "ABC-002").rows[0]!.result).toMatch(/did not answer in time/);
    expect(ctrl.counters().failed).toBe(3);
    expect(runOf(ctrl, "ABC-003").phase).toBe("done");
  });

  it("counts the results, and the distinct compounds a check would create", async () => {
    const reply =
      (ref: string, outcomes: Parameters<typeof outcome>[]) =>
      (): ImportApplyReply => ({ ok: true, experiment: { ref, code: null }, products: outcomes.map((o) => outcome(...o)), reaction: "none", reaction_message: null });
    const tabs = new FakeTabs()
      .page("4110", { apply: reply("4110", [[1, { status: "new", compound_key: "A" }]]) })
      .page("4120", { apply: reply("4120", [[1, { status: "known_core", compound_key: "B" }], [2, { status: "new", compound_key: "A" }]]) })
      .page("4130", { apply: reply("4130", [[1, { status: "known_compound", compound_key: "C" }]]) })
      .page("4131", { apply: reply("4131", [[1, { kind: "attention", status: "new", message: "The structure has warnings." }]]) })
      .page("4160", { apply: reply("4160", [[1, { compound_key: "D" }], [2, { compound_key: "D" }]]) })
      .page("4180", { apply: reply("4180", [[1, { kind: "failed", status: null, compound_key: null, message: "x" }]]) })
      .page("4190", { apply: reply("4190", [[1, { kind: "exists", id: "000009_01_001", status: "registered_here" }]]) });
    const { ctrl } = await setup([], tabs);
    await walk(ctrl);
    expect(ctrl.counters()).toEqual({ created: 0, exists: 1, wouldCreate: 6, attention: 1, failed: 1, skipped: 2, notOpened: 0, findings: 0 });
    const distinct = ctrl.distinct();
    expect(distinct).toEqual({ products: 6, compounds: 4, newCores: 2, newStereos: 1, furtherBatches: 1 });
    expect(distinctLine(distinct)).toBe(
      "6 products would get an ID: 2 new compounds (new CORE), 1 new stereo variant of known compounds, 1 further batch of known compounds.",
    );
    expect(runOf(ctrl, "ABC-004").rows[0]!.result).toBe("needs a look: The structure has warnings.");
    expect(summaryLines(ctrl.state)).toEqual([
      "The check is finished. Nothing was created or stored.",
      "6 products would get an ID, 1 product had an ID already, 1 needs a look, 1 failed, 2 experiments skipped, 0 not opened, 0 differences to check.",
      distinctLine(distinct),
    ]);
  });

  it("counts compounds without a key one by one", () => {
    const run = {
      phase: "done",
      findings: [],
      rows: [1, 2].map((i) => ({ kind: "would_create", findings: [], outcome: outcome(i, { compound_key: null, status: "new" }) })),
    } as unknown as ExperimentRun;
    expect(distinctCompounds([run])).toMatchObject({ products: 2, compounds: 2, newCores: 2 });
    expect(countRuns([run]).wouldCreate).toBe(2);
  });
});

describe("import walk: stop and progress", () => {
  it("stop() ends the walk after the current experiment and closes the tab", async () => {
    const { tabs, ctrl } = await setup();
    const p = ctrl.run({ mode: "check" });
    await vi.advanceTimersByTimeAsync(500);
    expect(ctrl.state.status).toBe("running");
    ctrl.stop();
    expect(ctrl.state.status).toBe("stopping");
    await settle(p);
    expect(tabs.applies().map((a) => a.ref)).toEqual(["4110"]);
    expect(tabs.events.map((e) => e.op)).toEqual(["create", "remove"]);
    expect(ctrl.state.message).toBe(STOPPED_MESSAGE);
    expect(runOf(ctrl, "ABC-001").phase).toBe("done");
    expect(runOf(ctrl, "ABC-002").rows[0]!.kind).toBe("not_reached");
    expect(summaryLines(ctrl.state).slice(0, 2)).toEqual(["The check ended after 1 of 7 experiments. Nothing was created.", STOPPED_MESSAGE]);
  });

  it("shows the experiment being handled and a rough time left", async () => {
    const { ctrl } = await setup();
    const p = ctrl.run({ mode: "check" });
    await vi.advanceTimersByTimeAsync(3_000);
    expect(ctrl.state.handled).toBe(1);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(progressLine(ctrl.state)).toBe("Experiment 2 of 7: ABC-002 …");
    expect(timeLeftText(ctrl.state)).toBe("less than a minute left");
    await settle(p);
    expect(progressLine(ctrl.state)).toBeNull();
  });
});

describe("import report", () => {
  it("has one line per product, as CSV for the file and tab-separated for the clipboard", async () => {
    const tabs = new FakeTabs().page("4110", {
      apply: (): ImportApplyReply => ({
        ok: true,
        experiment: { ref: "4110", code: "ABC-001" },
        products: [outcome(1, { formula: "C13H10O", yields: { isolated: 52.4, analytical: null }, notes: ["A note, with a comma."] })],
        reaction: "none",
        reaction_message: null,
      }),
    });
    const { ctrl } = await setup([yieldRow("4131", 1, "000004_01_001", { yield: info(64.5) })], tabs);
    await walk(ctrl);
    const csv = ctrl.reportCsv().split("\r\n");
    expect(csv[0]).toBe(REPORT_HEADER.join(","));
    expect(csv[0]).toBe(
      "ELN number,pk,Product,Result,ID,Formula on page,Formula in list,Isolated yield on page (%),Isolated yield in list (%),Isolated yield in registry (%),Findings and notes",
    );
    expect(csv[1]).toBe('ABC-001,4110,1,would get an ID (new compound),,C13H10O,C13H10O,52.4,52.38,,"A note, with a comma."');
    expect(csv).toContain(`ABC-004,4131,1,skipped: ${SKIP_ALL_REGISTERED},000004_01_001,,C21H19N,,64.55,64.5,`);
    expect(csv).toContain(`ABC-005,4150,1,skipped: ${SKIP_NO_YIELD},,,C22H23N,,0,,`);
    expect(csv).toHaveLength(13); // header, 11 products, final line break
    const tsv = ctrl.reportTsv().split("\n");
    expect(tsv[0]).toBe(REPORT_HEADER.join("\t"));
    expect(tsv[1]).toBe("ABC-001\t4110\t1\twould get an ID (new compound)\t\tC13H10O\tC13H10O\t52.4\t52.38\t\tA note, with a comma.");
  });
});
