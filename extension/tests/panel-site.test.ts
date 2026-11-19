// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { controlFrame } from "../src/content/frame";
import { ManualSource } from "../src/content/manual-source";
import { createManualForm } from "../src/content/ui/manual-form";
import { HOST_TAG, mountPanel, type PanelHandle } from "../src/content/ui/panel";
import type { SkippedProduct, SourceState } from "../src/shared/adapter";
import type { AnalyzeResult, RegisterParams } from "../src/shared/protocol";
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
  registerResult,
} from "./helpers";

let handles: PanelHandle[] = [];
afterEach(() => {
  for (const h of handles) h.destroy();
  handles = [];
  document.documentElement.querySelectorAll(HOST_TAG).forEach((n) => n.remove());
});

function siteState(over: Partial<Extract<SourceState, { kind: "ready" }>> = {}): SourceState {
  return {
    kind: "ready",
    experiment: experiment({ url: null }),
    products: [
      product({ productIndex: 1, label: "Product 1", changeToken: "k1" }),
      product({ productIndex: 2, label: "Product 2", changeToken: "k2", structure: { molfile: "M2\nM  END\n" } }),
    ],
    ...over,
  };
}

function mountSite(state: SourceState = siteState(), withManual = true, extensionVersion: string | null = null) {
  const client = new FakeClient();
  const timers = manualTimers();
  const source = new StaticSource(state, { autoLookup: true, confirmBeforeCreate: false });
  const manualSource = new ManualSource();
  const copyText = vi.fn(async () => {});
  const handle = mountPanel({
    doc: document,
    source,
    client,
    copyText,
    schedule: timers.schedule,
    manual: withManual ? { source: manualSource, form: createManualForm(document, manualSource) } : null,
    extensionVersion,
  });
  handles.push(handle);
  return { client, timers, source, manualSource, handle, root: handle.root, copyText };
}

const buttons = (root: ShadowRoot) => Array.from(root.querySelectorAll("button"));
function button(root: ShadowRoot, text: string | RegExp): HTMLButtonElement {
  const found = buttons(root).find((b) => (typeof text === "string" ? b.textContent === text : text.test(b.textContent ?? "")));
  if (!found) throw new Error(`no button ${text}; have: ${buttons(root).map((b) => b.textContent).join(" | ")}`);
  return found;
}
const launcher = (root: ShadowRoot) => root.querySelector<HTMLButtonElement>("button.launcher")!;

async function answerLookups(ctx: ReturnType<typeof mountSite>, results: AnalyzeResult[]) {
  ctx.timers.run();
  const calls = ctx.client.callsOf("analyze").slice(-results.length);
  calls.forEach((c, i) => c.resolve({ ok: true, result: results[i]! }));
  await flush();
}

describe("automatic lookups in the panel", () => {
  it("looks every product up by itself, debounced to one request each", () => {
    const ctx = mountSite();
    expect(ctx.client.callsOf("analyze")).toHaveLength(0);
    ctx.source.set(siteState()); // same input again: no extra lookup
    ctx.timers.run();
    expect(ctx.client.callsOf("analyze")).toHaveLength(2);
    expect(ctx.client.callsOf("analyze").map((c) => (c.params as { product_index: number }).product_index)).toEqual([1, 2]);
  });

  it("looks up again when a product's token changes, and only that product", async () => {
    const ctx = mountSite();
    await answerLookups(ctx, [analyzeResult(), analyzeResult()]);
    const changed = siteState();
    (changed as Extract<SourceState, { kind: "ready" }>).products[1] = product({
      productIndex: 2,
      label: "Product 2",
      changeToken: "k2b",
    });
    ctx.source.set(changed);
    ctx.timers.run();
    expect(ctx.client.callsOf("analyze")).toHaveLength(3);
    expect(ctx.client.last("analyze").params).toMatchObject({ product_index: 2 });
  });

  it("a manual source is never looked up by itself", () => {
    const client = new FakeClient();
    const timers = manualTimers();
    const source = new StaticSource(siteState());
    handles.push(mountPanel({ doc: document, source, client, schedule: timers.schedule }));
    timers.run();
    expect(client.calls).toHaveLength(0);
  });
});

