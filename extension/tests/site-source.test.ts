// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import type { BridgeClient, MolfileResult } from "../src/content/bridge-client";
import { NO_STRUCTURE, READING, SiteSource, productToken } from "../src/content/site-source";
import type { SourceState } from "../src/shared/adapter";
import { experimentPageUrl } from "../src/shared/import";
import { SITE_ORIGIN } from "../src/shared/protocol";
import { flush } from "./helpers";
import { showExperiment, testIsVisible, type FixtureOptions } from "./page-fixture";

class FakeBridge implements BridgeClient {
  helloAnswer: boolean | null = true;
  helloCalls = 0;
  calls: { domId: string; resolve: (r: MolfileResult) => void }[] = [];
  hello(): Promise<boolean | null> {
    this.helloCalls += 1;
    return Promise.resolve(this.helloAnswer);
  }
  molfile(domId: string): Promise<MolfileResult> {
    return new Promise((resolve) => this.calls.push({ domId, resolve }));
  }
  dispose(): void {}
  /** Answers the oldest open call with a molfile made from its dom id. */
  async answer(molfile?: string): Promise<void> {
    const call = this.calls.find((c) => !(c as { done?: boolean }).done) as
      | ({ done?: boolean } & (typeof this.calls)[number])
      | undefined;
    if (!call) throw new Error("no open molfile call");
    call.done = true;
    call.resolve({ ok: true, molfile: molfile ?? molOf(call.domId) });
    await flush();
  }
  async fail(code: "no_widget" | "timeout", message = "failed"): Promise<void> {
    const call = this.calls.find((c) => !(c as { done?: boolean }).done) as { done?: boolean; resolve: (r: MolfileResult) => void };
    call.done = true;
    call.resolve({ ok: false, code, message });
    await flush();
  }
  open(): string[] {
    return this.calls.filter((c) => !(c as { done?: boolean }).done).map((c) => c.domId);
  }
}

const molOf = (domId: string) => `\n  bridge ${domId}\n\n  0  0  0  0  0  0            999 V2000\nM  END\n`;

let observerCallback: (() => void) | null = null;
function setup(bridge = new FakeBridge()) {
  const source = new SiteSource({
    doc: document,
    bridge,
    isVisible: testIsVisible,
    observe: (onChange) => {
      observerCallback = onChange;
      return () => {
        observerCallback = null;
      };
    },
    schedule: (fn) => {
      fn();
      return () => {};
    },
    helloAttempts: 2,
  });
  return { source, bridge };
}
const mutate = () => observerCallback?.();

