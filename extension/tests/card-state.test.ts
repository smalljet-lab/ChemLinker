import { describe, expect, it, vi } from "vitest";
import { CardController, type CardInput } from "../src/content/card-state";
import type { AnalyzeParams, BackgroundReply, RegisterParams } from "../src/shared/protocol";
import {
  FakeClient,
  MOLFILE,
  analyzeResult,
  batch,
  experiment,
  flush,
  product,
  registerResult,
} from "./helpers";

function setup(input: CardInput | null = { product: product(), experiment: experiment() }) {
  const client = new FakeClient();
  const onChange = vi.fn();
  const card = new CardController(client, { onChange });
  if (input) card.setInput(input);
  return { client, card, onChange };
}

async function analyzed(result = analyzeResult(), input?: CardInput) {
  const s = setup(input);
  const p = s.card.lookUp();
  s.client.last("analyze").resolve({ ok: true, result });
  await p;
  return s;
}

const ok = <T>(result: T): BackgroundReply => ({ ok: true, result: result as never });

describe("lookup", () => {
  it("cannot create before a lookup", () => {
    const { card } = setup();
    const view = card.view();
    expect(view.canCreate).toBe(false);
    expect(view.createLabel).toBeNull();
    expect(view.createBlockers.join(" ")).toMatch(/Look up/);
  });

  it("sends analyze with mode null and the experiment when one is known", async () => {
    const { client, card } = setup();
    void card.lookUp();
    expect(client.last("analyze").params).toEqual({
      molfile: MOLFILE,
      mode: null,
      experiment_ref: "4180",
      product_index: 1,
    } satisfies AnalyzeParams);
  });

  it("sends neither experiment_ref nor product_index without an experiment", async () => {
    const { client, card } = setup({ product: product({ structure: { smiles: "CCO" } }), experiment: null });
    void card.lookUp();
    expect(client.last("analyze").params).toEqual({ smiles: "CCO", mode: null });
  });

  it("cannot look up without input, nor while a request runs", async () => {
    const { client, card } = setup(null);
    await card.lookUp();
    expect(client.calls).toHaveLength(0);
    card.setInput({ product: product(), experiment: experiment() });
    void card.lookUp();
    expect(card.view().canLookUp).toBe(false);
    expect(card.view().busy).toBe("analyze");
    expect(card.view().canCreate).toBe(false);
  });

  it("shows the analysis and enables Create for a new compound", async () => {
    const { card } = await analyzed();
    const view = card.view();
    expect(view.status).toBe("new");
    expect(view.stale).toBe(false);
    expect(view.createLabel).toBe("Create ID (new compound)");
    expect(view.canCreate).toBe(true);
  });

  it.each([
    ["new", "Create ID (new compound)"],
    ["known_core", "Create ID (new stereo variant)"],
    ["known_compound", "Create ID (new batch)"],
  ] as const)("labels Create for %s", async (status, label) => {
    const { card } = await analyzed(analyzeResult({ status }));
    expect(card.view().createLabel).toBe(label);
  });

  it("offers no Create when registered here and shows the ID", async () => {
    const { card } = await analyzed(analyzeResult({ status: "registered_here", id: "000042_01_001" }));
    const view = card.view();
    expect(view.createLabel).toBeNull();
    expect(view.canCreate).toBe(false);
    expect(view.existingId).toBe("000042_01_001");
  });

  it("keeps the last analysis when a lookup fails", async () => {
    const { card, client } = await analyzed();
    const p = card.lookUp();
    client.last("analyze").resolve({ ok: false, error: { code: "host_timeout", message: "late" } });
    await p;
    expect(card.state.error?.error.code).toBe("host_timeout");
    expect(card.state.analysis).not.toBeNull();
    expect(card.view().canCreate).toBe(true);
  });
});