describe("compact view", () => {
  it("shows the experiment once, a card per product and the create line", async () => {
    const ctx = mountSite();
    await answerLookups(ctx, [
      analyzeResult(),
      analyzeResult({ status: "known_compound", core_stereo_id: "000042_02", structure: { stereo_mode: "none" } }),
    ]);
    const text = ctx.root.textContent!;
    expect(ctx.root.querySelector(".experiment")!.textContent).toBe("ABC-008 (4180)");
    expect(text.split("4180").length - 1).toBe(1);
    const cards = Array.from(ctx.root.querySelectorAll(".cards > article"));
    expect(cards.map((c) => c.querySelector("h3")!.textContent)).toEqual(["Product 1", "Product 2"]);
    expect(cards[0]!.textContent).toContain("New compound · Absolute (as drawn)");
    expect(cards[1]!.textContent).toContain("New batch of 000042_02");
    expect(cards[1]!.textContent).not.toContain("·");
    expect(cards[0]!.querySelectorAll("img.thumb.small")).toHaveLength(1);
  });

  it("names the core for a new stereo variant", async () => {
    const ctx = mountSite(siteState({ products: [product({ label: "Product 1" })] }));
    await answerLookups(ctx, [
      analyzeResult({
        status: "known_core",
        other_stereos: [{ core_stereo_id: "000042_01", stereo_no: 1, stereo_mode: "abs", smiles: "C", inchikey: "K", batch_count: 2, svg: null }],
      }),
    ]);
    expect(ctx.root.textContent).toContain("New stereo variant of 000042");
  });

  it("contains no selector, SMILES, InChIKey, manual form or database path by default", async () => {
    const ctx = mountSite();
    ctx.handle.open();
    ctx.client.last("ping").resolve({ ok: true, result: pingResult() });
    await answerLookups(ctx, [analyzeResult(), analyzeResult()]);
    const text = ctx.root.textContent!;
    expect(ctx.root.querySelector("select")).toBeNull();
    expect(ctx.root.querySelector("textarea")!.closest("[hidden]")).not.toBeNull(); // built once, hidden
    expect(ctx.root.querySelector(".advanced")!.hasAttribute("hidden")).toBe(true);
    expect(text).not.toContain("LFQSCWFLJHTTHZ");
    expect(ctx.root.querySelector(".cards")!.textContent).not.toContain("CCO");
    const visible = visibleText(ctx.root);
    expect(visible).not.toContain("registry.sqlite");
    expect(visible).not.toContain("Manual input");
  });

  it("keeps warnings, ignored-stereo checkboxes, slot conflict and errors visible", async () => {
    const ctx = mountSite();
    ctx.handle.open();
    ctx.timers.run();
    const [first, second] = ctx.client.callsOf("analyze");
    first!.resolve({
      ok: true,
      result: analyzeResult({
        structure: { warnings: ["Charged structure."], ignored_stereo: ["allene axis"] },
        warnings: ["Registry in a sync folder."],
        slot_conflict: batch({ full_id: "000009_01_004" }),
      }),
    });
    second!.resolve({ ok: false, error: { code: "structure_parse_failed", message: "Bad atom." } });
    await flush();
    const visible = visibleText(ctx.root);
    for (const t of ["Charged structure.", "Registry in a sync folder.", "allene axis", "000009_01_004", "The structure could not be read", "Bad atom."]) {
      expect(visible).toContain(t);
    }
    expect(ctx.root.querySelectorAll(".cards input[type=checkbox]")).toHaveLength(1);
    expect(button(ctx.root, /^Create ID/).disabled).toBe(true);
  });

  it("Advanced reveals the details without sending anything, and closing it changes nothing", async () => {
    const ctx = mountSite();
    ctx.handle.open();
    ctx.client.last("ping").resolve({ ok: true, result: pingResult() });
    await answerLookups(ctx, [analyzeResult({ batches: [batch()] }), analyzeResult()]);
    const before = ctx.client.calls.length;
    const toggle = button(ctx.root, "Advanced");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    toggle.click();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(ctx.handle.isAdvancedOpen()).toBe(true);
    const visible = visibleText(ctx.root);
    expect(ctx.root.querySelectorAll(".cards select")).toHaveLength(2);
    expect(visible).toContain("LFQSCWFLJHTTHZ-UHFFFAOYSA-N");
    expect(visible).toContain("000042_01_001");
    expect(visible).toContain("registry.sqlite");
    expect(visible).toContain("Manual input");
    expect(ctx.root.querySelector("textarea")).not.toBeNull();
    expect(buttons(ctx.root).filter((b) => b.textContent === "Look up again")).toHaveLength(2);
    ctx.timers.run();
    expect(ctx.client.calls.length).toBe(before);
    toggle.click();
    expect(ctx.client.calls.length).toBe(before);
    expect(ctx.root.querySelector(".cards select")).toBeNull();
    expect(buttons(ctx.root).filter((b) => /^Create ID/.test(b.textContent ?? "") && !b.disabled)).toHaveLength(2);
  });

  it("pre-fills the manual form from the detected experiment", () => {
    const ctx = mountSite();
    button(ctx.root, "Advanced").click();
    const inputs = Array.from(ctx.root.querySelectorAll<HTMLInputElement>(".manual input[type=text]"));
    expect(inputs.map((i) => i.value)).toEqual(["4180", "ABC-008"]);
    const state = ctx.manualSource.getState();
    expect(state.kind).toBe("idle"); // nothing entered yet; key is stored for later
  });

  it("a mode chosen in Advanced stays in force and visible after closing it", async () => {
    const ctx = mountSite(siteState({ products: [product({ label: "Product 1" })] }));
    await answerLookups(ctx, [analyzeResult()]);
    button(ctx.root, "Advanced").click();
    const select = ctx.root.querySelector<HTMLSelectElement>(".cards select")!;
    select.value = "rac";
    select.dispatchEvent(new Event("change"));
    expect(ctx.client.last("analyze").params).toMatchObject({ mode: "rac" });
    ctx.client.last("analyze").resolve({ ok: true, result: analyzeResult({ structure: { stereo_mode: "rac", mode_overridden: true } }) });
    await flush();
    button(ctx.root, "Advanced").click();
    expect(ctx.root.textContent).toContain("New compound · Racemic");
    button(ctx.root, /^Create ID/).click();
    expect((ctx.client.last("register").params as RegisterParams).mode).toBe("rac");
  });
});