type Ready = Extract<SourceState, { kind: "ready" }>;
function ready(source: SiteSource): Ready {
  const s = source.getState();
  if (s.kind !== "ready") throw new Error(JSON.stringify(s));
  return s;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("SiteSource", () => {
  it("is one-click and automatic", () => {
    const { source } = setup();
    expect(source.name).toBe("Sciformation page");
    expect(source.autoLookup).toBe(true);
    expect(source.confirmBeforeCreate).toBe(false);
  });

  it("is idle without an experiment and never says hello there", async () => {
    document.body.textContent = "Login";
    const { source, bridge } = setup();
    source.start();
    await flush();
    expect(source.getState().kind).toBe("idle");
    expect(bridge.helloCalls).toBe(0);
  });

  it("reads molfiles one at a time from the bridge and offers them as products", async () => {
    showExperiment(document);
    const { source, bridge } = setup();
    source.start();
    expect(ready(source).products).toEqual([]);
    expect(ready(source).skipped?.map((s) => s.pending)).toEqual([true, true]);
    await flush();
    expect(bridge.open()).toEqual(["cNfVkc3"]); // one at a time
    await bridge.answer();
    expect(bridge.open()).toEqual(["cNfVkd7"]);
    await bridge.answer();
    const state = ready(source);
    expect(state.experiment).toEqual({ ref: "4180", code: "ABC-008", url: experimentPageUrl("4180") });
    expect(state.experiment?.url).toBe(`${SITE_ORIGIN}/startUseCase?useCase=performSearch&table=ElnReaction&pk=4180&viewMode=1`);
    expect(state.products.map((p) => [p.productIndex, p.label])).toEqual([
      [1, "Product 1"],
      [2, "Product 2"],
    ]);
    expect(state.products[0]!.changeToken).toBe(productToken("4180", 1, molOf("cNfVkc3")));
    expect(state.skipped).toBeUndefined();
  });

  it("never uses the smiles attribute as the structure", async () => {
    showExperiment(document);
    const { source, bridge } = setup();
    source.start();
    await flush();
    await bridge.answer();
    await bridge.answer();
    for (const p of ready(source).products) {
      expect(p.structure.smiles).toBeUndefined();
      expect(p.structure.molfile).toBe(molOf(p.productIndex === 1 ? "cNfVkc3" : "cNfVkd7"));
      expect(JSON.stringify(p.structure)).not.toContain("CC(=O)O");
    }
  });

  it("re-reads replaced rows; the same molfile changes nothing for the cards", async () => {
    showExperiment(document);
    const { source, bridge } = setup();
    let notified = 0;
    source.subscribe(() => (notified += 1));
    source.start();
    await flush();
    await bridge.answer();
    await bridge.answer();
    const before = ready(source);
    const count = notified;
    // ZK replaced the rows: new nodes, new ids, same smiles.
    showExperiment(document, { rows: [{ id: "newA", smiles: "CCO" }, { id: "newB", smiles: "CC(=O)O" }] });
    mutate();
    await flush();
    expect(bridge.open()).toEqual(["newA"]);
    // While being read again, the products stay as they were (no flicker).
    expect(ready(source)).toEqual(before);
    await bridge.answer(molOf("cNfVkc3"));
    await bridge.answer(molOf("cNfVkd7"));
    expect(bridge.calls.map((c) => c.domId)).toEqual(["cNfVkc3", "cNfVkd7", "newA", "newB"]);
    expect(ready(source)).toEqual(before);
    expect(notified).toBe(count);
  });

  it("notices a changed molfile behind an UNCHANGED smiles attribute after the row was replaced", async () => {
    showExperiment(document);
    const { source, bridge } = setup();
    source.start();
    await flush();
    await bridge.answer();
    await bridge.answer();
    const oldToken = ready(source).products[1]!.changeToken;
    showExperiment(document, { rows: [{ id: "cNfVkc3", smiles: "CCO" }, { id: "moved", smiles: "CC(=O)O" }] });
    mutate();
    await flush();
    expect(bridge.open()).toEqual(["moved"]);
    await bridge.answer("WEDGE MOVED\nM  END\n");
    const p2 = ready(source).products[1]!;
    expect(p2.structure.molfile).toBe("WEDGE MOVED\nM  END\n");
    expect(p2.changeToken).not.toBe(oldToken);
    expect(p2.changeToken).toBe(productToken("4180", 2, "WEDGE MOVED\nM  END\n"));
  });

  it("re-reads only the product whose smiles attribute changed, keeping its old molfile meanwhile", async () => {
    showExperiment(document);
    const { source, bridge } = setup();
    source.start();
    await flush();
    await bridge.answer();
    await bridge.answer();
    document.getElementById("cNfVkd7")!.setAttribute("smiles", "CC(=O)OC");
    mutate();
    await flush();
    expect(bridge.open()).toEqual(["cNfVkd7"]);
    expect(ready(source).products.map((p) => p.structure.molfile)).toEqual([molOf("cNfVkc3"), molOf("cNfVkd7")]);
    expect(ready(source).skipped).toBeUndefined();
    await bridge.answer("NEW MOLFILE\nM  END\n");
    expect(ready(source).products[1]!.structure.molfile).toBe("NEW MOLFILE\nM  END\n");
    expect(bridge.calls).toHaveLength(3);
  });

  it("drops a product whose re-read failed instead of offering the old molfile", async () => {
    showExperiment(document, { rows: [{ id: "a", smiles: "C" }] });
    const { source, bridge } = setup();
    source.start();
    await flush();
    await bridge.answer();
    showExperiment(document, { rows: [{ id: "b", smiles: "C" }] });
    mutate();
    await flush();
    await bridge.fail("timeout", "late");
    expect(ready(source).products).toEqual([]);
    expect(ready(source).skipped?.[0]?.reason).toContain("late");
  });

  describe("recheck", () => {
    async function twoProducts() {
      showExperiment(document);
      const s = setup();
      s.source.start();
      await flush();
      await s.bridge.answer();
      await s.bridge.answer();
      return s;
    }

    it("answers unchanged when the page still has exactly this molfile (no cache)", async () => {
      const { source, bridge } = await twoProducts();
      const product = ready(source).products[0]!;
      const p = source.recheck(product);
      await flush();
      expect(bridge.open()).toEqual(["cNfVkc3"]);
      await bridge.answer(molOf("cNfVkc3"));
      expect(await p).toEqual({ result: "unchanged" });
      expect(bridge.calls).toHaveLength(3);
    });

    it("answers changed for a different molfile and publishes it", async () => {
      const { source, bridge } = await twoProducts();
      const product = ready(source).products[1]!;
      const p = source.recheck(product);
      await flush();
      await bridge.answer(molOf("cNfVkd7") + " ");
      expect(await p).toEqual({ result: "changed" });
      expect(ready(source).products[1]!.structure.molfile).toBe(molOf("cNfVkd7") + " ");
      expect(ready(source).products[1]!.changeToken).not.toBe(product.changeToken);
    });

    it("answers changed when the row is gone", async () => {
      const { source, bridge } = await twoProducts();
      const product = ready(source).products[1]!;
      showExperiment(document, { rows: [{ id: "cNfVkc3", smiles: "CCO" }] });
      expect(await source.recheck(product)).toEqual({ result: "changed" });
      expect(bridge.calls).toHaveLength(2);
    });

    it("answers changed when the row has no structure any more", async () => {
      const { source } = await twoProducts();
      const product = ready(source).products[1]!;
      showExperiment(document, { rows: [{ id: "cNfVkc3", smiles: "CCO" }, { id: "x", smiles: "" }] });
      expect(await source.recheck(product)).toEqual({ result: "changed" });
    });

    it("answers changed when another experiment is shown", async () => {
      const { source } = await twoProducts();
      const product = ready(source).products[0]!;
      showExperiment(document, { key: "300000" });
      expect(await source.recheck(product)).toEqual({ result: "changed" });
      expect(ready(source).experiment?.ref).toBe("300000");
    });

    it("answers unavailable with the bridge's message on an error or timeout", async () => {
      const { source, bridge } = await twoProducts();
      const product = ready(source).products[0]!;
      const p = source.recheck(product);
      await flush();
      await bridge.fail("timeout", "The ELN server did not send the structure in time.");
      expect(await p).toEqual({ result: "unavailable", message: "The ELN server did not send the structure in time." });
    });

    it("re-reads the page once after no_widget and uses the new handle", async () => {
      const { source, bridge } = await twoProducts();
      const product = ready(source).products[0]!;
      const p = source.recheck(product);
      await flush();
      showExperiment(document, { rows: [{ id: "fresh", smiles: "CCO" }, { id: "cNfVkd7", smiles: "CC(=O)O" }] });
      await bridge.fail("no_widget", "gone");
      await flush();
      expect(bridge.open()).toContain("fresh");
      // Answer every open call in order; the recheck's call is for "fresh".
      while (bridge.open().length > 0) {
        const id = bridge.open()[0]!;
        await bridge.answer(id === "fresh" ? molOf("cNfVkc3") : undefined);
      }
      expect(await p).toEqual({ result: "unchanged" });
    });

    it("waits for a molfile read already running (one bridge request at a time)", async () => {
      showExperiment(document);
      const { source, bridge } = setup();
      source.start();
      await flush();
      await bridge.answer();
      // Product 2 is being read; product 1 is offered.
      const product = ready(source).products[0]!;
      const p = source.recheck(product);
      await flush();
      expect(bridge.open()).toEqual(["cNfVkd7"]);
      await bridge.answer();
      await flush();
      expect(bridge.open()).toEqual(["cNfVkc3"]);
      await bridge.answer(molOf("cNfVkc3"));
      expect(await p).toEqual({ result: "unchanged" });
    });
  });

  it("lists rows without a structure and rows that could not be read", async () => {
    showExperiment(document, { rows: [{ id: "a", smiles: "" }, { id: "b", smiles: "C" }, { id: "c", smiles: "N" }] });
    const { source, bridge } = setup();
    source.start();
    await flush();
    await bridge.fail("timeout", "The ELN server did not send the structure in time.");
    await bridge.answer();
    const state = ready(source);
    expect(state.products.map((p) => p.productIndex)).toEqual([3]);
    expect(state.skipped?.[0]).toEqual({ productIndex: 1, label: "Product 1", reason: NO_STRUCTURE });
    expect(state.skipped?.[1]?.productIndex).toBe(2);
    expect(state.skipped?.[1]?.pending).toBeUndefined();
    expect(state.skipped?.[1]?.reason).toContain("did not send the structure in time");
    // A failed product is not asked again automatically.
    mutate();
    await flush();
    expect(bridge.calls).toHaveLength(2);
  });

  it("asks again with the new handle after no_widget", async () => {
    showExperiment(document, { rows: [{ id: "old", smiles: "C" }] });
    const { source, bridge } = setup();
    source.start();
    await flush();
    showExperiment(document, { rows: [{ id: "new", smiles: "C" }] });
    await bridge.fail("no_widget");
    expect(bridge.open()).toEqual(["new"]);
    await bridge.answer();
    expect(ready(source).products).toHaveLength(1);
  });

  it("gives up after repeated no_widget for the same token", async () => {
    showExperiment(document, { rows: [{ id: "x", smiles: "C" }] });
    const { source, bridge } = setup();
    source.start();
    await flush();
    for (let i = 0; i < 4; i++) await bridge.fail("no_widget", "gone");
    expect(bridge.open()).toEqual([]);
    expect(ready(source).skipped?.[0]?.pending).toBeUndefined();
  });

  it("reports an error when the bridge never answers hello", async () => {
    showExperiment(document);
    const bridge = new FakeBridge();
    bridge.helloAnswer = null;
    const { source } = setup(bridge);
    source.start();
    await flush();
    await flush();
    expect(bridge.helloCalls).toBe(2);
    const state = source.getState();
    expect(state.kind).toBe("error");
    expect(state.kind === "error" && state.message).toMatch(/^The page layout changed or the page is not ready: /);
  });

  it("reports an error for a key mismatch", async () => {
    showExperiment(document, { incidentKey: "1" });
    const { source } = setup();
    source.start();
    expect(source.getState().kind).toBe("error");
  });

  it("becomes idle when the experiment disappears, and notifies only on real changes", async () => {
    showExperiment(document);
    const { source, bridge } = setup();
    let notified = 0;
    source.subscribe(() => (notified += 1));
    source.start();
    await flush();
    await bridge.answer();
    await bridge.answer();
    const count = notified;
    mutate();
    mutate();
    expect(notified).toBe(count);
    document.body.replaceChildren();
    mutate();
    expect(source.getState().kind).toBe("idle");
    expect(notified).toBe(count + 1);
  });

  it("forgets molfiles of another experiment", async () => {
    showExperiment(document, { rows: [{ id: "a", smiles: "C" }] });
    const { source, bridge } = setup();
    source.start();
    await flush();
    await bridge.answer();
    showExperiment(document, { key: "5", rows: [{ id: "a", smiles: "C" }] });
    mutate();
    await flush();
    expect(bridge.open()).toEqual(["a"]);
    expect(ready(source).experiment?.ref).toBe("5");
  });

  describe("yields", () => {
    async function oneProduct(options: FixtureOptions) {
      showExperiment(document, options);
      const s = setup();
      s.source.start();
      await flush();
      await s.bridge.answer();
      return s;
    }

    it("puts the row's yields on each product", async () => {
      const { source } = await oneProduct({ rows: [{ id: "a", smiles: "C", yields: { isolated: "76.2", analytical: "90" } }] });
      expect(ready(source).products[0]!.yields).toEqual({ isolated: 76.2, analytical: 90 });
    });

    it("publishes null while the experiment is edited", async () => {
      const { source } = await oneProduct({ rows: [{ id: "a", smiles: "C", yields: { isolated: "76.2", mode: "edit" } }] });
      expect(ready(source).products[0]!.yields).toBeNull();
    });

    it("publishes a change of the yields alone without a new token and without reading the molfile again", async () => {
      const { source, bridge } = await oneProduct({ rows: [{ id: "a", smiles: "C", yields: { isolated: "10" } }] });
      let notified = 0;
      source.subscribe(() => (notified += 1));
      const before = ready(source).products[0]!;
      // Edit mode: the fields are not used.
      showExperiment(document, { rows: [{ id: "a", smiles: "C", yields: { isolated: "10", mode: "edit" } }] });
      mutate();
      await flush();
      expect(ready(source).products[0]!.yields).toBeNull();
      // Saved: the server re-renders the row with the new value, in view mode.
      showExperiment(document, { rows: [{ id: "a", smiles: "C", yields: { isolated: "73.7", title: "73.651758265378" } }] });
      mutate();
      await flush();
      const after = ready(source).products[0]!;
      expect(after.yields).toEqual({ isolated: 73.7, analytical: null });
      expect(after.changeToken).toBe(before.changeToken);
      expect(after.structure).toEqual(before.structure);
      expect(bridge.calls).toHaveLength(1);
      expect(notified).toBe(2);
    });

    it("the page's own observer notices the switch between edit and view mode and a changed value attribute", async () => {
      showExperiment(document, { rows: [{ id: "a", smiles: "C", yields: { isolated: "10" } }] });
      const bridge = new FakeBridge();
      const source = new SiteSource({
        doc: document,
        bridge,
        isVisible: testIsVisible,
        schedule: (fn) => {
          fn();
          return () => {};
        },
      });
      source.start();
      await flush();
      await bridge.answer();
      expect(ready(source).products[0]!.yields).toEqual({ isolated: 10, analytical: null });
      const isolated = document.querySelector<HTMLInputElement>('input[placeholder="isolated"]')!;
      isolated.removeAttribute("readonly");
      await flush();
      expect(ready(source).products[0]!.yields).toBeNull();
      isolated.setAttribute("readonly", "readonly");
      isolated.setAttribute("value", "50");
      await flush();
      expect(ready(source).products[0]!.yields).toEqual({ isolated: 50, analytical: null });
      source.stop();
    });
  });

  it("stop() ends observing", () => {
    showExperiment(document);
    const { source } = setup();
    source.start();
    expect(observerCallback).not.toBeNull();
    source.stop();
    expect(observerCallback).toBeNull();
  });
});
