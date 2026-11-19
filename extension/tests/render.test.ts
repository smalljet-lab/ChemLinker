// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { ManualSource } from "../src/content/manual-source";
import { createManualForm } from "../src/content/ui/manual-form";
import { HOST_TAG, mountPanel, type PanelHandle } from "../src/content/ui/panel";
import type { SourceState } from "../src/shared/adapter";
import { SITE_ORIGIN, type AnalyzeResult, type RegisterParams } from "../src/shared/protocol";
import {
  FakeClient,
  StaticSource,
  analyzeResult,
  batch,
  experiment,
  flush,
  pingResult,
  product,
  registerResult,
} from "./helpers";

const EVIL = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>`;

let handle: PanelHandle | null = null;
afterEach(() => {
  handle?.destroy();
  handle = null;
  document.documentElement.querySelectorAll(HOST_TAG).forEach((n) => n.remove());
});

function readyState(over: Partial<Extract<SourceState, { kind: "ready" }>> = {}): SourceState {
  return { kind: "ready", experiment: experiment(), products: [product()], ...over };
}

function mount(state: SourceState = readyState(), copyText = vi.fn(async () => {})) {
  const client = new FakeClient();
  const source = new StaticSource(state);
  handle = mountPanel({ doc: document, source, client, copyText });
  return { client, source, root: handle.root, handle, copyText };
}

function button(root: ShadowRoot, text: string | RegExp): HTMLButtonElement {
  const all = Array.from(root.querySelectorAll("button"));
  const found = all.find((b) => (typeof text === "string" ? b.textContent === text : text.test(b.textContent ?? "")));
  if (!found) throw new Error(`no button ${text}; have: ${all.map((b) => b.textContent).join(" | ")}`);
  return found;
}

async function lookUpWith(ctx: ReturnType<typeof mount>, result: AnalyzeResult) {
  button(ctx.root, "Look up").click();
  ctx.client.last("analyze").resolve({ ok: true, result });
  await flush();
}

describe("panel shell", () => {
  it("adds one host element with a closed shadow root, collapsed", () => {
    const { handle } = mount();
    expect(document.documentElement.querySelectorAll(HOST_TAG)).toHaveLength(1);
    expect(handle.host.parentNode).toBe(document.documentElement);
    expect(handle.host.shadowRoot).toBeNull(); // closed
    expect(handle.isOpen()).toBe(false);
    const launcher = button(handle.root, /^ID/);
    expect(launcher.getAttribute("aria-expanded")).toBe("false");
    expect(handle.root.querySelectorAll("style")).toHaveLength(1);
    expect(handle.host.style.getPropertyValue("position")).toBe("fixed");
    expect(handle.root.querySelector("[aria-live]")).not.toBeNull();
  });

  it("opens with the launcher, pings once, and closes with the close button", async () => {
    const ctx = mount();
    button(ctx.root, /^ID/).click();
    expect(ctx.handle.isOpen()).toBe(true);
    expect(ctx.client.callsOf("ping")).toHaveLength(1);
    ctx.client.last("ping").resolve({ ok: true, result: pingResult() });
    await flush();
    expect(ctx.root.textContent).toContain("registry.sqlite");
    button(ctx.root, "×").click();
    expect(ctx.handle.isOpen()).toBe(false);
    button(ctx.root, /^ID/).click();
    expect(ctx.client.callsOf("ping")).toHaveLength(1);
  });

  it("shows a non-default registry banner and the ping warnings", async () => {
    const ctx = mount();
    ctx.handle.open();
    ctx.client.last("ping").resolve({
      ok: true,
      result: pingResult({ home_source: "host-config", home: "D:\\Other", warnings: ["No backup exists yet."] }),
    });
    await flush();
    expect(ctx.root.textContent).toContain("Registry: D:\\Other (not the default)");
    expect(ctx.root.textContent).toContain("No backup exists yet.");
  });

  it("explains an unreachable host and retries", async () => {
    const ctx = mount();
    ctx.handle.open();
    ctx.client.last("ping").resolve({ ok: false, error: { code: "host_unavailable", message: "No such native application" } });
    await flush();
    expect(ctx.root.textContent).toContain(
      "Run the installer again (install.ps1 on Windows, install.sh on macOS/Linux), then press Retry.",
    );
    expect(ctx.root.textContent).not.toMatch(/project folder|Rebuild/);
    expect(ctx.root.textContent).toContain("No such native application");
    expect(ctx.root.textContent).toContain("host_unavailable");
    button(ctx.root, "Retry").click();
    expect(ctx.client.callsOf("ping")).toHaveLength(2);
  });

  it("explains a protocol mismatch", async () => {
    const ctx = mount();
    ctx.handle.open();
    ctx.client.last("ping").resolve({ ok: true, result: { protocol_version: 2, host_version: "9.0" } as never });
    await flush();
    expect(ctx.root.textContent).toMatch(/protocol version 2, this extension version 1/);
    expect(ctx.root.textContent).toContain("update.ps1 on Windows, update.sh on macOS/Linux");
    expect(ctx.root.textContent).toContain("reload the extension");
    expect(ctx.root.textContent).toContain("Host version 9.0");
    expect(ctx.root.textContent).not.toMatch(/Rebuild|install\.ps1/);
    expect(button(ctx.root, "Retry")).toBeTruthy();
  });

  it("shows the source's notice and idle message", () => {
    const ctx = mount({ kind: "idle", message: "Paste something." });
    expect(ctx.root.textContent).toContain("Paste something.");
    ctx.source.set(readyState({ experiment: null, notice: "Need a key." }));
    expect(ctx.root.textContent).toContain("Need a key.");
  });
});

describe("product card", () => {
  it("shows the thumbnail only as an <img> with a data URL, plus warnings without any click", async () => {
    const ctx = mount();
    await lookUpWith(
      ctx,
      analyzeResult({
        structure: { warnings: ["Charged structure."] },
        warnings: ["The registry is in a sync folder."],
      }),
    );
    const imgs = Array.from(ctx.root.querySelectorAll("img"));
    expect(imgs).toHaveLength(1);
    expect(imgs[0]!.getAttribute("src")!.startsWith("data:image/svg+xml;charset=utf-8,")).toBe(true);
    expect(ctx.root.querySelector("svg")).toBeNull();
    expect(ctx.root.textContent).toContain("Charged structure.");
    expect(ctx.root.textContent).toContain("The registry is in a sync folder.");
    button(ctx.root, "Advanced").click();
    expect(ctx.root.textContent).toContain("C2H6O");
    expect(ctx.root.textContent).toContain("LFQSCWFLJHTTHZ-UHFFFAOYSA-N");
  });

  it("skips an oversized thumbnail", async () => {
    const ctx = mount();
    await lookUpWith(ctx, analyzeResult({ structure: { svg: `<svg>${"x".repeat(400_000)}</svg>` } }));
    expect(ctx.root.querySelectorAll("img")).toHaveLength(0);
  });

  it("gates Create on the ignored-stereo checkboxes", async () => {
    const ctx = mount();
    await lookUpWith(ctx, analyzeResult({ structure: { ignored_stereo: ["allene axis", "wedge at N5"] } }));
    expect(ctx.root.textContent).toMatch(/would share the same ID/);
    const create = () => button(ctx.root, "Create ID (new compound)");
    expect(create().disabled).toBe(true);
    let boxes = Array.from(ctx.root.querySelectorAll<HTMLInputElement>("input[type=checkbox]"));
    expect(boxes).toHaveLength(2);
    boxes[0]!.click();
    expect(create().disabled).toBe(true);
    boxes = Array.from(ctx.root.querySelectorAll<HTMLInputElement>("input[type=checkbox]"));
    boxes[1]!.click();
    expect(create().disabled).toBe(false);
  });

  it("confirms before registering, then sends register once and shows the ID with Copy", async () => {
    const ctx = mount();
    await lookUpWith(ctx, analyzeResult({ status: "known_compound", core_stereo_id: "000042_01" }));
    button(ctx.root, "Create ID (new batch)").click();
    expect(ctx.client.callsOf("register")).toHaveLength(0);
    const confirm = ctx.root.querySelector(".confirm")!;
    expect(confirm.textContent).toContain("4180");
    expect(confirm.textContent).toContain("ABC-008");
    expect(confirm.textContent).toContain("Absolute (as drawn)");
    expect(confirm.textContent).toMatch(/permanent/);
    const createButton = button(ctx.root, "Create");
    createButton.click();
    createButton.click();
    expect(ctx.client.callsOf("register")).toHaveLength(1);
    expect((ctx.client.last("register").params as RegisterParams).confirm.inchikey).toBe("LFQSCWFLJHTTHZ-UHFFFAOYSA-N");
    ctx.client.last("register").resolve({
      ok: true,
      result: registerResult({
        id: "000042_01_002",
        warnings: ["Backup failed: disk full"],
        backup: { performed: true, path: "C:\\b\\backup-1.sqlite", warning: null, removed: [] },
      }),
    });
    await flush();
    expect(ctx.root.querySelector(".big-id")?.textContent).toBe("000042_01_002");
    expect(ctx.root.textContent).toContain("ID created");
    expect(ctx.root.textContent).toContain("Backup failed: disk full");
    button(ctx.root, "Advanced").click();
    expect(ctx.root.textContent).toContain("backup-1.sqlite");
    expect(Array.from(ctx.root.querySelectorAll("button")).some((b) => /^Create ID/.test(b.textContent ?? ""))).toBe(false);
    button(ctx.root, "Copy").click();
    await flush();
    expect(ctx.copyText).toHaveBeenCalledWith("000042_01_002");
    expect(ctx.root.textContent).toContain("Copied.");
  });

  it("shows a fallback when copying fails", async () => {
    const ctx = mount(readyState(), vi.fn(async () => Promise.reject(new Error("denied"))));
    await lookUpWith(ctx, analyzeResult({ status: "registered_here", id: "000001_01_001" }));
    button(ctx.root, "Copy").click();
    await flush();
    expect(ctx.root.textContent).toMatch(/Ctrl\+C/);
  });

  it("greys a stale analysis and disables Create", async () => {
    const ctx = mount();
    await lookUpWith(ctx, analyzeResult());
    expect(button(ctx.root, "Create ID (new compound)").disabled).toBe(false);
    ctx.source.set(readyState({ products: [product({ changeToken: "t2" })] }));
    expect(ctx.root.querySelector(".analysis.stale")).not.toBeNull();
    expect(button(ctx.root, "Create ID (new compound)").disabled).toBe(true);
    expect(button(ctx.root, "Look up again").disabled).toBe(false);
  });

  it("changing the stereo selector re-analyses with that mode", async () => {
    const ctx = mount();
    await lookUpWith(ctx, analyzeResult());
    button(ctx.root, "Advanced").click();
    const select = ctx.root.querySelector("select")!;
    select.value = "rac";
    select.dispatchEvent(new Event("change"));
    expect(ctx.client.last("analyze").params).toMatchObject({ mode: "rac" });
    expect(ctx.root.querySelector("select")!.disabled).toBe(true);
  });

  it("shows error heading, the host's message and the code", async () => {
    const ctx = mount();
    button(ctx.root, "Look up").click();
    ctx.client.last("analyze").resolve({
      ok: false,
      error: { code: "structure_has_placeholders", message: "Atom 3 is an R-group." },
    });
    await flush();
    const text = ctx.root.textContent!;
    expect(text).toContain("The structure contains placeholders");
    expect(text).toContain("Atom 3 is an R-group.");
    expect(text).toContain("structure_has_placeholders");
  });

  it("explains a slot conflict", async () => {
    const ctx = mount();
    await lookUpWith(ctx, analyzeResult({ slot_conflict: batch({ full_id: "000009_01_004" }) }));
    expect(ctx.root.textContent).toContain("000009_01_004");
    expect(ctx.root.textContent).toMatch(/must be retired/);
    expect(ctx.root.textContent).toContain('compound-registry retire 000009_01_004 --reason "<why>"');
    expect(button(ctx.root, "Create ID (new compound)").disabled).toBe(true);
  });

  it("renders hostile host text as text in every field", async () => {
    (window as unknown as Record<string, unknown>).__pwned = 0;
    const ctx = mount(readyState({ products: [product({ label: EVIL })] }));
    const evilBatch = batch({
      full_id: EVIL,
      core_stereo_id: EVIL,
      experiment_ref: EVIL,
      experiment_code: EVIL,
      experiment_url: `${SITE_ORIGIN}/x"><script>alert(1)</script>`,
      created_at: EVIL,
      retired: true,
      retired_reason: EVIL,
    });
    await lookUpWith(
      ctx,
      analyzeResult({
        status: "registered_here",
        id: EVIL,
        core_stereo_id: EVIL,
        structure: {
          formula: EVIL,
          smiles: EVIL,
          inchikey: EVIL,
          stereo_mode: EVIL as never,
          detected_mode: EVIL as never,
          warnings: [EVIL],
          ignored_stereo: [EVIL],
          svg: `<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>`,
        },
        batches: [evilBatch],
        other_stereos: [
          { core_stereo_id: EVIL, stereo_no: 2, stereo_mode: EVIL as never, smiles: EVIL, inchikey: EVIL, batch_count: 1, svg: EVIL },
        ],
        slot_conflict: { ...evilBatch, product_index: 1 },
        warnings: [EVIL],
      }),
    );
    button(ctx.root, "Advanced").click();
    button(ctx.root, "Look up again").click();
    ctx.client.last("analyze").resolve({ ok: false, error: { code: EVIL, message: EVIL } });
    await flush();

    expect(ctx.root.querySelectorAll("script")).toHaveLength(0);
    for (const img of Array.from(ctx.root.querySelectorAll("img"))) {
      expect(img.getAttribute("src")!.startsWith("data:image/svg+xml;charset=utf-8,")).toBe(true);
      expect(img.getAttributeNames().sort()).toEqual(["alt", "class", "src"]);
    }
    for (const node of Array.from(ctx.root.querySelectorAll("*"))) {
      for (const name of node.getAttributeNames()) expect(name.startsWith("on")).toBe(false);
    }
    const text = ctx.root.textContent ?? "";
    expect(text.split(EVIL).length - 1).toBeGreaterThanOrEqual(10);
    expect((window as unknown as Record<string, unknown>).__pwned).toBe(0);
    const links = Array.from(ctx.root.querySelectorAll("a"));
    for (const a of links) {
      expect(new URL(a.href).origin).toBe(SITE_ORIGIN);
      expect(a.rel).toBe("noopener noreferrer");
    }
  });

  it("links an experiment only on the Sciformation site", async () => {
    const ctx = mount();
    await lookUpWith(
      ctx,
      analyzeResult({
        status: "known_compound",
        batches: [
          batch({ full_id: "A_1", experiment_code: "GOOD", experiment_url: `${SITE_ORIGIN}/experiment/1` }),
          batch({ full_id: "A_2", experiment_code: "JS", experiment_url: "javascript:alert(1)" }),
          batch({ full_id: "A_3", experiment_code: "FOREIGN", experiment_url: "https://evil.example/experiment/1" }),
          batch({ full_id: "A_4", experiment_code: "DATA", experiment_url: "data:text/html,hi" }),
        ],
      }),
    );
    expect(ctx.root.querySelectorAll("a")).toHaveLength(0); // batches are behind Advanced
    button(ctx.root, "Advanced").click();
    const links = Array.from(ctx.root.querySelectorAll("a"));
    expect(links.map((a) => a.textContent)).toEqual(["GOOD"]);
    expect(links[0]!.getAttribute("href")).toBe(`${SITE_ORIGIN}/experiment/1`);
    expect(links[0]!.getAttribute("rel")).toBe("noopener noreferrer");
    for (const code of ["JS", "FOREIGN", "DATA"]) expect(ctx.root.textContent).toContain(code);
  });
});