describe("one-click create", () => {
  it("sends one register with the analysis' confirm while Advanced is closed, then refreshes", async () => {
    const ctx = mountSite(siteState({ products: [product({ label: "Product 1" })] }));
    await answerLookups(ctx, [analyzeResult()]);
    expect(ctx.handle.isAdvancedOpen()).toBe(false);
    const create = button(ctx.root, "Create ID (new compound)");
    create.click();
    create.click();
    expect(ctx.root.querySelector(".confirm")).toBeNull();
    expect(ctx.client.callsOf("register")).toHaveLength(1);
    expect((ctx.client.last("register").params as RegisterParams).confirm.inchikey).toBe("LFQSCWFLJHTTHZ-UHFFFAOYSA-N");
    ctx.client.last("register").resolve({ ok: true, result: registerResult({ id: "000042_01_002" }) });
    await flush();
    expect(ctx.client.callsOf("analyze")).toHaveLength(2); // refresh after register
    expect(ctx.root.querySelector(".big-id")!.textContent).toBe("000042_01_002");
    expect(ctx.root.textContent).toContain("ID created now.");
  });

  it("shows the compound's folder with Copy after Create ID, and the host's warnings", async () => {
    const ctx = mountSite(siteState({ products: [product({ label: "Product 1" })] }));
    await answerLookups(ctx, [analyzeResult()]);
    button(ctx.root, "Create ID (new compound)").click();
    ctx.client.last("register").resolve({
      ok: true,
      result: registerResult({
        id: "000042_01_002",
        folder: { path: "C:\\Registry\\compounds\\000042_01", created: true },
        warnings: ["The structure file could not be written."],
      }),
    });
    await flush();
    const line = () => ctx.root.querySelector<HTMLElement>(".folder-line");
    expect(line()!.textContent).toContain("Folder: C:\\Registry\\compounds\\000042_01");
    expect(line()!.querySelector(".folder-path")!.textContent).toBe("C:\\Registry\\compounds\\000042_01");
    expect(line()!.querySelector("a")).toBeNull();
    expect(ctx.root.textContent).toContain("The structure file could not be written.");
    // after the refresh the card shows the ID as registered here: the line stays, once
    ctx.client.last("analyze").resolve({
      ok: true,
      result: analyzeResult({
        status: "registered_here",
        id: "000042_01_002",
        core_stereo_id: "000042_01",
        batches: [batch({ full_id: "000042_01_002", experiment_ref: "4180", product_index: 1 })],
      }),
    });
    await flush();
    expect(ctx.root.querySelectorAll(".folder-line")).toHaveLength(1);
    const copy = Array.from(line()!.querySelectorAll("button")).find((b) => b.textContent === "Copy")!;
    expect(copy.getAttribute("aria-label")).toBe("Copy the folder of 000042_01_002");
    copy.click();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledWith("C:\\Registry\\compounds\\000042_01");
  });

  it.each([
    ["null", { folder: null }],
    ["absent", {}],
    ["malformed", { folder: { path: 5, created: true } as never }],
  ])("shows no folder line when the folder is %s, and still the ID", async (_name, over) => {
    const ctx = mountSite(siteState({ products: [product({ label: "Product 1" })] }));
    await answerLookups(ctx, [analyzeResult()]);
    button(ctx.root, "Create ID (new compound)").click();
    ctx.client.last("register").resolve({
      ok: true,
      result: registerResult({ id: "000042_01_002", warnings: ["No folder could be made for 000042_01."], ...over }),
    });
    await flush();
    expect(ctx.root.querySelector(".folder-line")).toBeNull();
    expect(ctx.root.querySelector(".big-id")!.textContent).toBe("000042_01_002");
    expect(ctx.root.textContent).toContain("No folder could be made for 000042_01.");
  });

  it("offers no Create before the analysis is current", async () => {
    const ctx = mountSite(siteState({ products: [product({ label: "Product 1" })] }));
    ctx.timers.run();
    expect(buttons(ctx.root).some((b) => /^Create ID/.test(b.textContent ?? ""))).toBe(false);
    expect(ctx.root.textContent).toContain("Looking up…");
  });
});

