// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { CardController, cardName } from "../src/content/card-state";
import { controlFrame } from "../src/content/frame";
import type { OpenReply } from "../src/content/host-client";
import { HOST_TAG, mountPanel, type PanelHandle } from "../src/content/ui/panel";
import type { SourceState } from "../src/shared/adapter";
import { MAX_NAME_TEXT, type AnalyzeResult, type RegisterParams, type SetNameParams } from "../src/shared/protocol";
import {
  FakeClient,
  StaticSource,
  analyzeResult,
  batch,
  experiment,
  flush,
  manualTimers,
  nameInfo,
  product,
  properties,
  registerResult,
  setNameResult,
} from "./helpers";

const EVIL = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>`;

let handles: PanelHandle[] = [];
afterEach(() => {
  for (const h of handles) h.destroy();
  handles = [];
  document.documentElement.querySelectorAll(HOST_TAG).forEach((n) => n.remove());
});

function readyState(over: Partial<Extract<SourceState, { kind: "ready" }>> = {}): SourceState {
  return { kind: "ready", experiment: experiment(), products: [product({ label: "Product 1" })], ...over };
}

function mount(options: { autoLookup?: boolean; confirmBeforeCreate?: boolean; openCompounds?: () => Promise<OpenReply> } = {}) {
  const client = new FakeClient();
  const timers = manualTimers();
  const source = new StaticSource(readyState(), {
    autoLookup: options.autoLookup ?? false,
    confirmBeforeCreate: options.confirmBeforeCreate ?? true,
  });
  const copyText = vi.fn(async () => {});
  const handle = mountPanel({
    doc: document,
    source,
    client,
    copyText,
    schedule: timers.schedule,
    ...(options.openCompounds ? { openCompounds: options.openCompounds } : {}),
  });
  handles.push(handle);
  handle.open();
  return { client, timers, source, handle, root: handle.root, copyText };
}

const buttons = (root: ShadowRoot) => Array.from(root.querySelectorAll("button"));
function button(root: ShadowRoot, text: string | RegExp): HTMLButtonElement {
  const found = buttons(root).find((b) => (typeof text === "string" ? b.textContent === text : text.test(b.textContent ?? "")));
  if (!found) throw new Error(`no button ${text}; have: ${buttons(root).map((b) => b.textContent).join(" | ")}`);
  return found;
}
const maybeButton = (root: ShadowRoot, text: string) => buttons(root).find((b) => b.textContent === text) ?? null;

async function lookUpWith(ctx: ReturnType<typeof mount>, result: AnalyzeResult) {
  button(ctx.root, /^Look up/).click();
  ctx.client.last("analyze").resolve({ ok: true, result });
  await flush();
}

const KNOWN = () =>
  analyzeResult({
    status: "known_compound",
    core_stereo_id: "000042_01",
    core_no: 42,
    batches: [batch()],
    name: nameInfo("(2S)-2-aminopropan-1-ol"),
    structure: { properties: properties() },
  });

describe("card details", () => {
  it("has its own Details toggle, collapsed by default, with nothing of the list in the DOM", async () => {
    const ctx = mount();
    await lookUpWith(ctx, KNOWN());
    const toggle = button(ctx.root, "Details");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(ctx.root.querySelector(".cards dl.details")).toBeNull();
    expect(ctx.root.textContent).not.toContain("aminopropan");
    expect(ctx.root.textContent).not.toContain("g/mol");
  });

  it("shows name, formula, masses, elemental analysis, SMILES, InChI, InChIKey and stereo", async () => {
    const ctx = mount();
    await lookUpWith(ctx, KNOWN());
    button(ctx.root, "Details").click();
    const toggle = button(ctx.root, "Details");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const list = ctx.root.querySelector(".cards dl.details")!;
    expect(ctx.root.getElementById(toggle.getAttribute("aria-controls")!)).toBe(list.parentElement);
    const terms = Array.from(list.querySelectorAll("dt")).map((d) => d.textContent);
    expect(terms).toEqual(["IUPAC name", "Formula", "Molar mass", "Exact mass", "Elemental analysis", "SMILES", "InChI", "InChIKey", "Stereo"]);
    const text = list.textContent!;
    for (const t of [
      "(2S)-2-aminopropan-1-ol",
      "C2H6O",
      "46.07 g/mol",
      "46.0419",
      "C 52.14, H 13.13, O 34.73 %",
      "CCO",
      "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3",
      "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
      "Absolute (as drawn)",
    ]) {
      expect(text).toContain(t);
    }
    expect(buttons(ctx.root).filter((b) => b.textContent === "Copy").map((b) => b.getAttribute("aria-label"))).toEqual([
      "Copy the name of 000042_01",
      "Copy SMILES of Product 1",
      "Copy InChI of Product 1",
      "Copy InChIKey of Product 1",
    ]);
    buttons(ctx.root).find((b) => b.getAttribute("aria-label") === "Copy InChI of Product 1")!.click();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledWith("InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3");
  });

  it("shows the per-fragment formula next to the sum formula when they differ", async () => {
    const ctx = mount();
    await lookUpWith(ctx, analyzeResult({ structure: { formula: "C14H15N.ClH", properties: properties({ sum_formula: "C14H16ClN" }) } }));
    button(ctx.root, "Details").click();
    expect(ctx.root.textContent).toContain("C14H16ClN (per fragment: C14H15N.ClH)");
  });

  it("keeps its state across the card's re-renders and is independent per card", async () => {
    const ctx = mount({ autoLookup: true, confirmBeforeCreate: false });
    ctx.source.set(readyState({ products: [product({ label: "Product 1" }), product({ productIndex: 2, label: "Product 2", changeToken: "t2" })] }));
    ctx.timers.run();
    for (const call of ctx.client.callsOf("analyze")) call.resolve({ ok: true, result: KNOWN() });
    await flush();
    const toggles = buttons(ctx.root).filter((b) => b.textContent === "Details");
    expect(toggles).toHaveLength(2);
    toggles[0]!.click();
    expect(ctx.root.querySelectorAll(".cards dl.details")).toHaveLength(1);
    // A new lookup of card 1 re-renders it as a whole.
    ctx.source.set(readyState({ products: [product({ label: "Product 1", changeToken: "t1b" }), product({ productIndex: 2, label: "Product 2", changeToken: "t2" })] }));
    ctx.timers.run();
    ctx.client.last("analyze").resolve({ ok: true, result: KNOWN() });
    await flush();
    const after = buttons(ctx.root).filter((b) => b.textContent === "Details");
    expect(after.map((b) => b.getAttribute("aria-expanded"))).toEqual(["true", "false"]);
    expect(ctx.root.querySelectorAll(".cards dl.details")).toHaveLength(1);
  });

  it("with Advanced open shows the list once on every card and no toggle", async () => {
    const ctx = mount();
    await lookUpWith(ctx, KNOWN());
    button(ctx.root, "Details").click(); // open before: still one list
    button(ctx.root, "Advanced").click();
    expect(ctx.root.querySelectorAll(".cards dl.details")).toHaveLength(1);
    expect(maybeButton(ctx.root, "Details")).toBeNull();
    expect(ctx.root.querySelector(".cards")!.textContent!.split("LFQSCWFLJHTTHZ-UHFFFAOYSA-N").length - 1).toBe(1);
    button(ctx.root, "Advanced").click();
    expect(button(ctx.root, "Details").getAttribute("aria-expanded")).toBe("true");
  });

  it("an older host without properties or name shows less, without a name row", async () => {
    const ctx = mount();
    const old = analyzeResult({ status: "known_compound", core_stereo_id: "000042_01" });
    delete (old.structure as { properties?: unknown }).properties;
    await lookUpWith(ctx, old);
    button(ctx.root, "Details").click();
    const terms = Array.from(ctx.root.querySelectorAll(".cards dl.details dt")).map((d) => d.textContent);
    expect(terms).toEqual(["Formula", "SMILES", "InChI", "InChIKey", "Stereo"]);
    expect(maybeButton(ctx.root, "Add name")).toBeNull();
  });

  it("puts host text into the DOM as text only", async () => {
    const ctx = mount();
    (window as unknown as Record<string, unknown>).__pwned = 0;
    await lookUpWith(
      ctx,
      analyzeResult({
        core_stereo_id: "000042_01",
        status: "known_compound",
        name: nameInfo(EVIL),
        structure: { smiles: EVIL, inchi: EVIL, properties: properties({ sum_formula: EVIL, composition: [{ element: EVIL, percent: 1 }] }) },
      }),
    );
    button(ctx.root, "Details").click();
    await flush();
    expect(ctx.root.querySelector(".cards script")).toBeNull();
    expect(ctx.root.querySelectorAll(".cards img")).toHaveLength(1); // the thumbnail only
    expect(ctx.root.textContent!.split(EVIL).length - 1).toBeGreaterThanOrEqual(4);
    expect((window as unknown as Record<string, unknown>).__pwned).toBe(0);
  });
});

describe("name editor on a card", () => {
  it("stores a new name for the analysis' CORE_STEREO and shows it, without making the analysis stale", async () => {
    const ctx = mount();
    await lookUpWith(ctx, { ...KNOWN(), name: null });
    button(ctx.root, "Details").click();
    expect(ctx.root.textContent).toContain("No name stored");
    button(ctx.root, "Add name").click();
    await flush();
    const input = ctx.root.querySelector<HTMLInputElement>(".name-row input")!;
    expect(ctx.root.activeElement).toBe(input);
    expect(input.getAttribute("maxlength")).toBe(String(MAX_NAME_TEXT));
    expect(ctx.root.querySelector(`label[for="${input.id}"]`)!.textContent).toBe("Name of 000042_01");
    input.value = "  (2S)-2-aminopropan-1-ol ";
    input.dispatchEvent(new Event("input"));
    button(ctx.root, "Save").click();
    expect(ctx.client.last("set_name").params).toEqual({ core_stereo_id: "000042_01", name: "(2S)-2-aminopropan-1-ol" });
    expect(button(ctx.root, "Save").disabled).toBe(true);
    ctx.client.last("set_name").resolve({ ok: true, result: setNameResult({ name: nameInfo("(2S)-2-aminopropan-1-ol") }) });
    await flush();
    expect(ctx.root.querySelector(".name-text")!.textContent).toBe("(2S)-2-aminopropan-1-ol");
    expect(ctx.root.activeElement).toBe(button(ctx.root, "Edit"));
    expect(ctx.root.textContent).not.toContain("Out of date");
    expect(ctx.root.querySelector(".analysis.stale")).toBeNull();
    expect(ctx.client.callsOf("analyze")).toHaveLength(1); // nothing re-analysed
    expect(button(ctx.root, /^Create ID/).disabled).toBe(false);
  });

  it("Enter saves, Escape cancels without closing the panel, Remove sends null", async () => {
    const ctx = mount();
    await lookUpWith(ctx, KNOWN());
    button(ctx.root, "Details").click();
    button(ctx.root, "Edit").click();
    let input = ctx.root.querySelector<HTMLInputElement>(".name-row input")!;
    expect(input.value).toBe("(2S)-2-aminopropan-1-ol");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(ctx.handle.isOpen()).toBe(true);
    expect(ctx.root.querySelector(".name-row input")).toBeNull();
    expect(ctx.client.callsOf("set_name")).toHaveLength(0);

    button(ctx.root, "Edit").click();
    input = ctx.root.querySelector<HTMLInputElement>(".name-row input")!;
    input.value = "other";
    input.dispatchEvent(new Event("input"));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect((ctx.client.last("set_name").params as SetNameParams).name).toBe("other");
    ctx.client.last("set_name").resolve({ ok: true, result: setNameResult({ name: nameInfo("other") }) });
    await flush();

    button(ctx.root, "Edit").click();
    button(ctx.root, "Remove").click();
    expect(ctx.client.last("set_name").params).toEqual({ core_stereo_id: "000042_01", name: null });
    ctx.client.last("set_name").resolve({ ok: true, result: setNameResult({ name: null }) });
    await flush();
    expect(ctx.root.textContent).toContain("No name stored");
    expect(maybeButton(ctx.root, "Add name")).not.toBeNull();
  });

  it("sends nothing for a blank or multi-line name and shows the host's refusal", async () => {
    const ctx = mount();
    await lookUpWith(ctx, KNOWN());
    button(ctx.root, "Details").click();
    button(ctx.root, "Edit").click();
    const input = ctx.root.querySelector<HTMLInputElement>(".name-row input")!;
    input.value = "   ";
    input.dispatchEvent(new Event("input"));
    button(ctx.root, "Save").click();
    expect(ctx.client.callsOf("set_name")).toHaveLength(0);
    expect(ctx.root.querySelector(".name-row [role=alert]")!.textContent).toBe("Enter a name first.");
    const again = ctx.root.querySelector<HTMLInputElement>(".name-row input")!;
    again.value = "a\tb";
    again.dispatchEvent(new Event("input"));
    button(ctx.root, "Save").click();
    expect(ctx.client.callsOf("set_name")).toHaveLength(0);
    const third = ctx.root.querySelector<HTMLInputElement>(".name-row input")!;
    third.value = "fine";
    third.dispatchEvent(new Event("input"));
    button(ctx.root, "Save").click();
    ctx.client.last("set_name").resolve({ ok: false, error: { code: "not_found", message: "No stereo variant 000042_01." } });
    await flush();
    expect(ctx.root.textContent).toContain("No stereo variant 000042_01.");
    expect(ctx.root.querySelector<HTMLInputElement>(".name-row input")!.value).toBe("fine");
  });

  it("without an ID says a name can be stored once the compound has one", async () => {
    const ctx = mount();
    await lookUpWith(ctx, analyzeResult({ status: "new", core_stereo_id: null, name: null }));
    button(ctx.root, "Details").click();
    expect(ctx.root.textContent).toContain("a name can be stored once the compound has an ID");
    expect(maybeButton(ctx.root, "Add name")).toBeNull();
  });

  it("names the new CORE_STEREO after an ID was created on this analysis", async () => {
    const ctx = mount(); // confirmBeforeCreate, no auto lookup
    await lookUpWith(ctx, analyzeResult({ status: "new", core_stereo_id: null, name: null }));
    button(ctx.root, /^Create ID/).click();
    button(ctx.root, "Create").click();
    ctx.client.last("register").resolve({
      ok: true,
      result: registerResult({ id: "000077_01_001", batch: batch({ full_id: "000077_01_001", core_stereo_id: "000077_01" }) }),
    });
    await flush();
    button(ctx.root, "Details").click();
    button(ctx.root, "Add name").click();
    const input = ctx.root.querySelector<HTMLInputElement>(".name-row input")!;
    input.value = "new compound";
    input.dispatchEvent(new Event("input"));
    button(ctx.root, "Save").click();
    expect((ctx.client.last("set_name").params as SetNameParams).core_stereo_id).toBe("000077_01");
  });

  it("does not disturb an open confirmation: Create still sends exactly the confirmed register", async () => {
    const ctx = mount(); // confirmBeforeCreate
    await lookUpWith(ctx, KNOWN());
    button(ctx.root, "Details").click();
    button(ctx.root, /^Create ID/).click(); // confirmation open
    const planned = JSON.stringify(ctx.handle.root.querySelector(".confirm")!.textContent);
    button(ctx.root, "Edit").click();
    const input = ctx.root.querySelector<HTMLInputElement>(".name-row input")!;
    input.value = "renamed";
    input.dispatchEvent(new Event("input"));
    button(ctx.root, "Save").click();
    ctx.client.last("set_name").resolve({ ok: true, result: setNameResult({ name: nameInfo("renamed") }) });
    await flush();
    expect(ctx.root.querySelector(".confirm")).not.toBeNull();
    expect(JSON.stringify(ctx.root.querySelector(".confirm")!.textContent)).toBe(planned);
    expect(ctx.root.querySelector(".name-text")!.textContent).toBe("renamed");
    button(ctx.root, "Create").click();
    const params = ctx.client.last("register").params as RegisterParams;
    expect(params.confirm).toEqual({ inchikey: "LFQSCWFLJHTTHZ-UHFFFAOYSA-N", stereo_mode: "abs", key_digest: "d1".repeat(32) });
    expect(params.experiment_ref).toBe("4180");
  });

  it("does not disturb a running register: its answer is still shown", async () => {
    const ctx = mount({ autoLookup: true, confirmBeforeCreate: false });
    ctx.timers.run();
    ctx.client.last("analyze").resolve({ ok: true, result: KNOWN() });
    await flush();
    button(ctx.root, "Details").click();
    button(ctx.root, /^Create ID/).click(); // one click: register sent
    expect(ctx.client.callsOf("register")).toHaveLength(1);
    button(ctx.root, "Edit").click();
    const input = ctx.root.querySelector<HTMLInputElement>(".name-row input")!;
    input.value = "while registering";
    input.dispatchEvent(new Event("input"));
    button(ctx.root, "Save").click();
    ctx.client.last("set_name").resolve({ ok: true, result: setNameResult({ name: nameInfo("while registering") }) });
    await flush();
    expect(ctx.root.textContent).toContain("Creating the ID…");
    ctx.client.last("register").resolve({ ok: true, result: registerResult({ id: "000042_01_002" }) });
    await flush();
    expect(ctx.root.textContent).toContain("000042_01_002");
    expect(ctx.root.textContent).toContain("ID created now.");
    expect(ctx.client.callsOf("register")).toHaveLength(1);
  });

  it("cannot be changed while the analysis is out of date", async () => {
    const ctx = mount();
    await lookUpWith(ctx, KNOWN());
    button(ctx.root, "Details").click();
    ctx.source.set(readyState({ products: [product({ label: "Product 1", changeToken: "t9" })] }));
    expect(ctx.root.textContent).toContain("Out of date");
    expect(button(ctx.root, "Edit").disabled).toBe(true);
    expect(ctx.root.textContent).toContain("Look up again before changing the name.");
  });
});

describe("card name state", () => {
  function controller() {
    const client = new FakeClient();
    const c = new CardController(client, { onChange: () => {} });
    c.setInput({ product: product(), experiment: experiment() });
    return { client, c };
  }

  it("an analysis requested before the name was sent still shows the stored name; a later one shows the host's", async () => {
    const { client, c } = controller();
    const first = c.lookUp();
    client.last("analyze").resolve({ ok: true, result: KNOWN() });
    await first;
    // A lookup already on its way when the name is saved:
    const second = c.lookUp();
    c.applyName("000042_01", nameInfo("new"), c.state.latestSeq);
    client.last("analyze").resolve({ ok: true, result: KNOWN() }); // computed before set_name: old name
    await second;
    expect(cardName(c.state)).toEqual({ target: "000042_01", name: nameInfo("new") });
    expect(c.view().stale).toBe(false);
    const third = c.lookUp();
    client.last("analyze").resolve({ ok: true, result: { ...KNOWN(), name: nameInfo("from host") } });
    await third;
    expect(cardName(c.state)!.name!.text).toBe("from host");
  });

  it("applyName touches neither pending, confirming, registration nor staleness", async () => {
    const { client, c } = controller();
    const p = c.lookUp();
    client.last("analyze").resolve({ ok: true, result: KNOWN() });
    await p;
    c.requestCreate();
    const before = { ...c.state };
    c.applyName("000042_01", null, c.state.latestSeq);
    for (const key of ["analysis", "confirming", "pending", "registration", "needsRefresh", "latestSeq", "error"] as const) {
      expect(c.state[key]).toBe(before[key]);
    }
    expect(c.view().canCreate).toBe(true);
  });

  it("no name row for an older host, and none without an analysis", () => {
    const { c } = controller();
    expect(cardName(c.state)).toBeNull();
  });
});

describe("All compounds link", () => {
  it("is in the main view, not under Advanced, and opens the page", async () => {
    const openCompounds = vi.fn(async (): Promise<OpenReply> => ({ ok: true }));
    const ctx = mount({ openCompounds });
    const link = button(ctx.root, "All compounds");
    expect(link.closest(".advanced")).toBeNull();
    expect(link.getAttribute("aria-label")).toMatch(/^All compounds/);
    link.click();
    expect(openCompounds).toHaveBeenCalledTimes(1);
    expect(link.disabled).toBe(true);
    await flush();
    expect(link.disabled).toBe(false);
    expect(ctx.root.querySelector(".status-line")!.textContent).toContain("opened in a new tab");
  });

  it("shows why the page could not be opened", async () => {
    const ctx = mount({ openCompounds: async () => ({ ok: false, error: { code: "not_allowed", message: "Refused." } }) });
    button(ctx.root, "All compounds").click();
    await flush();
    expect(ctx.root.querySelector(".nav-feedback")!.textContent).toContain("Refused.");
    expect(ctx.root.querySelector(".nav-feedback")!.textContent).toContain("Request refused by the extension");
  });

  it("is there in the ELN's content frame while no experiment is shown", () => {
    const source = new StaticSource({ kind: "idle", message: "No experiment." }, { autoLookup: true, confirmBeforeCreate: false });
    const openCompounds = vi.fn(async (): Promise<OpenReply> => ({ ok: true }));
    const frame = controlFrame(
      source,
      () => mountPanel({ doc: document, source, client: new FakeClient(), openCompounds, schedule: manualTimers().schedule }),
      { alwaysMounted: true },
    );
    const panel = frame.panel()!;
    panel.open();
    const link = Array.from(panel.root.querySelectorAll("button")).find((b) => b.textContent === "All compounds")!;
    expect(link).toBeTruthy();
    expect(link.closest("[hidden]")).toBeNull();
    link.click();
    expect(openCompounds).toHaveBeenCalledTimes(1);
    frame.stop();
  });
});
