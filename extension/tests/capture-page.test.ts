// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CaptureClient } from "../src/capture/client";
import type { Rect } from "../src/capture/geometry";
import type { Imaging, Picture } from "../src/capture/imaging";
import { isWidePicture, mountCapturePage, type CapturePageHandle } from "../src/capture/page";
import type { StructureEditor } from "../src/compounds/editor";
import {
  MAX_IMAGE_BASE64_CHARS,
  type BackgroundReply,
  type CaptureImageReply,
  type CaptureOp,
  type ParamsByOp,
  type RecognitionStatusResult,
  type RecognizeResult,
} from "../src/shared/protocol";
import { MOLFILE_A, MOLFILE_B, recognizeResult } from "./capture-fixtures";
import { flush } from "./helpers";

const ID = "3f0c2a9e-1b7d-4c55-9e21-0a6d5b8f7c13";
const SHOT = "data:image/png;base64,U0hPVA==";
const EVIL = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>`;

interface Call {
  op: CaptureOp;
  params: unknown;
  resolve: (reply: BackgroundReply) => void;
}

class FakeCapture implements CaptureClient {
  imageCalls: { id: string; resolve: (reply: CaptureImageReply) => void }[] = [];
  calls: Call[] = [];
  image(id: string): Promise<CaptureImageReply> {
    return new Promise((resolve) => this.imageCalls.push({ id, resolve }));
  }
  request<K extends CaptureOp>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>> {
    return new Promise((resolve) => this.calls.push({ op, params, resolve: resolve as (reply: BackgroundReply) => void }));
  }
  callsOf(op: CaptureOp): Call[] {
    return this.calls.filter((c) => c.op === op);
  }
  last(op: CaptureOp): Call {
    const call = this.callsOf(op).at(-1);
    if (!call) throw new Error(`no ${op} call`);
    return call;
  }
}

class FakeEditor implements StructureEditor {
  molfile: string | null = null;
  set: string[] = [];
  cleared = 0;
  failSet = false;
  getMolfile(): string | null {
    return this.molfile;
  }
  setMolfile(molfile: string): void {
    if (this.failSet) throw new Error("cannot parse");
    this.set.push(molfile);
    this.molfile = molfile;
  }
  clear(): void {
    this.molfile = null;
    this.cleared += 1;
  }
  destroy(): void {}
}

function manualTimers() {
  const intervals = new Map<number, { fn: () => void; ms: number }>();
  let next = 1;
  let now = 1_000_000;
  return {
    timers: {
      setInterval: (fn: () => void, ms: number) => {
        intervals.set(next, { fn, ms });
        return next++;
      },
      clearInterval: (handle: unknown) => void intervals.delete(handle as number),
      now: () => now,
    },
    /** Lets `ms` pass and fires every interval of exactly `every` ms once. */
    fire(every: number, ms = every): void {
      now += ms;
      for (const entry of [...intervals.values()]) if (entry.ms === every) entry.fn();
    },
    count: () => intervals.size,
  };
}

let handle: CapturePageHandle | null = null;
afterEach(() => {
  handle?.destroy();
  handle = null;
  document.body.replaceChildren();
  document.body.className = "";
  delete (window as unknown as { __pwned?: unknown }).__pwned;
});

async function settle(): Promise<void> {
  for (let i = 0; i < 6; i++) await flush();
}

interface MountOptions {
  captureId?: string | null;
  picture?: { width: number; height: number };
  crop?: (rect: Rect, width: number, height: number) => string;
  failEditor?: boolean;
  failLoad?: boolean;
  failed?: string | null;
}

function mount(options: MountOptions = {}) {
  const client = new FakeCapture();
  const editor = new FakeEditor();
  const clock = manualTimers();
  const crops: { rect: Rect; width: number; height: number }[] = [];
  const size = options.picture ?? { width: 1600, height: 800 };
  const imaging: Imaging = {
    load: async (url: string): Promise<Picture> => {
      if (options.failLoad) throw new Error("bad picture");
      return { url, ...size };
    },
    crop: (_picture, rect, width, height) => {
      crops.push({ rect, width, height });
      return options.crop ? options.crop(rect, width, height) : "Q1JPUA==";
    },
  };
  const root = document.createElement("div");
  document.body.append(root);
  const copyText = vi.fn(async (_text: string) => {});
  const rendered: string[] = [];
  handle = mountCapturePage({
    doc: document,
    root,
    client,
    imaging,
    createEditor: () => {
      if (options.failEditor) throw new Error("no canvas");
      return editor;
    },
    renderStructure: (molfile) => {
      rendered.push(molfile);
      return `<svg xmlns="http://www.w3.org/2000/svg"><title>${molfile.length}</title></svg>`;
    },
    captureId: options.captureId === undefined ? ID : options.captureId,
    failed: options.failed ?? null,
    copyText,
    timers: clock.timers,
    statusPollMs: 2000,
  });
  const stage = root.querySelector<HTMLElement>(".stage")!;
  // The screenshot is shown 800 x 400 CSS pixels large, at (100, 50) of the window.
  stage.getBoundingClientRect = () => ({ left: 100, top: 50, width: 800, height: 400, right: 900, bottom: 450, x: 100, y: 50, toJSON: () => ({}) });
  return { client, editor, clock, crops, root, copyText, rendered, stage };
}
type Ctx = ReturnType<typeof mount>;

const READY: RecognitionStatusResult = { installed: true, state: "ready", load_seconds: 21 };

function button(root: ParentNode, text: string | RegExp): HTMLButtonElement {
  const all = Array.from(root.querySelectorAll<HTMLButtonElement>("button"));
  const found = all.find((b) => {
    const label = b.getAttribute("aria-label") ?? b.textContent ?? "";
    return typeof text === "string" ? label === text || b.textContent === text : text.test(label);
  });
  if (!found) throw new Error(`no button ${text}; have: ${all.map((b) => b.textContent).join(" | ")}`);
  return found;
}

const visible = (root: HTMLElement, selector: string) => {
  const node = root.querySelector<HTMLElement>(selector);
  return node !== null && node.closest("[hidden]") === null;
};

function pointer(target: Element, type: string, clientX: number, clientY: number): void {
  target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY, button: 0 }));
}

function dragBox(ctx: Ctx, from: [number, number], to: [number, number]): void {
  pointer(ctx.stage, "pointerdown", from[0], from[1]);
  pointer(ctx.stage, "pointermove", (from[0] + to[0]) / 2, (from[1] + to[1]) / 2);
  pointer(ctx.stage, "pointermove", to[0], to[1]);
  pointer(ctx.stage, "pointerup", to[0], to[1]);
}

/** Mounted, screenshot loaded, recognition ready. */
async function opened(options: MountOptions = {}, status: RecognitionStatusResult = READY): Promise<Ctx> {
  const ctx = mount(options);
  await settle();
  ctx.client.imageCalls[0]!.resolve({ ok: true, result: { image: SHOT } });
  await settle(); // the recognition program is asked once the picture is there
  ctx.client.last("recognition_status").resolve({ ok: true, result: status });
  await settle();
  return ctx;
}

/** ... and a recognition of the whole picture finished with `result`. */
async function recognised(result: RecognizeResult = recognizeResult(), options: MountOptions = {}): Promise<Ctx> {
  const ctx = await opened(options);
  button(ctx.root, "Whole picture").click();
  button(ctx.root, "Recognise").click();
  await settle();
  ctx.client.last("recognize").resolve({ ok: true, result });
  await settle();
  return ctx;
}

const cards = (root: ParentNode) => Array.from(root.querySelectorAll<HTMLElement>("li.card"));
const cardOf = (root: ParentNode, n: number, index = 0) => cards(root).filter((c) => c.querySelector(".num")!.textContent === String(n))[index]!;
const select = (card: HTMLElement, kind: "role" | "step") => card.querySelector<HTMLSelectElement>(`select[data-kind="${kind}"]`)!;
function choose(node: HTMLSelectElement, value: string): void {
  node.value = value;
  node.dispatchEvent(new Event("change", { bubbles: true }));
}
const stepSections = (root: ParentNode) => Array.from(root.querySelectorAll<HTMLElement>("section.step:not(.unused)"));
const rxn = (section: HTMLElement) => section.querySelector(".rxn-line code")?.textContent ?? null;

describe("capture page: opening", () => {
  it("asks for its screenshot by the id of its address, and warms the recognition up as soon as the picture is there", async () => {
    const ctx = mount();
    await settle();
    expect(ctx.client.imageCalls.map((c) => c.id)).toEqual([ID]);
    expect(ctx.root.textContent).toContain("Loading the screenshot");
    expect(ctx.client.calls).toEqual([]);
    ctx.client.imageCalls[0]!.resolve({ ok: true, result: { image: SHOT } });
    await settle();
    expect(ctx.client.calls.map((c) => [c.op, c.params])).toEqual([["recognition_status", { start: true }]]);
  });

  it("shows the screenshot and waits for a selection", async () => {
    const ctx = await opened();
    const shot = ctx.root.querySelector<HTMLImageElement>("img.shot")!;
    expect(shot.getAttribute("src")).toBe(SHOT);
    expect(visible(ctx.root, ".stage")).toBe(true);
    expect(button(ctx.root, "Recognise").disabled).toBe(true);
    expect(ctx.root.querySelector(".selection-note")!.textContent).toContain("Nothing is selected yet");
    expect(ctx.root.textContent).toContain("ChemResolver is ready.");
    expect(ctx.root.textContent).toContain("nothing is sent anywhere");
  });

  it("explains itself when it was opened without a screenshot, and asks the host nothing", async () => {
    const ctx = mount({ captureId: null });
    await settle();
    expect(ctx.root.textContent).toContain("No screenshot");
    expect(ctx.root.textContent).toContain("choose Capture reaction");
    expect(ctx.client.imageCalls).toEqual([]);
    expect(ctx.client.calls).toEqual([]);
    expect(visible(ctx.root, ".stage")).toBe(false);
  });

  it("says so when the screenshot is no longer kept", async () => {
    const ctx = mount();
    await settle();
    ctx.client.imageCalls[0]!.resolve({ ok: false, error: { code: "capture_expired", message: "This screenshot is no longer kept." } });
    await settle();
    expect(ctx.root.textContent).toContain("The screenshot is no longer available");
    expect(ctx.root.textContent).toContain("This screenshot is no longer kept.");
    expect(visible(ctx.root, ".stage")).toBe(false);
  });

  it("says so when the picture cannot be decoded", async () => {
    const ctx = mount({ failLoad: true });
    await settle();
    ctx.client.imageCalls[0]!.resolve({ ok: true, result: { image: SHOT } });
    await settle();
    expect(ctx.root.textContent).toContain("could not be shown");
  });
});

describe("capture page: the recognition part", () => {
  it("explains the one command when it is not installed, and blocks Recognise", async () => {
    const ctx = await opened({}, { installed: false, state: "not_installed" });
    expect(ctx.root.textContent).toContain("not installed on this PC");
    expect(ctx.root.querySelector("code.command")!.textContent).toBe("compound-registry install-recognition");
    button(ctx.root, "Whole picture").click();
    expect(button(ctx.root, "Recognise").disabled).toBe(true);
    // No polling while it is not installed; "Check again" asks.
    ctx.clock.fire(2000);
    expect(ctx.client.callsOf("recognition_status")).toHaveLength(1);
    button(ctx.root, "Check again").click();
    expect(ctx.client.callsOf("recognition_status")).toHaveLength(2);
    ctx.client.last("recognition_status").resolve({ ok: true, result: READY });
    await settle();
    expect(button(ctx.root, "Recognise").disabled).toBe(false);
  });

  it("asks again every two seconds while the models load, and stops when they are ready", async () => {
    const ctx = await opened({}, { installed: true, state: "loading", load_seconds: 3 });
    expect(ctx.root.textContent).toContain("loading its models (3 s so far)");
    button(ctx.root, "Whole picture").click();
    expect(button(ctx.root, "Recognise").disabled).toBe(false); // the user need not wait
    ctx.clock.fire(2000);
    expect(ctx.client.callsOf("recognition_status")).toHaveLength(2);
    expect(ctx.client.last("recognition_status").params).toEqual({ start: true });
    ctx.clock.fire(2000); // the last one is still unanswered: no pile-up
    expect(ctx.client.callsOf("recognition_status")).toHaveLength(2);
    ctx.client.last("recognition_status").resolve({ ok: true, result: READY });
    await settle();
    ctx.clock.fire(2000);
    ctx.clock.fire(2000);
    expect(ctx.client.callsOf("recognition_status")).toHaveLength(2);
  });

  it("shows why it failed, with the host's detail as text, and offers to try again", async () => {
    const ctx = await opened({}, { installed: true, state: "failed", detail: `Exit code 1. ${EVIL}` });
    const alert = ctx.root.querySelector('.status-box [role="alert"]')!;
    expect(alert.textContent).toContain("could not be started");
    expect(alert.textContent).toContain(EVIL);
    expect(ctx.root.querySelector(".status-box img")).toBeNull();
    expect(button(ctx.root, "Check again")).toBeTruthy();
  });

  it("tells the user to update when the program does not know the operation", async () => {
    const ctx = mount();
    await settle();
    ctx.client.imageCalls[0]!.resolve({ ok: true, result: { image: SHOT } });
    await settle();
    ctx.client.last("recognition_status").resolve({ ok: false, error: { code: "unknown_op", message: "Unknown operation." } });
    await settle();
    expect(ctx.root.querySelector(".status-box")!.textContent).toContain("Update the program");
  });
});

describe("capture page: selecting", () => {
  it("a drag draws the box; the size is given in pixels of the screenshot, not of the page", async () => {
    const ctx = await opened();
    // Half of the 800 x 400 stage; the screenshot itself is 1600 x 800 (device pixel ratio 2).
    dragBox(ctx, [100, 50], [500, 250]);
    const box = ctx.root.querySelector<HTMLElement>(".sel-box")!;
    expect(box.hidden).toBe(false);
    expect([box.style.left, box.style.top, box.style.width, box.style.height]).toEqual(["0%", "0%", "50%", "50%"]);
    expect(ctx.root.querySelector(".selection-note")!.textContent).toBe("Selected: 800 × 400 pixels.");
    expect(button(ctx.root, "Recognise").disabled).toBe(false);
  });

  it("works in any drag direction and stays inside the picture", async () => {
    const ctx = await opened();
    dragBox(ctx, [700, 350], [-40, 10]);
    const box = ctx.root.querySelector<HTMLElement>(".sel-box")!;
    expect([box.style.left, box.style.top, box.style.width, box.style.height]).toEqual(["0%", "0%", "75%", "75%"]);
  });

  it("Esc and Clear selection remove the box; a click without a drag selects nothing", async () => {
    const ctx = await opened();
    dragBox(ctx, [200, 100], [600, 300]);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(ctx.root.querySelector<HTMLElement>(".sel-box")!.hidden).toBe(true);
    expect(button(ctx.root, "Recognise").disabled).toBe(true);
    dragBox(ctx, [200, 100], [600, 300]);
    button(ctx.root, "Clear selection").click();
    expect(ctx.root.querySelector<HTMLElement>(".sel-box")!.hidden).toBe(true);
    pointer(ctx.stage, "pointerdown", 300, 200);
    pointer(ctx.stage, "pointerup", 300, 200);
    expect(ctx.root.querySelector<HTMLElement>(".sel-box")!.hidden).toBe(true);
  });

  it("Whole picture selects everything", async () => {
    const ctx = await opened();
    button(ctx.root, "Whole picture").click();
    expect(ctx.root.querySelector(".selection-note")!.textContent).toBe("Selected: 1600 × 800 pixels.");
  });

  it("a corner can be dragged, and moved with the arrow keys", async () => {
    const ctx = await opened();
    dragBox(ctx, [100, 50], [500, 250]);
    const handle = ctx.root.querySelector<HTMLButtonElement>(".handle-se")!;
    expect(handle.getAttribute("aria-label")).toContain("Bottom right corner");
    pointer(handle, "pointerdown", 500, 250);
    pointer(ctx.stage, "pointermove", 900, 450);
    pointer(ctx.stage, "pointerup", 900, 450);
    expect(ctx.root.querySelector(".selection-note")!.textContent).toBe("Selected: 1600 × 800 pixels.");
    const nw = ctx.root.querySelector<HTMLButtonElement>(".handle-nw")!;
    nw.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    nw.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(ctx.root.querySelector(".selection-note")!.textContent).toBe("Selected: 1584 × 792 pixels.");
  });

  it("refuses a box that is too small for the program", async () => {
    const ctx = await opened();
    dragBox(ctx, [100, 50], [105, 250]); // 10 picture pixels wide
    expect(ctx.root.querySelector(".selection-note")!.textContent).toContain("too small");
    expect(button(ctx.root, "Recognise").disabled).toBe(true);
  });
});

describe("capture page: recognising", () => {
  it("cuts the selection out at the screenshot's resolution and sends it as base64 with the mode", async () => {
    const ctx = await opened();
    dragBox(ctx, [300, 150], [700, 350]);
    button(ctx.root, "Recognise").click();
    await settle();
    expect(ctx.crops).toEqual([{ rect: { x: 400, y: 200, w: 800, h: 400 }, width: 800, height: 400 }]);
    expect(ctx.client.last("recognize").params).toEqual({ image: "Q1JPUA==", mode: "reaction" });
  });

  it("sends mode molecule when Single molecule is chosen", async () => {
    const ctx = await opened();
    const radio = ctx.root.querySelector<HTMLInputElement>('input[type="radio"][value="molecule"]')!;
    radio.checked = true;
    radio.dispatchEvent(new Event("change", { bubbles: true }));
    button(ctx.root, "Whole picture").click();
    button(ctx.root, "Recognise").click();
    await settle();
    expect(ctx.client.last("recognize").params).toMatchObject({ mode: "molecule" });
    expect(ctx.root.textContent).toContain("Reading the molecule");
  });

  it("makes a large selection smaller until it fits the limit, never larger, and says so in the result", async () => {
    const ctx = await opened({ crop: (_rect, width, height) => "A".repeat(width * height) });
    button(ctx.root, "Whole picture").click(); // 1600 x 800 = 1 280 000 characters at full size
    button(ctx.root, "Recognise").click();
    await settle();
    const sent = ctx.client.last("recognize").params as { image: string };
    expect(sent.image.length).toBeLessThanOrEqual(MAX_IMAGE_BASE64_CHARS);
    expect(ctx.crops.length).toBeGreaterThan(1);
    expect(ctx.crops[0]).toMatchObject({ width: 1600, height: 800 });
    expect(ctx.crops.every((c) => c.width <= 1600 && c.rect.w === 1600)).toBe(true);
    ctx.client.last("recognize").resolve({ ok: true, result: recognizeResult() });
    await settle();
    expect(ctx.root.querySelector(".picture-panel")!.textContent).toMatch(/reduced to \d+ % before reading/);
  });

  it.each([
    [1600, 400, true],
    [800, 700, false],
  ])("shows a %i x %i picture above the steps (%s) or beside them, never larger than it is", async (width, height, wide) => {
    expect(isWidePicture(width, height)).toBe(wide);
    expect(isWidePicture(0, 0)).toBe(false);
    const ctx = await opened();
    button(ctx.root, "Whole picture").click();
    button(ctx.root, "Recognise").click();
    await settle();
    ctx.client.last("recognize").resolve({ ok: true, result: { ...recognizeResult(), width, height } });
    await settle();
    expect(ctx.root.querySelector(".result-body")!.classList.contains("wide")).toBe(wide);
    const frame = ctx.root.querySelector<HTMLElement>(".picture")!;
    expect(frame.style.getPropertyValue("--natural")).toBe(`${width}px`);
    expect(Number(frame.style.getPropertyValue("--ratio"))).toBeCloseTo(width / height, 2);
  });

  it("counts the seconds while waiting and explains the first run", async () => {
    const ctx = await opened();
    button(ctx.root, "Whole picture").click();
    button(ctx.root, "Recognise").click();
    await settle();
    expect(visible(ctx.root, ".elapsed")).toBe(true);
    expect(visible(ctx.root, ".stage")).toBe(false);
    expect(ctx.root.querySelector(".elapsed")!.textContent).toBe("0 s");
    ctx.clock.fire(1000, 7000);
    expect(ctx.root.querySelector(".elapsed")!.textContent).toBe("7 s");
    expect(ctx.root.textContent).toContain("loads its models");
    // No status questions while the host is busy with the recognition.
    ctx.clock.fire(2000);
    expect(ctx.client.callsOf("recognition_status")).toHaveLength(1);
    ctx.client.last("recognize").resolve({ ok: true, result: recognizeResult() });
    await settle();
    expect(visible(ctx.root, ".elapsed")).toBe(false);
  });

  it("goes back to the selection with the reason when the recognition fails, and asks for the state again", async () => {
    const ctx = await opened();
    button(ctx.root, "Whole picture").click();
    button(ctx.root, "Recognise").click();
    await settle();
    ctx.client.last("recognize").resolve({ ok: false, error: { code: "recognition_failed", message: "ChemResolver stopped unexpectedly." } });
    await settle();
    expect(visible(ctx.root, ".stage")).toBe(true);
    expect(ctx.root.textContent).toContain("ChemResolver stopped unexpectedly.");
    expect(ctx.client.callsOf("recognition_status")).toHaveLength(2);
    expect(ctx.root.querySelector<HTMLElement>(".sel-box")!.hidden).toBe(false); // the selection is kept
  });

  it("New selection returns to the same screenshot without fetching it again", async () => {
    const ctx = await recognised();
    button(ctx.root, "New selection").click();
    expect(visible(ctx.root, ".stage")).toBe(true);
    expect(visible(ctx.root, ".result-body")).toBe(false);
    expect(ctx.client.imageCalls).toHaveLength(1);
    dragBox(ctx, [100, 50], [500, 250]);
    button(ctx.root, "Recognise").click();
    await settle();
    expect(ctx.client.callsOf("recognize")).toHaveLength(2);
  });
});

describe("capture page: the result", () => {
  it("shows the cut-out picture with a numbered box for every molecule, placed in percent", async () => {
    const ctx = await recognised();
    const picture = ctx.root.querySelector<HTMLImageElement>("img.picture-img")!;
    expect(picture.getAttribute("src")).toBe("data:image/png;base64,Q1JPUA==");
    const boxes = Array.from(ctx.root.querySelectorAll<HTMLElement>(".mol-box"));
    expect(boxes.map((b) => b.textContent)).toEqual(["1", "2", "3", "4", "5", "6"]);
    // Molecule 1: box [100, 10, 180, 90] of an 800 x 400 picture.
    expect([boxes[0]!.style.left, boxes[0]!.style.top, boxes[0]!.style.width, boxes[0]!.style.height]).toEqual(["12.5%", "2.5%", "10%", "20%"]);
  });

  it("says it is a draft and shows the result's warnings in words", async () => {
    const ctx = await recognised();
    expect(ctx.root.querySelector(".result-head")!.textContent).toContain("This is a draft.");
    expect(ctx.root.querySelector(".result-head")!.textContent).toContain("belong to no reaction step");
  });

  it("lists every step with its molecules by role, and the molecule in no reaction", async () => {
    const ctx = await recognised();
    const steps = stepSections(ctx.root);
    expect(steps.map((s) => s.querySelector("h3")!.textContent)).toEqual(["Step 1", "Step 2"]);
    const numbersIn = (section: HTMLElement, role: string) =>
      Array.from(section.querySelectorAll(`.role-${role} li.card .num`)).map((n) => n.textContent);
    expect(numbersIn(steps[0]!, "reactant")).toEqual(["1", "2"]);
    expect(numbersIn(steps[0]!, "agent")).toEqual(["4"]);
    expect(numbersIn(steps[0]!, "product")).toEqual(["3"]);
    expect(numbersIn(steps[1]!, "reactant")).toEqual(["3"]);
    expect(steps[1]!.querySelector(".role-agent")!.textContent).toContain("None");
    const unused = ctx.root.querySelector<HTMLElement>("section.step.unused")!;
    expect(unused.querySelector("h3")!.textContent).toBe("Not used in a reaction");
    expect(cards(unused).map((c) => c.querySelector(".num")!.textContent)).toEqual(["6"]);
  });

  it("a card shows the redrawn structure, the SMILES, the confidence and the warnings as sentences", async () => {
    const ctx = await recognised();
    const card = cardOf(ctx.root, 2);
    const img = card.querySelector<HTMLImageElement>("img.structure")!;
    expect(img.getAttribute("src")).toMatch(/^data:image\/svg\+xml;charset=utf-8,/);
    expect(img.getAttribute("alt")).toBe("Structure of molecule 2 as the program read it");
    expect(card.querySelector("code.smiles")!.textContent).toBe("CCCO");
    expect(card.querySelector(".tag")!.textContent).toBe("Program's own score: 0.61");
    expect(Array.from(card.querySelectorAll(".warnings li")).map((li) => li.textContent)).toEqual([
      "Has stereochemistry - please check the wedges and the double-bond geometry (E/Z)",
      "The program is not sure about this one - check it carefully",
    ]);
    expect(select(card, "role").value).toBe("reactant");
    expect(select(card, "step").value).toBe("0");
    expect(cardOf(ctx.root, 1).querySelector(".warnings")).toBeNull();
    const unreadable = cardOf(ctx.root, 6);
    expect(unreadable.querySelector("img.structure")).toBeNull();
    expect(unreadable.textContent).toContain("No structure");
    expect(unreadable.textContent).toContain("No SMILES");
    expect(unreadable.textContent).toContain("Could not be read as a structure");
    expect(select(unreadable, "role").value).toBe("unused");
  });

  it("shows the reaction SMILES of each step and the condition text in an editable box with the note", async () => {
    const ctx = await recognised();
    const steps = stepSections(ctx.root);
    expect(rxn(steps[0]!)).toBe("CCO.CCCO>CCCCCO>CCCCO");
    expect(rxn(steps[1]!)).toBe("CCCCO>>*CCO");
    const text = steps[0]!.querySelector<HTMLTextAreaElement>("textarea.conditions")!;
    expect(text.value).toBe("Pd/C\nH2\nEtOH, 20 C");
    expect(steps[0]!.textContent).toContain("Formulas of reagents are often misread");
    expect(document.getElementById(text.getAttribute("aria-describedby")!)!.textContent).toContain("often misread");
  });

  it("changing a role moves the card and recomputes the reaction SMILES", async () => {
    const ctx = await recognised();
    choose(select(cardOf(ctx.root, 4), "role"), "reactant");
    const step = stepSections(ctx.root)[0]!;
    expect(rxn(step)).toBe("CCO.CCCO.CCCCCO>>CCCCO");
    expect(step.querySelector(".role-agent")!.textContent).toContain("None");
    expect(select(cardOf(ctx.root, 4), "role").value).toBe("reactant");
  });

  it("Not used takes a molecule out of the step but keeps it on the page", async () => {
    const ctx = await recognised();
    choose(select(cardOf(ctx.root, 2), "role"), "unused");
    expect(rxn(stepSections(ctx.root)[0]!)).toBe("CCO>CCCCCO>CCCCO");
    const unused = ctx.root.querySelector<HTMLElement>("section.step.unused")!;
    expect(cards(unused).map((c) => c.querySelector(".num")!.textContent)).toEqual(["2", "6"]);
  });

  it("changing the step moves the molecule, also to a new step", async () => {
    const ctx = await recognised();
    choose(select(cardOf(ctx.root, 4), "step"), "1");
    expect(rxn(stepSections(ctx.root)[0]!)).toBe("CCO.CCCO>>CCCCO");
    expect(rxn(stepSections(ctx.root)[1]!)).toBe("CCCCO>CCCCCO>*CCO");
    choose(select(cardOf(ctx.root, 5), "step"), "new");
    expect(stepSections(ctx.root)).toHaveLength(3);
    expect(rxn(stepSections(ctx.root)[2]!)).toBe(">>*CCO");
  });

  it("an unused molecule gets a role in the step chosen for it", async () => {
    const ctx = await recognised();
    choose(select(cardOf(ctx.root, 6), "step"), "1");
    expect(select(cardOf(ctx.root, 6), "step").value).toBe("1"); // remembered across the redraw
    choose(select(cardOf(ctx.root, 6), "role"), "agent");
    expect(ctx.root.querySelector("section.step.unused")).toBeNull();
    const second = stepSections(ctx.root)[1]!;
    expect(Array.from(second.querySelectorAll(".role-agent .num")).map((n) => n.textContent)).toEqual(["6"]);
    expect(rxn(second)).toBe("CCCCO>>*CCO"); // unreadable over the arrow: left out
  });

  it("without a readable reactant the reaction SMILES is not available, and the page says why", async () => {
    const ctx = await recognised();
    choose(select(cardOf(ctx.root, 6), "role"), "reactant");
    const step = stepSections(ctx.root)[0]!;
    expect(rxn(step)).toBeNull();
    expect(step.querySelector(".rxn-line")!.textContent).toContain("Not available");
  });

  it("steps can be added and removed", async () => {
    const ctx = await recognised();
    button(ctx.root, "Add step").click();
    expect(stepSections(ctx.root)).toHaveLength(3);
    button(stepSections(ctx.root)[0]!, "Remove this step").click();
    expect(stepSections(ctx.root)).toHaveLength(2);
    const unused = ctx.root.querySelector<HTMLElement>("section.step.unused")!;
    expect(cards(unused).map((c) => c.querySelector(".num")!.textContent)).toEqual(["1", "2", "4", "6"]);
  });

  it("Remove deletes a molecule from the result, its box included", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 3), "Remove molecule 3 from the result").click();
    expect(cards(ctx.root).some((c) => c.querySelector(".num")!.textContent === "3")).toBe(false);
    expect(Array.from(ctx.root.querySelectorAll(".mol-box")).map((b) => b.textContent)).toEqual(["1", "2", "4", "5", "6"]);
    expect(rxn(stepSections(ctx.root)[0]!)).toBe("CCO.CCCO>CCCCCO>");
  });

  it("single molecule: no steps, the molecules are simply listed", async () => {
    const result = recognizeResult({ reactions: [], unassigned: [1, 2, 3, 4, 5, 6], warnings: [] });
    const ctx = await recognised(result);
    expect(stepSections(ctx.root)).toEqual([]);
    expect(ctx.root.querySelector("section.step.unused h3")!.textContent).toBe("Molecules");
    expect(cards(ctx.root)).toHaveLength(6);
    // A role still builds a reaction: it goes to a new step.
    expect(select(cardOf(ctx.root, 1), "step").value).toBe("new");
    choose(select(cardOf(ctx.root, 1), "role"), "product");
    expect(stepSections(ctx.root)).toHaveLength(1);
    expect(stepSections(ctx.root)[0]!.querySelector("h3")!.textContent).toBe("Reaction");
  });

  it("nothing found: says so and still offers a new selection", async () => {
    const ctx = await recognised(recognizeResult({ molecules: [], reactions: [], unassigned: [], warnings: ["no_molecules"] }));
    expect(ctx.root.querySelector(".result-head")!.textContent).toContain("No molecule was found");
    expect(cards(ctx.root)).toEqual([]);
    expect(button(ctx.root, "New selection")).toBeTruthy();
  });

  it("hovering or focusing a card marks its box on the picture", async () => {
    const ctx = await recognised();
    const card = cardOf(ctx.root, 4);
    const box = Array.from(ctx.root.querySelectorAll<HTMLElement>(".mol-box")).find((b) => b.textContent === "4")!;
    card.dispatchEvent(new MouseEvent("mouseenter"));
    expect(box.classList.contains("hot")).toBe(true);
    card.dispatchEvent(new MouseEvent("mouseleave"));
    expect(box.classList.contains("hot")).toBe(false);
  });

  it("keeps the keyboard focus on the control that was used when the result is redrawn", async () => {
    const ctx = await recognised();
    const role = select(cardOf(ctx.root, 4), "role");
    role.focus();
    choose(role, "reactant");
    const again = select(cardOf(ctx.root, 4), "role");
    expect(again).not.toBe(role);
    expect(document.activeElement).toBe(again);
  });

  it("shows everything read from the picture as text, never as markup", async () => {
    const result = recognizeResult();
    result.molecules[0]!.smiles = EVIL;
    result.reactions[0]!.conditions[0]!.lines = [EVIL];
    const ctx = await recognised(result);
    expect(cardOf(ctx.root, 1).querySelector("code.smiles")!.textContent).toBe(EVIL);
    expect(ctx.root.querySelectorAll("script")).toHaveLength(0);
    expect(ctx.root.querySelectorAll("img:not(.structure):not(.picture-img):not(.shot)")).toHaveLength(0);
    expect(stepSections(ctx.root)[0]!.querySelector<HTMLTextAreaElement>("textarea")!.value).toContain(EVIL);
    expect((window as unknown as { __pwned?: unknown }).__pwned).toBeUndefined();
    // Structures are only ever images.
    for (const img of ctx.root.querySelectorAll<HTMLImageElement>("img.structure")) expect(img.getAttribute("src")).toMatch(/^data:image\/svg\+xml/);
  });
});

describe("capture page: copying", () => {
  it("copies one SMILES, a reaction SMILES and the whole result", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 1), "Copy SMILES of molecule 1").click();
    expect(ctx.copyText).toHaveBeenLastCalledWith("CCO");
    button(stepSections(ctx.root)[1]!, "Copy reaction SMILES of step 2").click();
    expect(ctx.copyText).toHaveBeenLastCalledWith("CCCCO>>*CCO");
    await settle();
    expect(ctx.root.querySelector('[role="status"].sr-only')!.textContent).toContain("copied");
    button(ctx.root, "Copy all").click();
    const all = ctx.copyText.mock.calls.at(-1)![0];
    expect(all).toContain("Step 1\nReaction SMILES: CCO.CCCO>CCCCCO>CCCCO");
    expect(all).toContain("Conditions: Pd/C; H2; EtOH, 20 C");
    expect(all).toContain("Molecule (6): no SMILES");
  });

  it("Copy all uses the text as the user changed it", async () => {
    const ctx = await recognised();
    const text = stepSections(ctx.root)[0]!.querySelector<HTMLTextAreaElement>("textarea.conditions")!;
    text.value = "Pd/C, H2\nEtOH";
    text.dispatchEvent(new Event("input", { bubbles: true }));
    choose(select(cardOf(ctx.root, 4), "role"), "unused"); // a redraw keeps the edited text
    expect(stepSections(ctx.root)[0]!.querySelector<HTMLTextAreaElement>("textarea.conditions")!.value).toBe("Pd/C, H2\nEtOH");
    button(ctx.root, "Copy all").click();
    expect(ctx.copyText.mock.calls.at(-1)![0]).toContain("Conditions: Pd/C, H2; EtOH");
  });
});

describe("capture page: editing a molecule", () => {
  const dialog = (root: HTMLElement) => root.querySelector<HTMLElement>('[role="dialog"]')!;
  const dialogOpen = (root: HTMLElement) => dialog(root).closest("[hidden]") === null;

  it("Edit opens the editor with the molecule in it", async () => {
    const ctx = await recognised();
    expect(dialogOpen(ctx.root)).toBe(false);
    button(cardOf(ctx.root, 2), "Edit molecule 2").click();
    expect(dialogOpen(ctx.root)).toBe(true);
    expect(dialog(ctx.root).getAttribute("aria-modal")).toBe("true");
    expect(dialog(ctx.root).querySelector("h2")!.textContent).toBe("Edit molecule 2");
    expect(ctx.editor.set).toEqual([MOLFILE_A.replace("invented", "invented 2")]);
    expect(document.activeElement).toBe(dialog(ctx.root).querySelector("h2"));
    expect(document.body.classList.contains("modal-open")).toBe(true);
  });

  it("Save asks the program for the SMILES and warnings of the drawing, then shows the molecule as edited by you", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 2), "Edit molecule 2").click();
    ctx.editor.molfile = MOLFILE_B;
    button(dialog(ctx.root), "Save").click();
    expect(ctx.client.last("describe_structures").params).toEqual({ structures: [{ molfile: MOLFILE_B }] });
    expect(dialogOpen(ctx.root)).toBe(true); // until the answer is there
    ctx.client.last("describe_structures").resolve({ ok: true, result: { structures: [{ smiles: "CN", warnings: ["charged_or_salt"] }] } });
    await settle();
    expect(dialogOpen(ctx.root)).toBe(false);
    expect(document.body.classList.contains("modal-open")).toBe(false);
    const card = cardOf(ctx.root, 2);
    expect(card.querySelector(".tag")!.textContent).toBe("edited by you");
    expect(card.textContent).not.toContain("score");
    expect(card.querySelector("code.smiles")!.textContent).toBe("CN");
    expect(Array.from(card.querySelectorAll(".warnings li")).map((li) => li.textContent)).toEqual(["Salt or charged - check the counter-ion"]);
    expect(rxn(stepSections(ctx.root)[0]!)).toBe("CCO.CN>CCCCCO>CCCCO");
    expect(ctx.rendered.at(-1)).not.toBeUndefined();
    expect(ctx.rendered).toContain(MOLFILE_B);
    expect(document.activeElement).toBe(button(card, "Edit molecule 2"));
    // The other molecules keep the program's confidence.
    expect(cardOf(ctx.root, 1).querySelector(".tag")!.textContent).toBe("Program's own score: 0.90");
  });

  it("an edit that makes two molecules equal marks both as duplicates", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 2), "Edit molecule 2").click();
    ctx.editor.molfile = MOLFILE_B;
    button(dialog(ctx.root), "Save").click();
    ctx.client.last("describe_structures").resolve({ ok: true, result: { structures: [{ smiles: "CCO", warnings: [] }] } });
    await settle();
    expect(cardOf(ctx.root, 1).textContent).toContain("The same structure appears more than once");
    expect(cardOf(ctx.root, 2).textContent).toContain("The same structure appears more than once");
  });

  it("keeps the editor open with the reason when the program cannot describe the drawing", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 1), "Edit molecule 1").click();
    ctx.editor.molfile = MOLFILE_B;
    button(dialog(ctx.root), "Save").click();
    ctx.client.last("describe_structures").resolve({ ok: false, error: { code: "host_unavailable", message: "The local registry program could not be reached." } });
    await settle();
    expect(dialogOpen(ctx.root)).toBe(true);
    expect(dialog(ctx.root).querySelector('[role="alert"]')!.textContent).toContain("could not be reached");
    expect(cardOf(ctx.root, 1).querySelector(".tag")!.textContent).toBe("Program's own score: 0.90");
  });

  it("does not save an empty drawing", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 1), "Edit molecule 1").click();
    ctx.editor.molfile = null;
    button(dialog(ctx.root), "Save").click();
    expect(ctx.client.callsOf("describe_structures")).toEqual([]);
    expect(dialog(ctx.root).textContent).toContain("Nothing is drawn");
  });

  it("Cancel and Esc close the editor and change nothing", async () => {
    const ctx = await recognised();
    const edit = button(cardOf(ctx.root, 1), "Edit molecule 1");
    edit.focus();
    edit.click();
    ctx.editor.molfile = MOLFILE_B;
    button(dialog(ctx.root), "Cancel").click();
    expect(dialogOpen(ctx.root)).toBe(false);
    expect(document.activeElement).toBe(edit);
    edit.click();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(dialogOpen(ctx.root)).toBe(false);
    expect(ctx.client.callsOf("describe_structures")).toEqual([]);
    expect(cardOf(ctx.root, 1).querySelector("code.smiles")!.textContent).toBe("CCO");
  });

  it("Add molecule opens an empty editor and adds the drawing as a new, unused molecule", async () => {
    const ctx = await recognised();
    button(ctx.root, "Add molecule").click();
    expect(dialog(ctx.root).querySelector("h2")!.textContent).toBe("Add a molecule");
    expect(ctx.editor.set).toEqual([]);
    expect(ctx.editor.cleared).toBeGreaterThan(0);
    ctx.editor.molfile = MOLFILE_B;
    button(dialog(ctx.root), "Save").click();
    ctx.client.last("describe_structures").resolve({ ok: true, result: { structures: [{ smiles: "CN", warnings: [] }] } });
    await settle();
    const unused = ctx.root.querySelector<HTMLElement>("section.step.unused")!;
    expect(cards(unused).map((c) => c.querySelector(".num")!.textContent)).toEqual(["6", "7"]);
    expect(cardOf(ctx.root, 7).querySelector(".tag")!.textContent).toBe("edited by you");
    // It has no box on the picture.
    expect(Array.from(ctx.root.querySelectorAll(".mol-box")).map((b) => b.textContent)).toEqual(["1", "2", "3", "4", "5", "6"]);
  });

  it("a molecule the editor cannot show can still be drawn from scratch", async () => {
    const ctx = await recognised();
    ctx.editor.failSet = true;
    button(cardOf(ctx.root, 1), "Edit molecule 1").click();
    expect(dialogOpen(ctx.root)).toBe(true);
    expect(dialog(ctx.root).textContent).toContain("Draw the molecule from scratch");
    expect(button(dialog(ctx.root), "Save").disabled).toBe(false);
  });

  it("says so when the editor cannot be started", async () => {
    const ctx = await recognised(recognizeResult(), { failEditor: true });
    button(cardOf(ctx.root, 1), "Edit molecule 1").click();
    expect(dialog(ctx.root).textContent).toContain("The structure editor could not be started");
    expect(button(dialog(ctx.root), "Save").disabled).toBe(true);
    button(dialog(ctx.root), "Cancel").click();
    expect(dialogOpen(ctx.root)).toBe(false);
  });
});

describe("capture page: Open in ChemDraw", () => {
  it("sends the current, edited state", async () => {
    const ctx = await recognised();
    choose(select(cardOf(ctx.root, 4), "role"), "unused");
    const text = stepSections(ctx.root)[0]!.querySelector<HTMLTextAreaElement>("textarea.conditions")!;
    text.value = "Pd/C, H2\n\n EtOH ";
    text.dispatchEvent(new Event("input", { bubbles: true }));
    button(cardOf(ctx.root, 1), "Edit molecule 1").click();
    ctx.editor.molfile = MOLFILE_B;
    button(ctx.root.querySelector<HTMLElement>('[role="dialog"]')!, "Save").click();
    ctx.client.last("describe_structures").resolve({ ok: true, result: { structures: [{ smiles: "CN", warnings: [] }] } });
    await settle();

    button(ctx.root, "Open in ChemDraw").click();
    const molfile = (n: number) => MOLFILE_A.replace("invented", `invented ${n}`);
    expect(ctx.client.last("reaction_to_chemdraw").params).toEqual({
      steps: [
        { reactants: [MOLFILE_B, molfile(2)], agents: [], products: [molfile(3)], conditions: ["Pd/C, H2", "EtOH"] },
        { reactants: [molfile(3)], agents: [], products: [molfile(5)], conditions: [] },
      ],
    });
    expect(ctx.root.querySelector(".result-head")!.textContent).toContain("Writing the ChemDraw file");
    ctx.client.last("reaction_to_chemdraw").resolve({ ok: true, result: { path: "C:\\Temp\\reaction.cdxml", opened: true } });
    await settle();
    expect(ctx.root.querySelector(".result-head")!.textContent).toContain("Opened in ChemDraw. The file is C:\\Temp\\reaction.cdxml");
  });

  it("says where the file is and why it was not opened", async () => {
    const ctx = await recognised();
    button(ctx.root, "Open in ChemDraw").click();
    ctx.client.last("reaction_to_chemdraw").resolve({ ok: true, result: { path: "/tmp/reaction.cdxml", opened: false, message: "No program is set up for ChemDraw files." } });
    await settle();
    const head = ctx.root.querySelector(".result-head")!.textContent!;
    expect(head).toContain("written but not opened: No program is set up for ChemDraw files.");
    expect(head).toContain("/tmp/reaction.cdxml");
  });

  it("shows the host's error", async () => {
    const ctx = await recognised();
    button(ctx.root, "Open in ChemDraw").click();
    ctx.client.last("reaction_to_chemdraw").resolve({ ok: false, error: { code: "bad_request", message: "A structure could not be read." } });
    await settle();
    expect(ctx.root.querySelector('.result-head [role="alert"]')!.textContent).toBe("A structure could not be read.");
    expect(button(ctx.root, "Open in ChemDraw").disabled).toBe(false);
  });

  it("sends nothing when a used molecule has no structure, and names it", async () => {
    const ctx = await recognised();
    choose(select(cardOf(ctx.root, 6), "role"), "reactant");
    button(ctx.root, "Open in ChemDraw").click();
    expect(ctx.client.callsOf("reaction_to_chemdraw")).toEqual([]);
    expect(ctx.root.querySelector('.result-head [role="alert"]')!.textContent).toContain("Molecule 6 has no structure");
  });

  it("does not send twice while the first request is under way", async () => {
    const ctx = await recognised();
    button(ctx.root, "Open in ChemDraw").click();
    expect(button(ctx.root, "Opening…").disabled).toBe(true);
    button(ctx.root, "Opening…").click();
    expect(ctx.client.callsOf("reaction_to_chemdraw")).toHaveLength(1);
  });
});

describe("capture page: what it sends", () => {
  it("sends only the four capture operations, and nothing after it was closed", async () => {
    const ctx = await recognised();
    button(ctx.root, "Open in ChemDraw").click();
    button(ctx.root, "Add molecule").click();
    ctx.editor.molfile = MOLFILE_B;
    button(ctx.root.querySelector<HTMLElement>('[role="dialog"]')!, "Save").click();
    expect([...new Set(ctx.client.calls.map((c) => c.op))].sort()).toEqual(
      ["describe_structures", "reaction_to_chemdraw", "recognition_status", "recognize"],
    );
    const before = ctx.client.calls.length;
    handle!.destroy();
    handle = null;
    expect(ctx.clock.count()).toBe(0);
    ctx.clock.fire(2000);
    expect(ctx.client.calls).toHaveLength(before);
    expect(ctx.root.childElementCount).toBe(0);
  });
});

describe("capture page: found in review", () => {
  const dialog = (root: HTMLElement) => root.querySelector<HTMLElement>('[role="dialog"]')!;
  const dialogOpen = (root: HTMLElement) => dialog(root).closest("[hidden]") === null;
  const statusBox = (root: HTMLElement) => root.querySelector<HTMLElement>(".status-box")!.textContent ?? "";
  const head = (root: HTMLElement) => root.querySelector<HTMLElement>(".result-head")!.textContent ?? "";
  const escape = () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
  const EMPTY = recognizeResult({ molecules: [], reactions: [], unassigned: [], warnings: ["no_molecules"] });

  it("does not start the recognition program for a screenshot that is gone", async () => {
    const ctx = mount();
    await settle();
    expect(ctx.client.calls).toEqual([]); // not before the picture is there
    ctx.client.imageCalls[0]!.resolve({ ok: false, error: { code: "capture_expired", message: "This screenshot is no longer kept." } });
    await settle();
    ctx.clock.fire(2000);
    expect(ctx.root.textContent).toContain("The screenshot is no longer available");
    expect(ctx.client.calls).toEqual([]);
  });

  it("explains why the keyboard shortcut took no picture, from the fixed word in its address", async () => {
    const ctx = mount({ captureId: null, failed: "not_capturable" });
    await settle();
    expect(ctx.root.textContent).toContain("The tab was not captured");
    expect(ctx.root.textContent).toContain("This tab could not be captured.");
    expect(ctx.root.textContent).toContain("choose Capture reaction instead");
    expect(ctx.root.textContent).not.toContain("opened by hand");
    expect(ctx.client.imageCalls).toEqual([]);
    expect(ctx.client.calls).toEqual([]);
  });

  it("explains a private tab", async () => {
    const ctx = mount({ captureId: null, failed: "private_window" });
    await settle();
    expect(ctx.root.textContent).toContain("private (Incognito)");
    expect(ctx.root.textContent).toContain("normal window");
  });

  it.each([EVIL, "", "constructor", "toString", "something_else"])("shows no text for the unknown reason %j", async (failed) => {
    const ctx = mount({ captureId: null, failed });
    await settle();
    expect(ctx.root.textContent).toContain("No screenshot");
    expect(ctx.root.textContent).toContain("opened by hand");
    expect(ctx.root.textContent).not.toContain(EVIL);
    expect(ctx.client.calls).toEqual([]);
  });

  it("says that the program may be busy when the first status answer takes long", async () => {
    const ctx = mount();
    await settle();
    ctx.client.imageCalls[0]!.resolve({ ok: true, result: { image: SHOT } });
    await settle();
    expect(statusBox(ctx.root)).toContain("Asking the program on this PC");
    ctx.clock.fire(2000, 2000);
    expect(statusBox(ctx.root)).not.toContain("Still waiting");
    ctx.clock.fire(2000, 4000);
    expect(statusBox(ctx.root)).toContain("Still waiting for the program on this PC");
    expect(statusBox(ctx.root)).toContain("another Capture reaction tab");
    expect(ctx.client.callsOf("recognition_status")).toHaveLength(1); // no second question on top
    ctx.client.last("recognition_status").resolve({ ok: true, result: READY });
    await settle();
    expect(statusBox(ctx.root)).toContain("ChemResolver is ready.");
  });

  it("a recognition that worked replaces an earlier status error, and a status time-out does not talk about the selection", async () => {
    const ctx = mount();
    await settle();
    ctx.client.imageCalls[0]!.resolve({ ok: true, result: { image: SHOT } });
    await settle();
    ctx.client.last("recognition_status").resolve({
      ok: false,
      error: { code: "host_timeout", message: "The local registry program did not answer within 30 seconds." },
    });
    await settle();
    expect(statusBox(ctx.root)).toContain("did not answer within 30 seconds");
    expect(statusBox(ctx.root)).not.toContain("smaller selection");
    expect(statusBox(ctx.root)).not.toContain("took too long");
    button(ctx.root, "Whole picture").click();
    button(ctx.root, "Recognise").click();
    await settle();
    ctx.client.last("recognize").resolve({ ok: true, result: recognizeResult() });
    await settle();
    expect(statusBox(ctx.root)).toContain("ChemResolver is ready.");
    button(ctx.root, "New selection").click();
    expect(statusBox(ctx.root)).toContain("ChemResolver is ready.");
    expect(statusBox(ctx.root)).not.toContain("Asking the program");
  });

  it("a recognize time-out does suggest a smaller selection", async () => {
    const ctx = await opened();
    button(ctx.root, "Whole picture").click();
    button(ctx.root, "Recognise").click();
    await settle();
    ctx.client.last("recognize").resolve({ ok: false, error: { code: "host_timeout", message: "No answer within 300 seconds." } });
    await settle();
    expect(ctx.root.textContent).toContain("Try a smaller selection.");
  });

  it("keeps the condition text out of the session file of Firefox", async () => {
    const ctx = await recognised();
    const boxes = Array.from(ctx.root.querySelectorAll("textarea"));
    expect(boxes).toHaveLength(2);
    for (const box of boxes) expect(box.getAttribute("autocomplete")).toBe("off");
  });

  it("Save without a change leaves the molecule as the program read it", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 2), "Edit molecule 2").click();
    button(dialog(ctx.root), "Save").click();
    // The host gives the unchanged drawing the SMILES the molecule already has.
    ctx.client.last("describe_structures").resolve({ ok: true, result: { structures: [{ smiles: "CCCO", warnings: ["stereo"] }] } });
    await settle();
    expect(dialogOpen(ctx.root)).toBe(false);
    const card = cardOf(ctx.root, 2);
    expect(card.textContent).not.toContain("edited by you");
    expect(card.querySelector(".tag")!.textContent).toBe("Program's own score: 0.61");
    expect(Array.from(card.querySelectorAll(".warnings li")).map((li) => li.textContent)).toEqual([
      "Has stereochemistry - please check the wedges and the double-bond geometry (E/Z)",
      "The program is not sure about this one - check it carefully",
    ]);
    expect(card.querySelector<HTMLImageElement>("img.structure")!.getAttribute("alt")).toBe("Structure of molecule 2 as the program read it");
    expect(ctx.root.querySelector(".sr-only[role=status]")!.textContent).toContain("nothing was changed");
  });

  it("an edited molecule's drawing is described as the user's", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 2), "Edit molecule 2").click();
    ctx.editor.molfile = MOLFILE_B;
    button(dialog(ctx.root), "Save").click();
    ctx.client.last("describe_structures").resolve({ ok: true, result: { structures: [{ smiles: "CN", warnings: [] }] } });
    await settle();
    expect(cardOf(ctx.root, 2).querySelector<HTMLImageElement>("img.structure")!.getAttribute("alt")).toBe("Structure of molecule 2 as you drew it");
  });

  it("Esc inside the editor's own small dialog does not close the editor", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 1), "Edit molecule 1").click();
    const editorHost = ctx.root.querySelector<HTMLElement>(".editor")!;

    // OpenChemLib's atom dialog, in the document ...
    const own = document.createElement("dialog");
    own.setAttribute("open", "");
    editorHost.append(own);
    escape();
    expect(dialogOpen(ctx.root)).toBe(true);
    own.remove();

    // ... or in the shadow tree of its editor.
    const shadowHost = document.createElement("div");
    const inner = document.createElement("dialog");
    inner.setAttribute("open", "");
    shadowHost.attachShadow({ mode: "open" }).append(inner);
    editorHost.append(shadowHost);
    escape();
    expect(dialogOpen(ctx.root)).toBe(true);
    shadowHost.remove();

    // A key press something else has already handled.
    const handled = (event: Event) => event.preventDefault();
    editorHost.addEventListener("keydown", handled);
    editorHost.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(dialogOpen(ctx.root)).toBe(true);
    editorHost.removeEventListener("keydown", handled);

    // Nothing of that, and nothing drawn: Esc closes.
    escape();
    expect(dialogOpen(ctx.root)).toBe(false);
  });

  it("Esc does not throw a changed drawing away", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 1), "Edit molecule 1").click();
    ctx.editor.molfile = MOLFILE_B; // the user drew something
    escape();
    expect(dialogOpen(ctx.root)).toBe(true);
    expect(dialog(ctx.root).textContent).toContain("Press Cancel to discard the changes, or Save to keep them.");
    button(dialog(ctx.root), "Cancel").click();
    expect(dialogOpen(ctx.root)).toBe(false);

    // The same for a molecule drawn from scratch.
    button(ctx.root, "Add molecule").click();
    escape();
    expect(dialogOpen(ctx.root)).toBe(false); // nothing drawn yet
    button(ctx.root, "Add molecule").click();
    ctx.editor.molfile = MOLFILE_A;
    escape();
    expect(dialogOpen(ctx.root)).toBe(true);
  });

  it("New selection keeps the corrected result within reach, also after a failed second recognition", async () => {
    const ctx = await opened();
    expect(button(ctx.root, "Back to the result").hidden).toBe(true);
    button(ctx.root, "Whole picture").click();
    button(ctx.root, "Recognise").click();
    await settle();
    ctx.client.last("recognize").resolve({ ok: true, result: recognizeResult() });
    await settle();
    button(cardOf(ctx.root, 4), "Remove molecule 4 from the result").click();

    button(ctx.root, "New selection").click();
    const back = button(ctx.root, "Back to the result");
    expect(back.hidden).toBe(false);
    expect(ctx.root.textContent).toContain("is kept, with your corrections");
    button(ctx.root, "Recognise").click();
    await settle();
    ctx.client.last("recognize").resolve({ ok: false, error: { code: "recognition_failed", message: "ChemResolver stopped unexpectedly." } });
    await settle();
    expect(visible(ctx.root, ".stage")).toBe(true);

    back.click();
    expect(visible(ctx.root, ".result-body")).toBe(true);
    expect(visible(ctx.root, ".stage")).toBe(false);
    expect(cards(ctx.root).map((c) => c.querySelector(".num")!.textContent)).not.toContain("4");
    expect(document.activeElement!.textContent).toBe("2. Check the result");
  });

  it("the editor shows the part of the picture the molecule was read from", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 2), "Edit molecule 2").click();
    const part = dialog(ctx.root).querySelector<HTMLImageElement>("img.ref-img")!;
    expect(part.getAttribute("src")).toBe("data:image/png;base64,Q1JPUA=="); // the picture that was read
    expect(part.getAttribute("alt")).toBe("Molecule 2 in the picture");
    // Box [200, 10, 280, 90] of an 800 x 400 picture plus 12 pixels around it: [188, 0, 292, 102].
    expect(parseFloat(part.style.width)).toBeCloseTo((800 / 104) * 100, 3);
    expect(parseFloat(part.style.height)).toBeCloseTo((400 / 102) * 100, 3);
    expect(parseFloat(part.style.left)).toBeCloseTo((-188 / 104) * 100, 3);
    expect(parseFloat(part.style.top)).toBeCloseTo(0, 3);
    button(dialog(ctx.root), "Cancel").click();
    expect(ctx.root.querySelector("img.ref-img")).toBeNull();

    // A molecule drawn from scratch has no box: the whole picture.
    button(ctx.root, "Add molecule").click();
    const whole = dialog(ctx.root).querySelector<HTMLImageElement>("img.ref-img")!;
    expect(whole.getAttribute("alt")).toBe("The picture");
    expect(whole.style.width).toBe("100%");
    expect(whole.style.height).toBe("100%");
    expect(parseFloat(whole.style.left)).toBeCloseTo(0, 6);
  });

  it("Back to the selection leaves a running recognition, and its late answer is not used", async () => {
    const ctx = await opened();
    button(ctx.root, "Whole picture").click();
    button(ctx.root, "Recognise").click();
    await settle();
    expect(document.activeElement!.textContent).toBe("Reading the reaction…");
    button(ctx.root, "Back to the selection").click();
    expect(visible(ctx.root, ".stage")).toBe(true);
    expect(ctx.root.textContent).toContain("The program still finishes reading the earlier picture");
    expect(document.activeElement).toBe(button(ctx.root, "Recognise"));

    button(ctx.root, "Recognise").click();
    await settle();
    const [first, second] = ctx.client.callsOf("recognize");
    expect(second).toBeDefined();
    first!.resolve({ ok: true, result: EMPTY });
    await settle();
    // Still waiting for the second one, and its counter still runs.
    expect(visible(ctx.root, ".elapsed")).toBe(true);
    expect(visible(ctx.root, ".result-body")).toBe(false);
    ctx.clock.fire(1000, 3000);
    expect(ctx.root.querySelector(".elapsed")!.textContent).toBe("3 s");
    second!.resolve({ ok: true, result: recognizeResult() });
    await settle();
    expect(visible(ctx.root, ".result-body")).toBe(true);
    expect(cards(ctx.root).length).toBeGreaterThan(0);
  });

  it("a late answer after Back to the selection changes nothing but the note", async () => {
    const ctx = await opened();
    button(ctx.root, "Whole picture").click();
    button(ctx.root, "Recognise").click();
    await settle();
    button(ctx.root, "Back to the selection").click();
    ctx.client.last("recognize").resolve({ ok: true, result: recognizeResult() });
    await settle();
    expect(visible(ctx.root, ".stage")).toBe(true);
    expect(visible(ctx.root, ".result-body")).toBe(false);
    expect(button(ctx.root, "Back to the result").hidden).toBe(true);
    expect(ctx.root.textContent).not.toContain("still finishes reading the earlier picture");
  });

  it("refuses to move a card onto a step that already uses the molecule, and says so", async () => {
    const ctx = await recognised();
    const step = select(cardOf(ctx.root, 3, 0), "step"); // molecule 3: product of step 1, reactant of step 2
    step.focus();
    choose(step, "1");
    expect(rxn(stepSections(ctx.root)[0]!)).toBe("CCO.CCCO>CCCCCO>CCCCO");
    expect(rxn(stepSections(ctx.root)[1]!)).toBe("CCCCO>>*CCO");
    expect(head(ctx.root)).toContain("Molecule 3 is already in step 2 (reactant)");
    expect(head(ctx.root)).toContain("Nothing was changed.");
    const again = select(cardOf(ctx.root, 3, 0), "step");
    expect(again.value).toBe("0");
    expect(document.activeElement).toBe(again);
    // The note goes with the next change.
    choose(select(cardOf(ctx.root, 4), "role"), "reactant");
    expect(head(ctx.root)).not.toContain("is already in step");
  });

  it("the focus follows a card that moved to another step", async () => {
    const ctx = await recognised();
    const step = select(cardOf(ctx.root, 3, 1), "step"); // the reactant of step 2
    step.focus();
    choose(step, "new");
    expect(stepSections(ctx.root)).toHaveLength(3);
    const moved = select(cardOf(ctx.root, 3, 1), "step");
    expect(moved.value).toBe("2");
    expect(document.activeElement).toBe(moved);
  });

  it("moves the keyboard focus with the view", async () => {
    const ctx = await opened();
    button(ctx.root, "Whole picture").click();
    button(ctx.root, "Recognise").click();
    await settle();
    ctx.client.last("recognize").resolve({ ok: false, error: { code: "recognition_failed", message: "Stopped." } });
    await settle();
    expect(document.activeElement).toBe(button(ctx.root, "Recognise"));
    button(ctx.root, "Recognise").click();
    await settle();
    ctx.client.last("recognize").resolve({ ok: true, result: recognizeResult() });
    await settle();
    expect(document.activeElement!.tagName).toBe("H2");
    expect(document.activeElement!.textContent).toBe("2. Check the result");
  });

  it("names the molecule of a card in text a screen reader reads", async () => {
    const ctx = await recognised();
    const card = cardOf(ctx.root, 1);
    expect(card.querySelector(".card-head")!.textContent).toContain("Molecule 1");
    expect(card.querySelector(".num")!.hasAttribute("aria-label")).toBe(false);
  });

  it("shows no score on a molecule that could not be read, and explains the score elsewhere", async () => {
    const result = recognizeResult();
    result.molecules[5] = { ...result.molecules[5]!, confidence: 0.99 };
    const ctx = await recognised(result);
    expect(cardOf(ctx.root, 6).textContent).toContain("Could not be read as a structure");
    expect(cardOf(ctx.root, 6).querySelector(".tag")).toBeNull();
    const tag = cardOf(ctx.root, 1).querySelector(".tag")!;
    expect(tag.textContent).toBe("Program's own score: 0.90");
    expect(tag.getAttribute("title")).toContain("not the chance that the structure is right");
  });

  it("single molecule: Open in ChemDraw says what to do", async () => {
    const one = recognizeResult({ molecules: [recognizeResult().molecules[0]!], reactions: [], unassigned: [1], warnings: [] });
    const ctx = await recognised(one);
    button(ctx.root, "Open in ChemDraw").click();
    expect(ctx.client.callsOf("reaction_to_chemdraw")).toEqual([]);
    const alert = ctx.root.querySelector('.result-head [role="alert"]')!.textContent!;
    expect(alert).toContain("To open this molecule there, give it the role Reactant or Product first");
    expect(alert).not.toContain("no reaction to draw");
  });

  it("says how many molecules are not in the ChemDraw file", async () => {
    const ctx = await recognised();
    button(ctx.root, "Open in ChemDraw").click();
    ctx.client.last("reaction_to_chemdraw").resolve({ ok: true, result: { path: "C:\\Temp\\reaction.cdxml", opened: true } });
    await settle();
    expect(head(ctx.root)).toContain("One molecule that is in no step was left out.");
  });
});

describe("capture page: whether a drawing was changed", () => {
  const dialog = (root: HTMLElement) => root.querySelector<HTMLElement>('[role="dialog"]')!;
  const dialogOpen = (root: HTMLElement) => dialog(root).closest("[hidden]") === null;
  const escape = () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));

  it("goes by the editor's signature of the structure, not by its molfile (the real editor rescales the coordinates)", async () => {
    const ctx = await recognised();
    let signature = "structure-1";
    (ctx.editor as StructureEditor).getSignature = () => signature;
    button(cardOf(ctx.root, 1), "Edit molecule 1").click();
    // Same structure, other coordinates: not a change, Esc closes.
    ctx.editor.molfile = MOLFILE_A.replace("0.8250", "1.6500");
    escape();
    expect(dialogOpen(ctx.root)).toBe(false);
    // Another structure: Esc keeps the editor.
    button(cardOf(ctx.root, 1), "Edit molecule 1").click();
    signature = "structure-2";
    escape();
    expect(dialogOpen(ctx.root)).toBe(true);
  });

  it("a drawing that cannot be read is never thrown away by Esc", async () => {
    const ctx = await recognised();
    button(cardOf(ctx.root, 1), "Edit molecule 1").click();
    ctx.editor.getMolfile = () => {
      throw new Error("broken drawing");
    };
    escape();
    expect(dialogOpen(ctx.root)).toBe(true);
  });
});