describe("skipped products and launcher", () => {
  const skipped: SkippedProduct[] = [
    { productIndex: 2, label: "Product 2", reason: "Reading the structure…", pending: true },
    { productIndex: 3, label: "Product 3", reason: "No structure is drawn for this product." },
  ];

  it("lists skipped products in place with their reason", () => {
    const ctx = mountSite(siteState({ products: [product({ label: "Product 1" })], skipped }));
    const cards = Array.from(ctx.root.querySelectorAll(".cards > article"));
    expect(cards.map((c) => c.querySelector("h3")!.textContent)).toEqual(["Product 1", "Product 2", "Product 3"]);
    expect(cards[1]!.textContent).toContain("Reading the structure…");
    expect(cards[2]!.textContent).toContain("No structure is drawn for this product.");
  });

  it("never opens by itself: the launcher shows the count and the user opens the panel", async () => {
    const ctx = mountSite();
    expect(ctx.handle.isOpen()).toBe(false);
    // Lookups still run while the panel is closed, so the launcher can count.
    await answerLookups(ctx, [analyzeResult({ status: "registered_here", id: "000001_01_001" }), analyzeResult()]);
    expect(ctx.handle.isOpen()).toBe(false);
    expect(launcher(ctx.root).hidden).toBe(false);
    expect(launcher(ctx.root).textContent).toBe("ID 1");
    expect(ctx.client.callsOf("ping")).toHaveLength(0);
    // Neither an unreadable product nor a source error opens it.
    ctx.source.set(siteState({ products: [], skipped: [skipped[1]!] }));
    expect(ctx.handle.isOpen()).toBe(false);
    ctx.source.set({ kind: "error", message: "The page layout changed or the page is not ready: x." });
    expect(ctx.handle.isOpen()).toBe(false);
    ctx.source.set(siteState());
    await answerLookups(ctx, [analyzeResult({ status: "registered_here", id: "000001_01_001" }), analyzeResult()]);
    launcher(ctx.root).click();
    expect(ctx.handle.isOpen()).toBe(true);
    expect(ctx.client.callsOf("ping")).toHaveLength(1);
    expect(button(ctx.root, /^Create ID/).disabled).toBe(false);
    expect(ctx.root.textContent).toContain("000001_01_001");
  });

  it("stays collapsed with a check mark when every product has an ID", async () => {
    const ctx = mountSite();
    await answerLookups(ctx, [
      analyzeResult({ status: "registered_here", id: "000001_01_001" }),
      analyzeResult({ status: "registered_here", id: "000002_01_001" }),
    ]);
    expect(ctx.handle.isOpen()).toBe(false);
    expect(launcher(ctx.root).textContent).toBe("ID ✓");
  });

  it("shows a source error once the user opens the panel, and the experiment in the head", async () => {
    const ctx = mountSite({ kind: "error", message: "The page layout changed or the page is not ready: x." });
    expect(ctx.handle.isOpen()).toBe(false);
    launcher(ctx.root).click();
    expect(ctx.root.textContent).toContain("The page layout changed");
    ctx.source.set(siteState({ experiment: experiment({ ref: "300000", code: "ABC-009" }) }));
    expect(ctx.root.querySelector(".experiment")!.textContent).toBe("ABC-009 (300000)");
    button(ctx.root, "×").click();
    expect(ctx.handle.isOpen()).toBe(false);
  });
});