describe("manual form", () => {
  it("feeds the source; the textarea value becomes the structure and a dropped file is kept verbatim", async () => {
    const source = new ManualSource();
    const client = new FakeClient();
    const page = new StaticSource({ kind: "idle", message: "nothing" }, { autoLookup: true, confirmBeforeCreate: false });
    handle = mountPanel({
      doc: document,
      source: page,
      client,
      manual: { source, form: createManualForm(document, source) },
      copyText: async () => {},
    });
    const root = handle.root;
    // The form is the fallback under "Advanced".
    expect(root.querySelector("textarea")!.closest(".advanced")).not.toBeNull();
    expect(root.textContent).toContain("Experiment key (internal number, not the ELN number)");
    expect(root.textContent).toContain("Experiment code (ELN number, optional)");
    const textarea = root.querySelector("textarea")!;
    textarea.value = "  CCO  ";
    textarea.dispatchEvent(new Event("input"));
    const state = source.getState();
    expect(state.kind === "ready" && state.products[0]!.structure).toEqual({ smiles: "CCO" });
    const key = root.querySelector<HTMLInputElement>('.manual-form input[data-key="experiment-key"]')!;
    key.value = " 4180 ";
    key.dispatchEvent(new Event("input"));
    const s2 = source.getState();
    expect(s2.kind === "ready" && s2.experiment?.ref).toBe("4180");

    const text = "\r\n  header\r\n\r\nM  END\r\n";
    await source.loadFile({ name: "x.mol", size: text.length, text: async () => text });
    const s3 = source.getState();
    expect(s3.kind === "ready" && s3.products[0]!.structure).toEqual({ molfile: text });
    button(root, "Look up").click();
    expect(client.last("analyze").params).toMatchObject({ molfile: text, experiment_ref: "4180", product_index: 1 });
  });
});