describe("create gating", () => {
  it("needs an experiment key", async () => {
    const { card } = await analyzed(analyzeResult(), { product: product(), experiment: null });
    expect(card.view().canCreate).toBe(false);
    expect(card.view().createBlockers.join(" ")).toMatch(/experiment key/);
    card.requestCreate();
    expect(card.state.confirming).toBeNull();
  });

  it("is blocked by a slot conflict naming the occupying ID", async () => {
    const conflict = batch({ full_id: "000007_01_001", product_index: 1 });
    const { card } = await analyzed(analyzeResult({ slot_conflict: conflict }));
    expect(card.view().canCreate).toBe(false);
    expect(card.view().createBlockers.join(" ")).toContain("000007_01_001");
    expect(card.view().createBlockers.join(" ")).toMatch(/retired/);
  });

  it("needs every ignored stereo item ticked", async () => {
    const ignored = ["allene axis at atoms 2-4", "wedge at N5"];
    const { card } = await analyzed(analyzeResult({ structure: { ignored_stereo: ignored } }));
    expect(card.view().ignored).toEqual([
      { text: ignored[0], ticked: false },
      { text: ignored[1], ticked: false },
    ]);
    expect(card.view().canCreate).toBe(false);
    card.setIgnoredTicked(0, true);
    expect(card.view().canCreate).toBe(false);
    card.setIgnoredTicked(1, true);
    expect(card.view().canCreate).toBe(true);
    card.setIgnoredTicked(0, false);
    expect(card.view().canCreate).toBe(false);
  });

  it("drops an open confirmation when an ignored item is unticked", async () => {
    const { card } = await analyzed(analyzeResult({ structure: { ignored_stereo: ["x"] } }));
    card.setIgnoredTicked(0, true);
    card.requestCreate();
    expect(card.state.confirming).not.toBeNull();
    card.setIgnoredTicked(0, false);
    expect(card.state.confirming).toBeNull();
  });
});

describe("staleness", () => {
  const base = { product: product(), experiment: experiment() };
  it.each([
    ["structure text", { ...base, product: product({ structure: { molfile: MOLFILE + " " }, changeToken: "t2" }) }],
    ["change token only", { ...base, product: product({ changeToken: "t2" }) }],
    ["experiment key", { ...base, experiment: experiment({ ref: "999" }) }],
    ["experiment removed", { ...base, experiment: null }],
    ["product no.", { ...base, product: product({ productIndex: 2 }) }],
  ])("a change of the %s makes the analysis stale", async (_n, next) => {
    const { card } = await analyzed();
    card.setInput(next);
    const view = card.view();
    expect(view.stale).toBe(true);
    expect(view.canCreate).toBe(false);
    expect(view.lookUpLabel).toBe("Look up again");
    card.requestCreate();
    expect(card.state.confirming).toBeNull();
  });

  it("no input at all makes it stale", async () => {
    const { card } = await analyzed();
    card.setInput(null);
    expect(card.view().stale).toBe(true);
    expect(card.view().canLookUp).toBe(false);
  });

  it("an experiment code change does not make it stale but cancels a confirmation", async () => {
    const { card } = await analyzed();
    card.requestCreate();
    expect(card.state.confirming).not.toBeNull();
    card.setInput({ product: product(), experiment: experiment({ code: "ABC-009" }) });
    expect(card.view().stale).toBe(false);
    expect(card.state.confirming).toBeNull();
    card.requestCreate();
    expect(card.state.confirming?.params.experiment_code).toBe("ABC-009");
  });

  it("looking up again makes it current", async () => {
    const { card, client } = await analyzed();
    card.setInput({ product: product({ productIndex: 3 }), experiment: experiment() });
    const p = card.lookUp();
    expect((client.last("analyze").params as AnalyzeParams).product_index).toBe(3);
    client.last("analyze").resolve(ok(analyzeResult()));
    await p;
    expect(card.view().stale).toBe(false);
    expect(card.view().canCreate).toBe(true);
  });

  it("a new structure forgets the chosen mode", async () => {
    const { card, client } = await analyzed();
    const p = card.selectMode("rac");
    client.last("analyze").resolve(ok(analyzeResult({ structure: { stereo_mode: "rac", mode_overridden: true } })));
    await p;
    expect(card.state.selectedMode).toBe("rac");
    card.setInput({ product: product({ changeToken: "t2", structure: { smiles: "C[C@H](N)O" } }), experiment: experiment() });
    expect(card.state.selectedMode).toBeNull();
  });
});