describe("versions of the extension and the registry program", () => {
  const versions = (root: ShadowRoot) => root.querySelector<HTMLElement>(".versions")!;

  it("shows both under Advanced, and nothing else, when they are from the same release", async () => {
    const ctx = mountSite(siteState(), true, "0.10.0");
    expect(versions(ctx.root).textContent).toBe("Extension 0.10.0");
    launcher(ctx.root).click();
    ctx.client.last("ping").resolve({ ok: true, result: pingResult({ host_version: "0.10.3" }) });
    await flush();
    expect(versions(ctx.root).textContent).toBe("Extension 0.10.0 · registry program 0.10.3");
    expect(versions(ctx.root).closest(".advanced")).not.toBeNull();
    expect(ctx.root.querySelector(".version-notice")).toBeNull();
  });

  it("warns, without blocking anything, when the registry program is from another release", async () => {
    const ctx = mountSite(siteState(), true, "0.11.0");
    launcher(ctx.root).click();
    ctx.client.last("ping").resolve({ ok: true, result: pingResult({ host_version: "0.10.3" }) });
    await flush();
    const notice = ctx.root.querySelector(".version-notice")!;
    expect(notice.textContent).toContain("The extension is version 0.11.0, the registry program version 0.10.3");
    expect(notice.textContent).toContain("update.ps1 on Windows, update.sh on macOS/Linux");
    expect(notice.textContent).toContain("reload the extension");
    expect(versions(ctx.root).textContent).toBe("Extension 0.11.0 · registry program 0.10.3");
    // A warning only: the cards work as before.
    await answerLookups(ctx, [analyzeResult(), analyzeResult()]);
    expect(buttons(ctx.root).filter((b) => /^Create ID/.test(b.textContent ?? "") && !b.disabled)).toHaveLength(2);
    expect(buttons(ctx.root).some((b) => b.textContent === "Retry")).toBe(false);
  });

  it("shows the registry program's version alone when the extension's is not known", async () => {
    const ctx = mountSite();
    expect(versions(ctx.root).hidden).toBe(true);
    launcher(ctx.root).click();
    ctx.client.last("ping").resolve({ ok: true, result: pingResult({ host_version: "0.10.0" }) });
    await flush();
    expect(versions(ctx.root).textContent).toBe("Registry program 0.10.0");
    expect(ctx.root.querySelector(".version-notice")).toBeNull();
  });
});