describe("registry state in the panel", () => {
  const buttons = (root: ShadowRoot) => Array.from(root.querySelectorAll("button"));
  const enabledCreates = (root: ShadowRoot) =>
    buttons(root).filter((b) => /^Create ID/.test(b.textContent ?? "") && !b.disabled);

  it("shows registry_missing from ping as an error with Retry", async () => {
    const ctx = mount();
    ctx.handle.open();
    ctx.client.last("ping").resolve({
      ok: false,
      error: { code: "registry_missing", message: "D:\\Gone\\registry.sqlite does not exist." },
    });
    await flush();
    expect(ctx.root.textContent).toContain("The registry does not exist");
    expect(ctx.root.textContent).toContain("D:\\Gone\\registry.sqlite does not exist.");
    expect(ctx.root.textContent).toContain("registry_missing");
    button(ctx.root, "Retry").click();
    expect(ctx.client.callsOf("ping")).toHaveLength(2);
  });

  it("shows registry_missing from a lookup with its heading", async () => {
    const ctx = mount();
    button(ctx.root, "Look up").click();
    ctx.client.last("analyze").resolve({ ok: false, error: { code: "registry_missing", message: "No registry here." } });
    await flush();
    expect(ctx.root.textContent).toContain("The registry does not exist");
    expect(ctx.root.textContent).toContain("No registry here.");
  });

  it("pings again and greys every card when any reply says registry_changed", async () => {
    const two = readyState({
      products: [product(), product({ productIndex: 2, label: "Second", changeToken: "t2" })],
    });
    const ctx = mount(two);
    ctx.handle.open();
    ctx.client.last("ping").resolve({ ok: true, result: pingResult({ db_path: "C:\\Old\\registry.sqlite" }) });
    await flush();
    const lookups = buttons(ctx.root).filter((b) => b.textContent === "Look up");
    expect(lookups).toHaveLength(2);
    lookups[0]!.click();
    lookups[1]!.click();
    const [a, b] = ctx.client.callsOf("analyze");
    a!.resolve({ ok: true, result: analyzeResult() });
    await flush();
    expect(enabledCreates(ctx.root)).toHaveLength(1);

    b!.resolve({ ok: false, error: { code: "registry_changed", message: "The registry configuration changed." } });
    await flush();
    expect(ctx.client.callsOf("ping")).toHaveLength(2);
    expect(ctx.root.textContent).toContain("Contacting the local registry program");
    expect(ctx.root.querySelector(".analysis.stale")).not.toBeNull();
    expect(enabledCreates(ctx.root)).toHaveLength(0);

    ctx.client.last("ping").resolve({ ok: true, result: pingResult({ db_path: "C:\\New\\registry.sqlite" }) });
    await flush();
    expect(ctx.root.textContent).toContain("C:\\New\\registry.sqlite");
    expect(ctx.root.textContent).not.toContain("C:\\Old\\registry.sqlite");

    buttons(ctx.root).find((x) => x.textContent === "Look up again")!.click();
    ctx.client.last("analyze").resolve({ ok: true, result: analyzeResult() });
    await flush();
    expect(enabledCreates(ctx.root)).toHaveLength(1);
  });

  it("does not ping in a loop when the automatic ping also answers registry_changed", async () => {
    const ctx = mount();
    ctx.handle.open();
    ctx.client.last("ping").resolve({ ok: false, error: { code: "registry_changed", message: "changed" } });
    await flush();
    expect(ctx.client.callsOf("ping")).toHaveLength(2);
    ctx.client.last("ping").resolve({ ok: false, error: { code: "registry_changed", message: "changed again" } });
    await flush();
    expect(ctx.client.callsOf("ping")).toHaveLength(2);
    expect(ctx.root.textContent).toContain("changed again");
    expect(button(ctx.root, "Retry")).toBeTruthy();
  });
});