describe("stereo mode", () => {
  it("re-analyses immediately with the chosen mode", async () => {
    const { card, client } = await analyzed();
    const p = card.selectMode("rel");
    expect(client.last("analyze").params).toMatchObject({ mode: "rel" });
    expect(card.view().stale).toBe(true); // the old analysis is greyed while the new one loads
    expect(card.view().modeControl?.value).toBe("rel");
    client.last("analyze").resolve(ok(analyzeResult({ structure: { stereo_mode: "rel", mode_overridden: true } })));
    await p;
    expect(card.view().stale).toBe(false);
    expect(card.view().modeControl?.value).toBe("rel");
  });

  it("shows the mode of the analysis, not the one asked for, once current", async () => {
    const { card, client } = await analyzed();
    const p = card.selectMode("rac");
    client.last("analyze").resolve(ok(analyzeResult({ structure: { stereo_mode: "abs", mode_overridden: true } })));
    await p;
    expect(card.view().modeControl?.value).toBe("abs");
  });

  it("pre-selects the analysed mode", async () => {
    const { card } = await analyzed(analyzeResult({ structure: { stereo_mode: "rac" } }));
    const control = card.view().modeControl!;
    expect(control.value).toBe("rac");
    expect(control.disabled).toBe(false);
    expect(control.options.map((o) => o.value)).toEqual(["abs", "rac", "rel", "undefined"]);
  });

  it("is disabled and reads 'No stereo' when the mode is none", async () => {
    const { card } = await analyzed(analyzeResult({ structure: { stereo_mode: "none" } }));
    const control = card.view().modeControl!;
    expect(control.disabled).toBe(true);
    expect(control.options).toEqual([{ value: "none", label: "No stereo", disabled: true }]);
  });

  it.each(["partial", "mixed"] as const)("shows %s as a non-choosable first option", async (mode) => {
    const { card } = await analyzed(analyzeResult({ structure: { stereo_mode: mode } }));
    const control = card.view().modeControl!;
    expect(control.value).toBe(mode);
    expect(control.options[0]).toMatchObject({ value: mode, disabled: true });
    expect(control.options.slice(1).every((o) => !o.disabled)).toBe(true);
  });

  it("ignores invalid modes and mode changes while a request runs", async () => {
    const { card, client } = await analyzed();
    await card.selectMode("bogus");
    expect(client.callsOf("analyze")).toHaveLength(1);
    void card.selectMode("rac");
    void card.selectMode("rel");
    expect(client.callsOf("analyze")).toHaveLength(2);
    expect(card.state.selectedMode).toBe("rac");
  });
});

describe("request sequencing", () => {
  it("discards the answer of an older analyze", async () => {
    const { card, client } = setup();
    const first = card.lookUp();
    card.setInput({ product: product({ productIndex: 2 }), experiment: experiment() });
    const second = card.lookUp(); // supersedes (programmatic; the UI disables the button)
    const [a, b] = client.callsOf("analyze");
    b!.resolve(ok(analyzeResult({ status: "known_core" })));
    await second;
    a!.resolve(ok(analyzeResult({ status: "registered_here", id: "OLD" })));
    await first;
    expect(card.view().status).toBe("known_core");
    expect(card.state.analysis?.snapshot.productIndex).toBe(2);
    expect(card.state.pending).toBeNull();
  });

  it("an older error does not clear the newer pending state", async () => {
    const { card, client } = setup();
    const first = card.lookUp();
    void card.lookUp();
    client.callsOf("analyze")[0]!.resolve({ ok: false, error: { code: "host_timeout", message: "x" } });
    await first;
    expect(card.state.error).toBeNull();
    expect(card.view().busy).toBe("analyze");
  });
});