describe("one panel per frame, only while an experiment is shown", () => {
  it("mounts on an experiment and removes the panel when it goes away", () => {
    const source = new StaticSource({ kind: "idle", message: "nothing" }, { autoLookup: true, confirmBeforeCreate: false });
    const client = new FakeClient();
    const frame = controlFrame(source, () => mountPanel({ doc: document, source, client, schedule: manualTimers().schedule }));
    expect(document.documentElement.querySelectorAll(HOST_TAG)).toHaveLength(0);
    source.set(siteState());
    expect(document.documentElement.querySelectorAll(HOST_TAG)).toHaveLength(1);
    source.set(siteState({ products: [product({ label: "Product 1", changeToken: "z" })] }));
    expect(document.documentElement.querySelectorAll(HOST_TAG)).toHaveLength(1);
    source.set({ kind: "error", message: "x" });
    expect(document.documentElement.querySelectorAll(HOST_TAG)).toHaveLength(1);
    source.set({ kind: "idle", message: "gone" });
    expect(document.documentElement.querySelectorAll(HOST_TAG)).toHaveLength(0);
    expect(frame.panel()).toBeNull();
    frame.stop();
  });
});

/** Text of everything not inside a hidden element. */
function visibleText(root: ShadowRoot): string {
  const parts: string[] = [];
  const walk = (node: Node): void => {
    if (node.nodeType === 1) {
      const el = node as HTMLElement;
      if (el.hidden || el.tagName === "STYLE") return;
    }
    if (node.nodeType === 3) parts.push(node.textContent ?? "");
    node.childNodes.forEach(walk);
  };
  root.childNodes.forEach(walk);
  return parts.join(" ");
}

describe("Create re-checks the page first", () => {
  type Outcome = { result: "unchanged" | "changed" } | { result: "unavailable"; message: string };

  function mountWithRecheck() {
    const client = new FakeClient();
    const timers = manualTimers();
    const rechecks: ((o: Outcome) => void)[] = [];
    class RecheckSource extends StaticSource {
      recheck(): Promise<Outcome> {
        return new Promise((resolve) => rechecks.push(resolve));
      }
    }
    const source = new RecheckSource(siteState({ products: [product({ label: "Product 1" })] }), {
      autoLookup: true,
      confirmBeforeCreate: false,
    });
    const handle = mountPanel({ doc: document, source, client, schedule: timers.schedule });
    handles.push(handle);
    return { client, timers, rechecks, root: handle.root };
  }

  it("shows the busy line, then registers on unchanged", async () => {
    const ctx = mountWithRecheck();
    ctx.timers.run();
    ctx.client.last("analyze").resolve({ ok: true, result: analyzeResult() });
    await flush();
    button(ctx.root, "Create ID (new compound)").click();
    expect(ctx.root.textContent).toContain("Checking the structure on the page…");
    expect(button(ctx.root, "Create ID (new compound)").disabled).toBe(true);
    button(ctx.root, "Create ID (new compound)").click();
    expect(ctx.rechecks).toHaveLength(1);
    ctx.rechecks[0]!({ result: "unchanged" });
    await flush();
    expect(ctx.client.callsOf("register")).toHaveLength(1);
  });

  it("shows why nothing was created", async () => {
    const ctx = mountWithRecheck();
    ctx.timers.run();
    ctx.client.last("analyze").resolve({ ok: true, result: analyzeResult() });
    await flush();
    button(ctx.root, "Create ID (new compound)").click();
    ctx.rechecks[0]!({ result: "unavailable", message: "The page did not answer in time." });
    await flush();
    expect(ctx.root.textContent).toContain("The structure could not be re-checked");
    expect(ctx.root.textContent).toContain("The page did not answer in time.");
    expect(button(ctx.root, "Create ID (new compound)").disabled).toBe(false);
    button(ctx.root, "Create ID (new compound)").click();
    ctx.rechecks[1]!({ result: "changed" });
    await flush();
    expect(ctx.root.textContent).toContain("The structure on the page changed, so no ID was created.");
    expect(ctx.client.callsOf("register")).toHaveLength(0);
  });
});
