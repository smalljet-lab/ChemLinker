// @vitest-environment jsdom
/**
 * Names from ChemDraw (`generate_name`): validators, the shared name editor
 * and name row, the panel's automatic request after "Create ID", and the
 * compounds page.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StructureEditor } from "../src/compounds/editor";
import { mountCompoundsPage, type CompoundsPageHandle } from "../src/compounds/page";
import { autoNameTarget, CardController } from "../src/content/card-state";
import { presentError } from "../src/content/errors";
import { createRuntimeHostClient } from "../src/content/host-client";
import { generatorLabel, NameEditor } from "../src/content/name-edit";
import { nameGeneratorOf, pingStateFromReply } from "../src/content/ping-state";
import { renderNameRow, type NameRowOptions } from "../src/content/ui/details";
import { makeEl } from "../src/content/ui/dom";
import { HOST_TAG, mountPanel, type PanelHandle } from "../src/content/ui/panel";
import {
  isGenerateNameResult,
  isNameInfo,
  isResultOf,
  nameSource,
  NO_NAME_GENERATOR,
  readNameGenerator,
} from "../src/content/validate";
import type { SourceState } from "../src/shared/adapter";
import type { AnalyzeResult, BackgroundReply, GenerateNameParams, NameGeneratorInfo, NameInfo, PingResult } from "../src/shared/protocol";
import {
  FakeClient,
  StaticSource,
  analyzeResult,
  batch,
  compoundItem,
  compoundsResult,
  compoundVariant,
  coreFindResult,
  experiment,
  flush,
  foundVariant,
  generatedName,
  generateNameResult,
  manualTimers,
  nameInfo,
  pingResult,
  pingWithGenerator,
  product,
  registerResult,
} from "./helpers";

const AVAILABLE: NameGeneratorInfo = { available: true, label: "ChemDraw" };
type Failure = Extract<BackgroundReply, { ok: false }>;

// ------------------------------------------------------------------ validators

describe("validators: generate_name, name source, name generator", () => {
  it("accepts generate_name answers with and without a name", () => {
    expect(isGenerateNameResult(generateNameResult())).toBe(true);
    expect(
      isGenerateNameResult(generateNameResult({ name: null, generated: false, changed: false, message: "ChemDraw could not name this structure." })),
    ).toBe(true);
    // Not generated, but a name was already stored (replace: false).
    expect(isGenerateNameResult(generateNameResult({ name: nameInfo("kept"), generated: false, changed: false, message: "A name is already stored." }))).toBe(true);
    expect(isResultOf("generate_name", generateNameResult(), 1)).toBe(true);
  });

  it.each([
    ["no core_stereo_id", { ...generateNameResult(), core_stereo_id: undefined }],
    ["empty core_stereo_id", { ...generateNameResult(), core_stereo_id: "" }],
    ["generated as text", { ...generateNameResult(), generated: "yes" }],
    ["changed missing", { ...generateNameResult(), changed: undefined }],
    ["message as number", { ...generateNameResult(), message: 5 }],
    ["message missing", { ...generateNameResult(), message: undefined }],
    ["warnings not a list", { ...generateNameResult(), warnings: "w" }],
    ["name as text", { ...generateNameResult(), name: "ethanol" }],
    ["generated without a name", { ...generateNameResult(), name: null, generated: true }],
    ["not an object", "ok"],
  ])("rejects a generate_name answer with %s", (_name, value) => {
    expect(isGenerateNameResult(value)).toBe(false);
    expect(isResultOf("generate_name", value, 1)).toBe(false);
  });

  it("the host client turns a malformed generate_name answer into host_bad_response", async () => {
    const client = createRuntimeHostClient(async () => ({ ok: true, result: { generated: true } }));
    const reply = (await client.request("generate_name", { core_stereo_id: "000042_01", replace: false })) as Failure;
    expect(reply.ok).toBe(false);
    expect(reply.error.code).toBe("host_bad_response");
  });

  it("the host client passes a valid answer and the request through", async () => {
    const sent: unknown[] = [];
    const client = createRuntimeHostClient(async (m) => {
      sent.push(m);
      return { ok: true, result: generateNameResult() };
    });
    const reply = await client.request("generate_name", { core_stereo_id: "000042_01", replace: true });
    expect(reply).toEqual({ ok: true, result: generateNameResult() });
    expect(sent).toEqual([{ type: "host-request", op: "generate_name", params: { core_stereo_id: "000042_01", replace: true } }]);
  });

  it("reads the source of a stored name: absent or unknown is the user", () => {
    expect(nameSource(generatedName("x"))).toBe("chemdraw");
    expect(nameSource(nameInfo("x"))).toBe("user");
    expect(nameSource({ ...nameInfo("x"), source: "user" })).toBe("user");
    expect(nameSource({ ...nameInfo("x"), source: "openai" as never })).toBe("user");
    expect(nameSource({ ...nameInfo("x"), source: 7 as never })).toBe("user");
    expect(nameSource(null)).toBe("user");
    expect(nameSource(undefined)).toBe("user");
    // An unknown source does not make the name itself invalid.
    expect(isNameInfo({ ...nameInfo("x"), source: "future" })).toBe(true);
  });

  it("reads the name generator of a ping: absent or malformed is not available", () => {
    expect(readNameGenerator(pingWithGenerator())).toEqual({ available: true, label: "ChemDraw" });
    expect(readNameGenerator(pingWithGenerator("ChemDraw 23"))).toEqual({ available: true, label: "ChemDraw 23" });
    expect(readNameGenerator(pingWithGenerator(null))).toEqual({ available: true, label: null });
    expect(readNameGenerator(pingWithGenerator("  "))).toEqual({ available: true, label: null });
    expect(readNameGenerator({ ...pingResult(), name_generator: { available: true } })).toEqual({ available: true, label: null });
    for (const bad of [
      undefined,
      null,
      "ChemDraw",
      true,
      [],
      {},
      { available: "yes", label: "ChemDraw" },
      { available: 1, label: "ChemDraw" },
      { available: true, label: 5 },
      { available: false, label: "ChemDraw" },
    ]) {
      expect(readNameGenerator({ ...pingResult(), name_generator: bad })).toEqual(NO_NAME_GENERATOR);
    }
    expect(readNameGenerator(pingResult())).toEqual(NO_NAME_GENERATOR);
    expect(readNameGenerator(null)).toEqual(NO_NAME_GENERATOR);
  });

  it("the ping state carries the name generator; only an ok ping makes it available", () => {
    const ok = pingStateFromReply({ ok: true, result: pingWithGenerator() });
    expect(nameGeneratorOf(ok)).toEqual(AVAILABLE);
    expect(nameGeneratorOf(pingStateFromReply({ ok: true, result: pingResult() }))).toEqual(NO_NAME_GENERATOR);
    expect(nameGeneratorOf(pingStateFromReply({ ok: false, error: { code: "host_unavailable", message: "m" } }))).toEqual(NO_NAME_GENERATOR);
    expect(nameGeneratorOf({ kind: "pending" })).toEqual(NO_NAME_GENERATOR);
    expect(nameGeneratorOf({ kind: "idle" })).toEqual(NO_NAME_GENERATOR);
    const mismatch = pingStateFromReply({ ok: true, result: { ...pingWithGenerator(), protocol_version: 2 } });
    expect(nameGeneratorOf(mismatch)).toEqual(NO_NAME_GENERATOR);
  });

  it("names the program by its label, else ChemDraw", () => {
    expect(generatorLabel(AVAILABLE)).toBe("ChemDraw");
    expect(generatorLabel({ available: true, label: "ChemDraw 23" })).toBe("ChemDraw 23");
    expect(generatorLabel({ available: true, label: null })).toBe("ChemDraw");
    expect(generatorLabel(null)).toBe("ChemDraw");
  });

  it("explains a time-out of generate_name differently from a slow start", () => {
    const error = { code: "host_timeout", message: "The local registry program did not answer within 60 seconds." };
    expect(presentError(error, "generate_name").hint).toContain("did not finish in time");
    expect(presentError(error, "ping").hint).toContain("chemistry library");
  });
});

// ------------------------------------------------------------------ name editor

describe("name editor: generate", () => {
  function editor() {
    const client = new FakeClient();
    const announced: string[] = [];
    const names = new NameEditor(client, { onChange: () => {}, announce: (m) => announced.push(m) });
    return { client, names, announced };
  }

  it("sends generate_name once, is busy meanwhile, and returns the stored name", async () => {
    const { client, names, announced } = editor();
    const done = names.generate("000042_01", false);
    expect(client.last("generate_name").params).toEqual({ core_stereo_id: "000042_01", replace: false });
    expect(names.state.pending).toEqual({ target: "000042_01", remove: false, generate: true });
    expect(await names.generate("000042_01", false)).toBeNull(); // second click while waiting
    names.begin("000042_01", null); // no editing while waiting
    expect(names.state.editing).toBeNull();
    expect(client.callsOf("generate_name")).toHaveLength(1);
    client.last("generate_name").resolve({ ok: true, result: generateNameResult() });
    const result = await done;
    expect(result!.name!.text).toBe("(2S)-2-aminopropan-1-ol");
    expect(names.state.pending).toBeNull();
    expect(names.state.notice).toBeNull();
    expect(names.takeFocusKey()).toBe("name-edit:000042_01");
    expect(announced).toContain("Name of 000042_01 generated by ChemDraw.");
  });

  it("closes the editor of that name after a generated replacement", async () => {
    const { client, names } = editor();
    names.begin("000042_01", "old");
    const done = names.generate("000042_01", true, { label: "ChemDraw 23" });
    expect(client.last("generate_name").params).toEqual({ core_stereo_id: "000042_01", replace: true });
    client.last("generate_name").resolve({ ok: true, result: generateNameResult() });
    await done;
    expect(names.state.editing).toBeNull();
    expect(names.state.draft).toBe("");
  });

  it("keeps the host's plain words when no name came back, without an error", async () => {
    const { client, names, announced } = editor();
    names.begin("000042_01", "old");
    const done = names.generate("000042_01", true);
    client.last("generate_name").resolve({
      ok: true,
      result: generateNameResult({ name: nameInfo("old"), generated: false, changed: false, message: "ChemDraw is not licensed." }),
    });
    const result = await done;
    expect(result!.generated).toBe(false);
    expect(names.state.notice).toEqual({ target: "000042_01", message: "ChemDraw is not licensed." });
    expect(names.state.problem).toBeNull();
    expect(names.state.editing).toBe("000042_01"); // the user can still type a name
    expect(announced).toContain("ChemDraw is not licensed.");
    names.cancel();
    expect(names.state.notice).toBeNull();
  });

  it("says something when the host gives no message", async () => {
    const { client, names } = editor();
    const done = names.generate("000042_01", false);
    client.last("generate_name").resolve({ ok: true, result: generateNameResult({ name: null, generated: false, changed: false, message: null }) });
    await done;
    expect(names.state.notice!.message).toBe("ChemDraw gave no name for 000042_01.");
  });

  it("keeps a host error as the row's problem", async () => {
    const { client, names } = editor();
    const done = names.generate("000042_01", false);
    client.last("generate_name").resolve({ ok: false, error: { code: "not_found", message: "No stereo variant 000042_01." } });
    expect(await done).toBeNull();
    expect(names.state.problem).toEqual({
      target: "000042_01",
      error: { code: "not_found", message: "No stereo variant 000042_01." },
      message: null,
      op: "generate_name",
    });
    expect(names.takeFocusKey()).toBe("name-generate:000042_01");
  });

  it("automatic: no focus, no status-line message, nothing while the user edits that name", async () => {
    const { client, names, announced } = editor();
    names.begin("000042_01", "");
    names.takeFocusKey();
    expect(await names.generate("000042_01", false, { auto: true })).toBeNull();
    expect(client.calls).toHaveLength(0);
    names.cancel();
    names.takeFocusKey();
    const done = names.generate("000042_01", false, { auto: true });
    client.last("generate_name").resolve({ ok: true, result: generateNameResult({ name: null, generated: false, changed: false, message: "No name." }) });
    await done;
    expect(names.takeFocusKey()).toBeNull();
    expect(announced).toEqual([]);
    expect(names.state.notice!.message).toBe("No name.");
  });
});

// ------------------------------------------------------------------ name row

describe("name row with and without ChemDraw", () => {
  let containers: HTMLElement[] = [];
  afterEach(() => {
    for (const c of containers) c.remove();
    containers = [];
  });

  function row(over: Partial<NameRowOptions> = {}) {
    const client = new FakeClient();
    const container = document.createElement("div");
    document.body.append(container);
    containers.push(container);
    const el = makeEl(document);
    const ctx = { copyText: async () => {}, announce: () => {} };
    const generateCalls: [string, boolean][] = [];
    let options: NameRowOptions;
    const names = new NameEditor(client, { onChange: () => render() });
    function render(): void {
      container.replaceChildren(renderNameRow(el, ctx, names, options));
    }
    options = {
      target: "000042_01",
      name: null,
      save: () => {},
      remove: () => {},
      generator: AVAILABLE,
      generate: (target, replace) => {
        generateCalls.push([target, replace]);
        void names.generate(target, replace, { label: generatorLabel(options.generator) }).then((result) => {
          if (result) {
            options = { ...options, name: result.name };
            render();
          }
        });
      },
      ...over,
    };
    render();
    const btn = (text: string) => Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text) ?? null;
    return { client, names, container, btn, generateCalls, setName: (name: NameInfo | null) => ((options = { ...options, name }), render()) };
  }

  it("not available: no ChemDraw button anywhere, as before", () => {
    for (const generator of [null, undefined, NO_NAME_GENERATOR]) {
      const r = row({ generator });
      expect(r.btn("Add name")).not.toBeNull();
      expect(r.container.textContent).not.toContain("ChemDraw");
      r.setName(nameInfo("x"));
      r.btn("Edit")!.click();
      expect(r.btn("Get from ChemDraw")).toBeNull();
      expect(r.container.textContent).toContain("nothing is generated or sent anywhere else");
    }
    // Available, but the caller offers no way to send: no button either.
    expect(row({ generate: null }).btn("Get name from ChemDraw")).toBeNull();
  });

  it("available, no name: offers Get name from ChemDraw next to Add name", () => {
    const r = row();
    const ask = r.btn("Get name from ChemDraw")!;
    expect(ask).not.toBeNull();
    expect(ask.getAttribute("aria-label")).toBe("Get the name of 000042_01 from ChemDraw");
    expect(ask.parentElement).toBe(r.btn("Add name")!.parentElement);
    expect(ask.dataset.key).toBe("name-generate:000042_01");
  });

  it("uses the label from ping", () => {
    const r = row({ generator: { available: true, label: "ChemDraw 23" } });
    expect(r.btn("Get name from ChemDraw 23")!.getAttribute("aria-label")).toBe("Get the name of 000042_01 from ChemDraw 23");
  });

  it("while waiting: buttons disabled and 'Asking ChemDraw…' (polite); then the generated name with its note", async () => {
    const r = row();
    r.btn("Get name from ChemDraw")!.click();
    expect(r.generateCalls).toEqual([["000042_01", false]]);
    expect(r.client.last("generate_name").params).toEqual({ core_stereo_id: "000042_01", replace: false });
    expect(r.btn("Get name from ChemDraw")!.disabled).toBe(true);
    expect(r.btn("Add name")!.disabled).toBe(true);
    const asking = Array.from(r.container.querySelectorAll("p")).find((p) => p.textContent === "Asking ChemDraw…")!;
    expect(asking.getAttribute("aria-live")).toBe("polite");
    r.client.last("generate_name").resolve({ ok: true, result: generateNameResult() });
    await flush();
    expect(r.container.querySelector(".name-text")!.textContent).toBe("(2S)-2-aminopropan-1-ol");
    expect(r.container.textContent).not.toContain("Asking ChemDraw…");
    expect(r.btn("Get name from ChemDraw")).toBeNull();
    expect(r.btn("Edit")!.disabled).toBe(false);
    const stored = r.container.querySelector(".name-source")!;
    expect(stored.textContent).toBe("Generated by ChemDraw");
    expect(stored.previousElementSibling!.textContent).toMatch(/^Stored /);
  });

  it("a generated name shows its note also when the generator is not available now (label: ChemDraw)", () => {
    const r = row({ generator: null, name: generatedName("x") });
    expect(r.container.querySelector(".name-source")!.textContent).toBe("Generated by ChemDraw");
    const typed = row({ name: nameInfo("typed") });
    expect(typed.container.querySelector(".name-source")).toBeNull();
    const labelled = row({ generator: { available: true, label: "ChemDraw 23" }, name: generatedName("x") });
    expect(labelled.container.querySelector(".name-source")!.textContent).toBe("Generated by ChemDraw 23");
  });

  it("generated: false shows the host's message as a status note, not as an error", async () => {
    const r = row();
    r.btn("Get name from ChemDraw")!.click();
    r.client.last("generate_name").resolve({
      ok: true,
      result: generateNameResult({ name: null, generated: false, changed: false, message: "ChemDraw could not name this structure." }),
    });
    await flush();
    const note = r.container.querySelector("[role=status]")!;
    expect(note.textContent).toBe("ChemDraw could not name this structure.");
    expect(r.container.querySelector(".banner")).toBeNull();
    expect(r.container.querySelector("[role=alert]")).toBeNull();
    expect(r.container.textContent).toContain("No name stored");
    expect(r.btn("Get name from ChemDraw")!.disabled).toBe(false);
  });

  it("a host error uses the row's error box", async () => {
    const r = row();
    r.btn("Get name from ChemDraw")!.click();
    r.client.last("generate_name").resolve({ ok: false, error: { code: "host_timeout", message: "The local registry program did not answer within 60 seconds." } });
    await flush();
    const box = r.container.querySelector(".banner.error[role=alert]")!;
    expect(box.textContent).toContain("did not answer within 60 seconds");
    expect(box.textContent).toContain("Code: host_timeout");
    expect(box.textContent).toContain("did not finish in time");
    expect(r.container.querySelector("[role=status]")).toBeNull();
  });

  it("edit mode with a stored name: Get from ChemDraw replaces it", async () => {
    const r = row({ name: nameInfo("typed by hand") });
    expect(r.btn("Get name from ChemDraw")).toBeNull(); // only without a name
    expect(r.btn("Get from ChemDraw")).toBeNull(); // only in edit mode
    r.btn("Edit")!.click();
    const replace = r.btn("Get from ChemDraw")!;
    expect(replace.getAttribute("aria-label")).toBe("Replace the name of 000042_01 with the one from ChemDraw");
    expect(r.container.textContent).toContain("ChemDraw runs on this PC");
    replace.click();
    expect(r.client.last("generate_name").params).toEqual({ core_stereo_id: "000042_01", replace: true });
    for (const text of ["Save", "Cancel", "Remove", "Get from ChemDraw"]) expect(r.btn(text)!.disabled).toBe(true);
    expect(r.container.querySelector<HTMLInputElement>("input")!.disabled).toBe(true);
    expect(r.container.textContent).toContain("Asking ChemDraw…");
    expect(r.container.textContent).not.toContain("Saving…");
    r.client.last("generate_name").resolve({ ok: true, result: generateNameResult({ name: generatedName("from ChemDraw") }) });
    await flush();
    expect(r.container.querySelector("input")).toBeNull();
    expect(r.container.querySelector(".name-text")!.textContent).toBe("from ChemDraw");
    expect(r.container.querySelector(".name-source")!.textContent).toBe("Generated by ChemDraw");
  });

  it("edit mode: no name back keeps the editor open with the message", async () => {
    const r = row({ name: nameInfo("typed by hand") });
    r.btn("Edit")!.click();
    r.btn("Get from ChemDraw")!.click();
    r.client.last("generate_name").resolve({
      ok: true,
      result: generateNameResult({ name: nameInfo("typed by hand"), generated: false, changed: false, message: "ChemDraw is not licensed." }),
    });
    await flush();
    expect(r.container.querySelector("input")).not.toBeNull();
    expect(r.container.querySelector("[role=status]")!.textContent).toBe("ChemDraw is not licensed.");
    expect(r.btn("Save")!.disabled).toBe(false);
  });

  it("puts the host's message and name into the DOM as text only", async () => {
    const evil = `<img src=x onerror="window.__pwned=1">`;
    (window as unknown as Record<string, unknown>).__pwned = 0;
    const r = row({ generator: { available: true, label: evil } });
    r.btn(`Get name from ${evil}`)!.click();
    r.client.last("generate_name").resolve({ ok: true, result: generateNameResult({ name: null, generated: false, changed: false, message: evil }) });
    await flush();
    expect(r.container.querySelector("img")).toBeNull();
    expect(r.container.querySelector("[role=status]")!.textContent).toBe(evil);
    expect((window as unknown as Record<string, unknown>).__pwned).toBe(0);
  });
});

// ------------------------------------------------------------------ panel

describe("panel: name from ChemDraw after Create ID", () => {
  let handles: PanelHandle[] = [];
  afterEach(() => {
    for (const h of handles) h.destroy();
    handles = [];
    document.documentElement.querySelectorAll(HOST_TAG).forEach((n) => n.remove());
  });

  function readyState(): SourceState {
    return { kind: "ready", experiment: experiment(), products: [product({ label: "Product 1" })] };
  }

  async function mount(options: { ping?: PingResult | null; autoLookup?: boolean; confirmBeforeCreate?: boolean } = {}) {
    const client = new FakeClient();
    const timers = manualTimers();
    const source = new StaticSource(readyState(), {
      autoLookup: options.autoLookup ?? false,
      confirmBeforeCreate: options.confirmBeforeCreate ?? true,
    });
    const handle = mountPanel({ doc: document, source, client, copyText: vi.fn(async () => {}), schedule: timers.schedule });
    handles.push(handle);
    handle.open();
    const ping = options.ping === undefined ? pingWithGenerator() : options.ping;
    if (ping !== null) {
      client.last("ping").resolve({ ok: true, result: ping });
      await flush();
    }
    return { client, timers, source, handle, root: handle.root };
  }
  type Ctx = Awaited<ReturnType<typeof mount>>;

  const buttons = (root: ShadowRoot) => Array.from(root.querySelectorAll("button"));
  function button(root: ShadowRoot, text: string | RegExp): HTMLButtonElement {
    const found = buttons(root).find((b) => (typeof text === "string" ? b.textContent === text : text.test(b.textContent ?? "")));
    if (!found) throw new Error(`no button ${text}; have: ${buttons(root).map((b) => b.textContent).join(" | ")}`);
    return found;
  }
  const maybeButton = (root: ShadowRoot, text: string) => buttons(root).find((b) => b.textContent === text) ?? null;

  async function lookUpWith(ctx: Ctx, result: AnalyzeResult) {
    button(ctx.root, /^Look up/).click();
    ctx.client.last("analyze").resolve({ ok: true, result });
    await flush();
  }

  const NEW = () => analyzeResult({ status: "new", core_stereo_id: null, name: null });
  const NEW_ID = () => registerResult({ id: "000077_01_001", batch: batch({ full_id: "000077_01_001", core_stereo_id: "000077_01" }) });
  const REGISTERED_HERE = (name: NameInfo | null) =>
    analyzeResult({ status: "registered_here", id: "000077_01_001", core_stereo_id: "000077_01", name, batches: [batch({ full_id: "000077_01_001", core_stereo_id: "000077_01" })] });

  async function createId(ctx: Ctx, result = NEW_ID()) {
    button(ctx.root, /^Create ID/).click();
    button(ctx.root, "Create").click();
    ctx.client.last("register").resolve({ ok: true, result });
    await flush();
  }

  it("asks ChemDraw once for the new CORE_STEREO; the ID shows at once, the name later in the name row", async () => {
    const ctx = await mount();
    await lookUpWith(ctx, NEW());
    await createId(ctx);
    expect(ctx.client.callsOf("generate_name")).toHaveLength(1);
    expect((ctx.client.last("generate_name").params as GenerateNameParams)).toEqual({ core_stereo_id: "000077_01", replace: false });
    // The ID is there while ChemDraw works.
    expect(ctx.root.querySelector(".big-id")!.textContent).toBe("000077_01_001");
    expect(ctx.root.textContent).toContain("ID created now.");
    expect(ctx.root.querySelector(".status-line")!.textContent).toBe("ID 000077_01_001 created.");
    button(ctx.root, "Details").click();
    expect(ctx.root.querySelector(".name-row")!.textContent).toContain("Asking ChemDraw…");
    expect(button(ctx.root, "Add name").disabled).toBe(true);
    expect(button(ctx.root, "Get name from ChemDraw").disabled).toBe(true);
    ctx.client.last("generate_name").resolve({
      ok: true,
      result: generateNameResult({ core_stereo_id: "000077_01", name: generatedName("2-aminoethanol") }),
    });
    await flush();
    expect(ctx.root.querySelector(".name-text")!.textContent).toBe("2-aminoethanol");
    expect(ctx.root.querySelector(".name-source")!.textContent).toBe("Generated by ChemDraw");
    expect(ctx.root.querySelector(".big-id")!.textContent).toBe("000077_01_001");
    expect(ctx.client.callsOf("generate_name")).toHaveLength(1);
  });

  it("with automatic lookups: the lookup after register does not hide the generated name, and nothing is asked twice", async () => {
    const ctx = await mount({ autoLookup: true, confirmBeforeCreate: false });
    ctx.timers.run();
    ctx.client.last("analyze").resolve({ ok: true, result: NEW() });
    await flush();
    button(ctx.root, /^Create ID/).click();
    ctx.client.last("register").resolve({ ok: true, result: NEW_ID() });
    await flush();
    expect(ctx.client.callsOf("generate_name")).toHaveLength(1);
    // The lookup after register answers first, without a name yet.
    expect(ctx.client.callsOf("analyze")).toHaveLength(2);
    ctx.client.last("analyze").resolve({ ok: true, result: REGISTERED_HERE(null) });
    await flush();
    ctx.client.last("generate_name").resolve({ ok: true, result: generateNameResult({ core_stereo_id: "000077_01", name: generatedName("2-aminoethanol") }) });
    await flush();
    button(ctx.root, "Details").click();
    expect(ctx.root.querySelector(".name-text")!.textContent).toBe("2-aminoethanol");
    // Later lookups (registered_here, no name) never ask again.
    ctx.source.set({ kind: "ready", experiment: experiment(), products: [product({ label: "Product 1", changeToken: "t2" })] });
    ctx.timers.run();
    ctx.client.last("analyze").resolve({ ok: true, result: REGISTERED_HERE(null) });
    await flush();
    expect(ctx.client.callsOf("generate_name")).toHaveLength(1);
  });

  it("is not sent when the compound already has a name", async () => {
    const ctx = await mount();
    await lookUpWith(
      ctx,
      analyzeResult({ status: "known_compound", core_stereo_id: "000042_01", core_no: 42, batches: [batch()], name: nameInfo("stored") }),
    );
    await createId(ctx, registerResult());
    expect(ctx.root.textContent).toContain("000042_01_002");
    expect(ctx.client.callsOf("generate_name")).toHaveLength(0);
  });

  it("is not sent when ChemDraw is not available, not known yet, or the host is older", async () => {
    for (const ping of [pingResult(), null, { ...pingResult(), name_generator: { available: false, label: null } }]) {
      const ctx = await mount({ ping });
      await lookUpWith(ctx, NEW());
      await createId(ctx);
      expect(ctx.root.querySelector(".big-id")!.textContent).toBe("000077_01_001");
      expect(ctx.client.callsOf("generate_name")).toHaveLength(0);
      button(ctx.root, "Details").click();
      expect(maybeButton(ctx.root, "Get name from ChemDraw")).toBeNull();
      expect(maybeButton(ctx.root, "Add name")).not.toBeNull();
    }
    // An older host reports no names at all: no name row, nothing asked.
    const old = await mount();
    const result = NEW();
    delete (result as { name?: unknown }).name;
    await lookUpWith(old, result);
    await createId(old);
    expect(old.client.callsOf("generate_name")).toHaveLength(0);
  });

  it("is not sent after a plain lookup", async () => {
    const ctx = await mount();
    await lookUpWith(ctx, analyzeResult({ status: "known_compound", core_stereo_id: "000042_01", core_no: 42, batches: [batch()], name: null }));
    button(ctx.root, "Advanced").click(); // offers "Look up again"
    await lookUpWith(ctx, REGISTERED_HERE(null));
    expect(ctx.client.callsOf("analyze")).toHaveLength(2);
    expect(ctx.client.callsOf("generate_name")).toHaveLength(0);
    // The user can still ask by hand.
    button(ctx.root, "Get name from ChemDraw").click();
    expect(ctx.client.last("generate_name").params).toEqual({ core_stereo_id: "000077_01", replace: false });
  });

  it("is not sent after a failed register", async () => {
    const ctx = await mount();
    await lookUpWith(ctx, NEW());
    button(ctx.root, /^Create ID/).click();
    button(ctx.root, "Create").click();
    ctx.client.last("register").resolve({ ok: false, error: { code: "storage_error", message: "Disk full." } });
    await flush();
    expect(ctx.client.callsOf("generate_name")).toHaveLength(0);
  });

  it("no name back: the message shows only in the card's name row; the panel does not open for it", async () => {
    const ctx = await mount();
    await lookUpWith(ctx, NEW());
    await createId(ctx);
    ctx.handle.close();
    ctx.client.last("generate_name").resolve({
      ok: true,
      result: generateNameResult({ core_stereo_id: "000077_01", name: null, generated: false, changed: false, message: "ChemDraw is not licensed on this PC." }),
    });
    await flush();
    expect(ctx.handle.isOpen()).toBe(false);
    ctx.handle.open();
    expect(ctx.root.querySelector(".status-line")!.textContent).toBe("ID 000077_01_001 created.");
    expect(ctx.root.textContent).not.toContain("ChemDraw is not licensed"); // Details closed: nowhere else
    button(ctx.root, "Details").click();
    const row = ctx.root.querySelector(".name-row")!;
    expect(row.querySelector("[role=status]")!.textContent).toBe("ChemDraw is not licensed on this PC.");
    expect(ctx.root.querySelectorAll(".banner")).toHaveLength(0);
    expect(ctx.root.textContent!.split("ChemDraw is not licensed").length - 1).toBe(1);
  });

  it("a host error shows in the name row's error box only, and leaves the ID alone", async () => {
    const ctx = await mount();
    await lookUpWith(ctx, NEW());
    await createId(ctx);
    ctx.client.last("generate_name").resolve({ ok: false, error: { code: "internal_error", message: "ChemDraw crashed." } });
    await flush();
    button(ctx.root, "Details").click();
    const boxes = Array.from(ctx.root.querySelectorAll(".banner.error"));
    expect(boxes).toHaveLength(1);
    expect(boxes[0]!.closest(".name-row")).not.toBeNull();
    expect(boxes[0]!.textContent).toContain("ChemDraw crashed.");
    expect(ctx.root.querySelector(".big-id")!.textContent).toBe("000077_01_001");
    expect(ctx.root.querySelector(".status-line")!.textContent).toBe("ID 000077_01_001 created.");
  });

  it("offers the buttons only once the ping says ChemDraw is there", async () => {
    const ctx = await mount({ ping: null });
    await lookUpWith(ctx, analyzeResult({ status: "known_compound", core_stereo_id: "000042_01", core_no: 42, batches: [batch()], name: null }));
    button(ctx.root, "Details").click();
    expect(maybeButton(ctx.root, "Get name from ChemDraw")).toBeNull();
    ctx.client.last("ping").resolve({ ok: true, result: pingWithGenerator() });
    await flush();
    expect(maybeButton(ctx.root, "Get name from ChemDraw")).not.toBeNull();
  });

  it("Get from ChemDraw in edit mode replaces the stored name on the card", async () => {
    const ctx = await mount();
    await lookUpWith(ctx, analyzeResult({ status: "known_compound", core_stereo_id: "000042_01", core_no: 42, batches: [batch()], name: nameInfo("typed") }));
    button(ctx.root, "Details").click();
    button(ctx.root, "Edit").click();
    button(ctx.root, "Get from ChemDraw").click();
    expect(ctx.client.last("generate_name").params).toEqual({ core_stereo_id: "000042_01", replace: true });
    ctx.client.last("generate_name").resolve({ ok: true, result: generateNameResult({ name: generatedName("from ChemDraw") }) });
    await flush();
    expect(ctx.root.querySelector(".name-text")!.textContent).toBe("from ChemDraw");
    expect(ctx.root.activeElement).toBe(button(ctx.root, "Edit"));
    expect(ctx.client.callsOf("analyze")).toHaveLength(1); // nothing re-analysed
    expect(ctx.root.querySelector(".analysis.stale")).toBeNull();
  });
});

describe("card state: which CORE_STEREO to name after register", () => {
  async function registered(name: NameInfo | null | undefined, onRegisteredWithoutName = vi.fn()) {
    const client = new FakeClient();
    const c = new CardController(client, { onChange: () => {} }, { confirmBeforeCreate: false, onRegisteredWithoutName });
    c.setInput({ product: product(), experiment: experiment() });
    const look = c.lookUp();
    const result = analyzeResult({ status: "new", core_stereo_id: null, name: name ?? null });
    if (name === undefined) delete (result as { name?: unknown }).name;
    client.last("analyze").resolve({ ok: true, result });
    await look;
    expect(autoNameTarget(c.state)).toBeNull(); // a lookup alone: nothing
    const create = c.create();
    client.last("register").resolve({
      ok: true,
      result: registerResult({ id: "000077_01_001", batch: batch({ full_id: "000077_01_001", core_stereo_id: "000077_01" }) }),
    });
    await create;
    return { c, client, onRegisteredWithoutName };
  }

  it("names the new CORE_STEREO once, with the sequence of the latest request", async () => {
    const { c, onRegisteredWithoutName } = await registered(null);
    expect(autoNameTarget(c.state)).toBe("000077_01");
    expect(onRegisteredWithoutName).toHaveBeenCalledTimes(1);
    expect(onRegisteredWithoutName).toHaveBeenCalledWith("000077_01", c.state.latestSeq);
  });

  it("nothing for an older host without names", async () => {
    const { c, onRegisteredWithoutName } = await registered(undefined);
    expect(autoNameTarget(c.state)).toBeNull();
    expect(onRegisteredWithoutName).not.toHaveBeenCalled();
  });

  it("nothing once a name is shown for it", async () => {
    const { c } = await registered(null);
    c.applyName("000077_01", generatedName("x"), c.state.latestSeq);
    expect(autoNameTarget(c.state)).toBeNull();
  });
});

// ------------------------------------------------------------------ compounds page

describe("compounds page: name from ChemDraw", () => {
  let handle: CompoundsPageHandle | null = null;
  afterEach(() => {
    handle?.destroy();
    handle = null;
    document.body.replaceChildren();
  });

  const editor: StructureEditor = { getMolfile: () => null, clear: () => {}, destroy: () => {} };

  async function expanded(ping: PingResult) {
    const client = new FakeClient();
    const root = document.createElement("div");
    document.body.append(root);
    handle = mountCompoundsPage({ doc: document, root, client, copyText: async () => {}, createEditor: () => editor });
    client.last("ping").resolve({ ok: true, result: ping });
    client.last("compounds").resolve({
      ok: true,
      result: compoundsResult({
        items: [compoundItem({ variants: [compoundVariant(), compoundVariant({ core_stereo_id: "000042_02", stereo_no: 2, name: nameInfo("typed") })] })],
      }),
    });
    await flush();
    Array.from(root.querySelectorAll("button")).find((b) => b.textContent === "Show stereo variants and details")!.click();
    client.last("find").resolve({
      ok: true,
      result: coreFindResult({
        variants: [foundVariant(), foundVariant({ core_stereo_id: "000042_02", stereo_no: 2, name: nameInfo("typed") })],
      }),
    });
    await flush();
    const block = (i: number) => Array.from(root.querySelectorAll(".variant-block"))[i]!;
    const btn = (scope: Element, text: string) => Array.from(scope.querySelectorAll("button")).find((b) => b.textContent === text) ?? null;
    return { client, root, block, btn };
  }

  it("not available: no ChemDraw buttons", async () => {
    const ctx = await expanded(pingResult());
    expect(ctx.btn(ctx.block(0), "Add name")).not.toBeNull();
    // The name rows offer nothing from ChemDraw (the "Synthesis route" section writes a ChemDraw file, whether or not ChemDraw names compounds).
    for (const row of Array.from(ctx.root.querySelectorAll(".name-row"))) expect(row.textContent).not.toContain("ChemDraw");
    const withoutRoutes = Array.from(ctx.root.querySelectorAll(".variant-block"))
      .map((b) => Array.from(b.children).filter((c) => !c.classList.contains("route-section")).map((c) => c.textContent).join(""))
      .join("");
    expect(withoutRoutes).not.toContain("ChemDraw");
  });

  it("gets a name for a variant without one and shows it in the variant and the entry", async () => {
    const ctx = await expanded(pingWithGenerator());
    expect(ctx.btn(ctx.block(1), "Get name from ChemDraw")).toBeNull(); // has a name
    ctx.btn(ctx.block(0), "Get name from ChemDraw")!.click();
    expect(ctx.client.last("generate_name").params).toEqual({ core_stereo_id: "000042_01", replace: false });
    expect(ctx.block(0).textContent).toContain("Asking ChemDraw…");
    expect(ctx.btn(ctx.block(0), "Get name from ChemDraw")!.disabled).toBe(true);
    ctx.client.last("generate_name").resolve({ ok: true, result: generateNameResult({ name: generatedName("(2S)-2-aminopropan-1-ol") }) });
    await flush();
    expect(ctx.block(0).querySelector(".name-text")!.textContent).toBe("(2S)-2-aminopropan-1-ol");
    expect(ctx.block(0).querySelector(".name-source")!.textContent).toBe("Generated by ChemDraw");
    expect(ctx.root.querySelector(".compound-name")!.textContent).toBe("(2S)-2-aminopropan-1-ol");
    expect(document.activeElement).toBe(ctx.btn(ctx.block(0), "Edit"));
  });

  it("shows 'no name' in that variant's row only", async () => {
    const ctx = await expanded(pingWithGenerator());
    ctx.btn(ctx.block(0), "Get name from ChemDraw")!.click();
    ctx.client.last("generate_name").resolve({
      ok: true,
      result: generateNameResult({ name: null, generated: false, changed: false, message: "ChemDraw could not name this structure." }),
    });
    await flush();
    expect(ctx.block(0).querySelector(".name-row [role=status]")!.textContent).toBe("ChemDraw could not name this structure.");
    expect(ctx.block(1).querySelector(".name-row [role=status]")).toBeNull();
    expect(ctx.root.querySelector(".messages")!.textContent).toBe("");
  });

  it("replaces a stored name from edit mode", async () => {
    const ctx = await expanded(pingWithGenerator("ChemDraw 23"));
    ctx.btn(ctx.block(1), "Edit")!.click();
    ctx.btn(ctx.block(1), "Get from ChemDraw 23")!.click();
    expect(ctx.client.last("generate_name").params).toEqual({ core_stereo_id: "000042_02", replace: true });
    ctx.client.last("generate_name").resolve({
      ok: true,
      result: generateNameResult({ core_stereo_id: "000042_02", name: generatedName("(2R)-2-aminopropan-1-ol") }),
    });
    await flush();
    expect(ctx.block(1).querySelector(".name-text")!.textContent).toBe("(2R)-2-aminopropan-1-ol");
    expect(ctx.block(1).querySelector(".name-source")!.textContent).toBe("Generated by ChemDraw 23");
  });
});