describe("register", () => {
  it("asks for confirmation first and sends nothing", async () => {
    const { card, client } = await analyzed();
    card.requestCreate();
    expect(client.callsOf("register")).toHaveLength(0);
    const plan = card.state.confirming!;
    expect(plan.experimentRef).toBe("4180");
    expect(plan.experimentCode).toBe("ABC-008");
    expect(plan.productIndex).toBe(1);
    expect(plan.stereoModeLabel).toBe("Absolute (as drawn)");
    card.cancelCreate();
    expect(card.state.confirming).toBeNull();
    expect(client.callsOf("register")).toHaveLength(0);
  });

  it("sends exactly the analysed structure, mode and confirmation", async () => {
    const { card, client } = await analyzed();
    const p1 = card.selectMode("rel");
    client.last("analyze").resolve(
      ok(
        analyzeResult({
          status: "known_core",
          structure: {
            inchikey: "AAAAAAAAAAAAAA-BBBBBBBBBB-N",
            key_digest: "ab".repeat(32),
            stereo_mode: "rel",
            ignored_stereo: ["wedge at N5"],
          },
        }),
      ),
    );
    await p1;
    card.setIgnoredTicked(0, true);
    card.requestCreate();
    void card.confirmCreate();
    const params = client.last("register").params as RegisterParams;
    expect(params).toEqual({
      molfile: MOLFILE,
      mode: "rel",
      experiment_ref: "4180",
      experiment_code: "ABC-008",
      experiment_url: null,
      product_index: 1,
      confirm: { inchikey: "AAAAAAAAAAAAAA-BBBBBBBBBB-N", stereo_mode: "rel", key_digest: "ab".repeat(32) },
      accept_ignored_stereo: ["wedge at N5"],
    } satisfies RegisterParams);
    expect(params.molfile).toBe(MOLFILE); // verbatim, leading blank line kept
    expect("smiles" in params).toBe(false);
  });

  it("sends mode null when the user did not pick one and omits an empty acceptance list", async () => {
    const { card, client } = await analyzed(analyzeResult({ structure: { stereo_mode: "rac", detected_mode: "abs" } }));
    card.requestCreate();
    void card.confirmCreate();
    const params = client.last("register").params as RegisterParams;
    expect(params.mode).toBeNull();
    expect(params.confirm).toEqual({
      inchikey: "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
      stereo_mode: "rac",
      key_digest: "d1".repeat(32),
    });
    expect(params.accept_ignored_stereo).toBeUndefined();
  });

  it("drops a foreign experiment URL", async () => {
    const { card, client } = await analyzed(analyzeResult(), {
      product: product(),
      experiment: experiment({ url: "javascript:alert(1)" }),
    });
    card.requestCreate();
    void card.confirmCreate();
    expect((client.last("register").params as RegisterParams).experiment_url).toBeNull();
  });

  it("sends register only once on a double click", async () => {
    const { card, client } = await analyzed();
    card.requestCreate();
    const a = card.confirmCreate();
    const b = card.confirmCreate();
    card.requestCreate();
    const c = card.confirmCreate();
    expect(client.callsOf("register")).toHaveLength(1);
    expect(card.view().busy).toBe("register");
    expect(card.view().canLookUp).toBe(false);
    void card.lookUp();
    expect(client.callsOf("analyze")).toHaveLength(1);
    client.last("register").resolve(ok(registerResult()));
    await Promise.all([a, b, c]);
    expect(client.callsOf("register")).toHaveLength(1);
  });

  it("treats the card as registered after success", async () => {
    const { card, client } = await analyzed();
    card.requestCreate();
    const p = card.confirmCreate();
    client.last("register").resolve(ok(registerResult({ id: "000042_01_002", created: true })));
    await p;
    const view = card.view();
    expect(view.status).toBe("registered_here");
    expect(view.existingId).toBe("000042_01_002");
    expect(view.canCreate).toBe(false);
    expect(view.createLabel).toBeNull();
    expect(card.state.registration?.result.created).toBe(true);
    card.requestCreate();
    expect(card.state.confirming).toBeNull();
  });

  it("keeps a register answer that arrives after the input changed", async () => {
    const { card, client } = await analyzed();
    card.requestCreate();
    const p = card.confirmCreate();
    card.setInput({ product: product({ changeToken: "t9", structure: { smiles: "CC" } }), experiment: experiment() });
    client.last("register").resolve(ok(registerResult({ id: "000042_01_005" })));
    await p;
    expect(card.state.registration?.result.id).toBe("000042_01_005");
    expect(card.view().stale).toBe(true);
  });

  it("requires a new lookup after a failed register", async () => {
    const { card, client } = await analyzed();
    card.requestCreate();
    const p = card.confirmCreate();
    client.last("register").resolve({ ok: false, error: { code: "host_timeout", message: "no answer" } });
    await p;
    expect(card.state.error?.op).toBe("register");
    expect(card.view().stale).toBe(true);
    expect(card.view().canCreate).toBe(false);
    const q = card.lookUp();
    client.last("analyze").resolve(ok(analyzeResult({ status: "registered_here", id: "000042_01_002" })));
    await q;
    expect(card.view().stale).toBe(false);
    expect(card.view().existingId).toBe("000042_01_002");
  });

  it("does not send when the analysis became stale during the confirmation", async () => {
    const { card, client } = await analyzed();
    card.requestCreate();
    card.setInput({ product: product({ productIndex: 2 }), experiment: experiment() });
    await card.confirmCreate();
    expect(client.callsOf("register")).toHaveLength(0);
  });

  it("notifies on every change", async () => {
    const { onChange } = await analyzed();
    expect(onChange).toHaveBeenCalled();
    await flush();
  });
});

describe("registry changes", () => {
  it("invalidate() makes the shown analysis need a fresh lookup and drops a confirmation", async () => {
    const { card, client } = await analyzed();
    card.requestCreate();
    card.invalidate();
    expect(card.state.confirming).toBeNull();
    expect(card.view().stale).toBe(true);
    expect(card.view().canCreate).toBe(false);
    expect(card.view().staleReason).toMatch(/registry/);
    const p = card.lookUp();
    client.last("analyze").resolve(ok(analyzeResult()));
    await p;
    expect(card.view().stale).toBe(false);
    expect(card.view().canCreate).toBe(true);
  });

  it("an answer to a request sent before the invalidation still needs a fresh lookup", async () => {
    const { card, client } = await analyzed();
    const p = card.lookUp();
    card.invalidate();
    client.last("analyze").resolve(ok(analyzeResult()));
    await p;
    expect(card.view().stale).toBe(true);
    expect(card.view().canCreate).toBe(false);
    const q = card.lookUp();
    client.last("analyze").resolve(ok(analyzeResult()));
    await q;
    expect(card.view().canCreate).toBe(true);
  });

  it("a registry_changed answer to its own lookup makes the old analysis need a fresh lookup", async () => {
    const { card, client } = await analyzed();
    const p = card.lookUp();
    client.last("analyze").resolve({ ok: false, error: { code: "registry_changed", message: "Changed." } });
    await p;
    expect(card.state.error?.error.code).toBe("registry_changed");
    expect(card.view().stale).toBe(true);
    expect(card.view().canCreate).toBe(false);
  });

  it("a registry_changed answer to register requires a fresh lookup", async () => {
    const { card, client } = await analyzed();
    card.requestCreate();
    const p = card.confirmCreate();
    client.last("register").resolve({ ok: false, error: { code: "registry_changed", message: "Changed." } });
    await p;
    expect(card.view().stale).toBe(true);
    expect(card.view().canCreate).toBe(false);
  });
});
