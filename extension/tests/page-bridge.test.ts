// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createDomBridgeClient, parseBridgeResponse } from "../src/content/bridge-client";
import { installBridge, molfileDownloadUrl, type BridgeWindow } from "../src/page/bridge";
import {
  BRIDGE_INSTALLED_FLAG,
  BRIDGE_MOLFILE_TIMEOUT_MS,
  BRIDGE_REQUEST_EVENT,
  BRIDGE_RESPONSE_EVENT,
  BRIDGE_VERSION,
  type BridgeResponse,
} from "../src/shared/bridge";
import { MAX_STRUCTURE_CHARS } from "../src/shared/protocol";

const ORIGIN = "https://sciformation.chemie.unibas.ch";
const MOL_URL = "/zkau/view/z_desk/dwnmed-2/d0c1/molfile.mol";
const MOLFILE = "\n  ZK\n\n  0  0  0  0  0  0            999 V2000\nM  END\n";

interface Fake {
  win: BridgeWindow;
  original: ReturnType<typeof vi.fn>;
  fire: ReturnType<typeof vi.fn>;
  fetch: ReturnType<typeof vi.fn>;
  responses: BridgeResponse[];
  /** The server answers the last fired event with a download command. */
  serverDownload(url?: string): void;
  uninstall: () => void;
}

function okResponse(text: string, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    text: async () => text,
  } as unknown as Response;
}

function setup(options: { zk?: boolean; autoServer?: boolean } = {}): Fake {
  const original = vi.fn();
  const zAu = { cmd0: { download: original as (...args: unknown[]) => unknown } };
  const fire = vi.fn();
  const widgets = new Map<string, { fire: typeof fire }>();
  const zk = { Widget: { $: (el: Element) => widgets.get(el.id) ?? null } };
  for (const id of ["m1", "m2"]) {
    const div = document.createElement("div");
    div.id = id;
    document.body.append(div);
    widgets.set(id, { fire });
  }
  const fetch = vi.fn(async () => okResponse(MOLFILE));
  const win: BridgeWindow = {
    document,
    location: { href: `${ORIGIN}/main#x`, origin: ORIGIN },
    CustomEvent,
    fetch: fetch as unknown as BridgeWindow["fetch"],
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (t) => clearTimeout(t),
  };
  if (options.zk !== false) {
    win.zk = zk;
    win.zAu = zAu;
  }
  const responses: BridgeResponse[] = [];
  const listener = (e: Event) => {
    const detail = (e as CustomEvent).detail;
    if (typeof detail === "string") responses.push(JSON.parse(detail) as BridgeResponse);
  };
  document.addEventListener(BRIDGE_RESPONSE_EVENT, listener);
  const uninstallBridge = installBridge(win);
  const fake: Fake = {
    win,
    original,
    fire,
    fetch,
    responses,
    serverDownload: (url = MOL_URL) => zAu.cmd0.download(url),
    uninstall: () => {
      uninstallBridge();
      document.removeEventListener(BRIDGE_RESPONSE_EVENT, listener);
    },
  };
  return fake;
}

function send(detail: unknown): void {
  document.dispatchEvent(new CustomEvent(BRIDGE_REQUEST_EVENT, { detail }));
}
const request = (r: object) => send(JSON.stringify(r));
const flush = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
};

let fake: Fake | null = null;
beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  fake?.uninstall();
  fake = null;
  vi.useRealTimers();
  document.body.replaceChildren();
});

describe("page bridge", () => {
  it("answers hello with whether ZK is there", async () => {
    fake = setup();
    request({ id: "h1", kind: "hello" });
    expect(fake.responses).toEqual([{ id: "h1", ok: true, kind: "hello", zk: true }]);
  });

  it("answers hello in a frame without ZK, and refuses molfile there", async () => {
    fake = setup({ zk: false });
    request({ id: "h1", kind: "hello" });
    request({ id: "m", kind: "molfile", domId: "m1" });
    await flush();
    expect(fake.responses).toEqual([
      { id: "h1", ok: true, kind: "hello", zk: false },
      expect.objectContaining({ id: "m", ok: false, code: "no_zk" }),
    ]);
  });

  it("reads a molfile through the download hook and restores the original", async () => {
    fake = setup();
    request({ id: "r1", kind: "molfile", domId: "m1" });
    await flush();
    expect(fake.fire).toHaveBeenCalledWith("onGetMolfile", [null], { toServer: true });
    expect(fake.win.zAu.cmd0.download).not.toBe(fake.original); // hooked while waiting
    fake.serverDownload();
    await flush();
    expect(fake.win.zAu.cmd0.download).toBe(fake.original);
    expect(fake.original).not.toHaveBeenCalled(); // no browser download
    expect(fake.fetch).toHaveBeenCalledWith(`${ORIGIN}${MOL_URL}`, { credentials: "same-origin", mode: "same-origin" });
    expect(fake.responses).toEqual([{ id: "r1", ok: true, kind: "molfile", molfile: MOLFILE }]);
  });

  it("handles requests strictly one at a time", async () => {
    fake = setup();
    request({ id: "r1", kind: "molfile", domId: "m1" });
    request({ id: "r2", kind: "molfile", domId: "m2" });
    await flush();
    expect(fake.fire).toHaveBeenCalledTimes(1);
    fake.serverDownload();
    await flush();
    expect(fake.responses.map((r) => r.id)).toEqual(["r1"]);
    expect(fake.fire).toHaveBeenCalledTimes(2);
    fake.serverDownload();
    await flush();
    expect(fake.responses.map((r) => r.id)).toEqual(["r1", "r2"]);
    expect(fake.win.zAu.cmd0.download).toBe(fake.original);
  });

  it("passes other downloads to the original function untouched", async () => {
    fake = setup();
    request({ id: "r1", kind: "molfile", domId: "m1" });
    await flush();
    const ctx = { me: true };
    fake.win.zAu.cmd0.download.call(ctx, "/zkau/view/z/dwnmed-1/report.pdf", 7);
    expect(fake.original).toHaveBeenCalledWith("/zkau/view/z/dwnmed-1/report.pdf", 7);
    expect(fake.original.mock.contexts[0]).toBe(ctx);
    fake.serverDownload();
    await flush();
    expect(fake.responses[0]).toMatchObject({ id: "r1", ok: true });
  });

  it.each([
    "https://evil.example/zkau/view/x/molfile.mol",
    "http://sciformation.chemie.unibas.ch/zkau/view/x/molfile.mol",
    "/other/view/x/molfile.mol",
    "/zkau/view/x/molfile.mol.exe",
    "javascript:alert(1)//molfile.mol",
  ])("does not capture %s (passes it on, then times out)", async (url) => {
    fake = setup();
    request({ id: "r1", kind: "molfile", domId: "m1" });
    await flush();
    fake.serverDownload(url);
    expect(fake.original).toHaveBeenCalledWith(url);
    await vi.advanceTimersByTimeAsync(BRIDGE_MOLFILE_TIMEOUT_MS);
    await flush();
    expect(fake.fetch).not.toHaveBeenCalled();
    expect(fake.responses).toEqual([expect.objectContaining({ id: "r1", ok: false, code: "timeout" })]);
    expect(fake.win.zAu.cmd0.download).toBe(fake.original);
  });

  it("times out, restores the original, and goes on with the next request", async () => {
    fake = setup();
    request({ id: "r1", kind: "molfile", domId: "m1" });
    request({ id: "r2", kind: "molfile", domId: "m2" });
    await flush();
    await vi.advanceTimersByTimeAsync(BRIDGE_MOLFILE_TIMEOUT_MS - 1);
    expect(fake.responses).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    await flush();
    expect(fake.responses[0]).toMatchObject({ id: "r1", ok: false, code: "timeout" });
    expect(fake.fire).toHaveBeenCalledTimes(2);
    fake.serverDownload();
    await flush();
    expect(fake.responses[1]).toMatchObject({ id: "r2", ok: true });
    expect(fake.win.zAu.cmd0.download).toBe(fake.original);
  });

  it("restores the original when fire throws", async () => {
    fake = setup();
    fake.fire.mockImplementation(() => {
      throw new Error("zk broke");
    });
    request({ id: "r1", kind: "molfile", domId: "m1" });
    await flush();
    expect(fake.responses).toEqual([expect.objectContaining({ id: "r1", ok: false, code: "unexpected" })]);
    expect(fake.win.zAu.cmd0.download).toBe(fake.original);
  });

  it("reports failed downloads", async () => {
    fake = setup();
    fake.fetch.mockImplementationOnce(async () => okResponse("", 500));
    fake.fetch.mockImplementationOnce(async () => {
      throw new TypeError("NetworkError");
    });
    request({ id: "r1", kind: "molfile", domId: "m1" });
    await flush();
    fake.serverDownload();
    await flush();
    request({ id: "r2", kind: "molfile", domId: "m1" });
    await flush();
    fake.serverDownload();
    await flush();
    expect(fake.responses.map((r) => (r.ok ? "ok" : r.code))).toEqual(["fetch_failed", "fetch_failed"]);
  });

  it("refuses a molfile over the size limit", async () => {
    fake = setup();
    fake.fetch.mockImplementationOnce(async () => okResponse("x".repeat(MAX_STRUCTURE_CHARS + 1)));
    request({ id: "r1", kind: "molfile", domId: "m1" });
    await flush();
    fake.serverDownload();
    await flush();
    expect(fake.responses).toEqual([expect.objectContaining({ id: "r1", ok: false, code: "too_large" })]);
  });

  it("answers no_widget for a vanished element or widget", async () => {
    fake = setup();
    request({ id: "r1", kind: "molfile", domId: "gone" });
    document.body.append(Object.assign(document.createElement("div"), { id: "plain" }));
    request({ id: "r2", kind: "molfile", domId: "plain" });
    await flush();
    expect(fake.responses.map((r) => (r.ok ? "ok" : r.code))).toEqual(["no_widget", "no_widget"]);
    expect(fake.fire).not.toHaveBeenCalled();
  });

  it.each([
    [{ notAString: true }],
    ["{not json"],
    [JSON.stringify(["array"])],
    [JSON.stringify({ kind: "hello" })],
    [JSON.stringify({ id: "x", kind: "delete" })],
    [JSON.stringify({ id: "x", kind: "molfile" })],
    [null],
  ])("answers a bad request %j once, without throwing", async (detail) => {
    fake = setup();
    expect(() => send(detail)).not.toThrow();
    await flush();
    expect(fake.responses).toHaveLength(1);
    expect(fake.responses[0]).toMatchObject({ ok: false, code: "bad_request" });
  });

  it("answers every request exactly once", async () => {
    fake = setup();
    request({ id: "a", kind: "hello" });
    request({ id: "b", kind: "molfile", domId: "m1" });
    request({ id: "c", kind: "molfile", domId: "nope" });
    await flush();
    fake.serverDownload();
    fake.serverDownload(); // a second download is not ours any more: passed on
    await flush();
    await vi.advanceTimersByTimeAsync(BRIDGE_MOLFILE_TIMEOUT_MS * 2);
    await flush();
    const ids = fake.responses.map((r) => r.id).sort();
    expect(ids).toEqual(["a", "b", "c"]);
    expect(fake.original).toHaveBeenCalledTimes(1);
  });

  it("installs only once per window", () => {
    fake = setup();
    const second = installBridge(fake.win);
    request({ id: "h", kind: "hello" });
    expect(fake.responses).toHaveLength(1);
    second();
  });

  it("names its events and its installed mark after the version, so a bridge of another release is never addressed", () => {
    expect(BRIDGE_VERSION).toBe("dev"); // outside a build; build.mjs puts the extension version here
    expect(BRIDGE_REQUEST_EVENT).toBe(`compound-registry-bridge-request/${BRIDGE_VERSION}`);
    expect(BRIDGE_RESPONSE_EVENT).toBe(`compound-registry-bridge-response/${BRIDGE_VERSION}`);
    expect(BRIDGE_INSTALLED_FLAG).toBe(`__compoundRegistryBridgeInstalled/${BRIDGE_VERSION}`);

    // What a tab still holds after an update: the mark and the listener of the previous release.
    const stale = vi.fn();
    document.addEventListener("compound-registry-bridge-request", stale);
    document.addEventListener("compound-registry-bridge-request/0.0.1", stale);
    Object.defineProperty(window, "__compoundRegistryBridgeInstalled", { value: true, configurable: true });
    Object.defineProperty(window, "__compoundRegistryBridgeInstalled/0.0.1", { value: true, configurable: true });
    try {
      fake = setup();
      (fake.win as unknown as Record<string, unknown>)["__compoundRegistryBridgeInstalled/0.0.1"] = true;
      const again = installBridge(fake.win); // this release is installed already
      request({ id: "h", kind: "hello" });
      expect(fake.responses).toEqual([{ id: "h", ok: true, kind: "hello", zk: true }]);
      expect(stale).not.toHaveBeenCalled();
      expect((fake.win as unknown as Record<string, unknown>)[BRIDGE_INSTALLED_FLAG]).toBe(true);
      again();
    } finally {
      document.removeEventListener("compound-registry-bridge-request", stale);
      document.removeEventListener("compound-registry-bridge-request/0.0.1", stale);
      delete (window as unknown as Record<string, unknown>)["__compoundRegistryBridgeInstalled"];
      delete (window as unknown as Record<string, unknown>)["__compoundRegistryBridgeInstalled/0.0.1"];
    }
  });

  it("gives up a download that never ends, aborts it, and goes on with the next request", async () => {
    fake = setup();
    const aborts: AbortSignal[] = [];
    fake.win.AbortController = AbortController;
    fake.fetch.mockImplementationOnce((_url: string, init: RequestInit) => {
      aborts.push(init.signal!);
      // The server accepted the request and never finishes the answer.
      return new Promise<Response>((_resolve, reject) => {
        init.signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    });
    request({ id: "r1", kind: "molfile", domId: "m1" });
    request({ id: "r2", kind: "molfile", domId: "m2" });
    await flush();
    await vi.advanceTimersByTimeAsync(5000);
    fake.serverDownload();
    await flush();
    expect(fake.fetch).toHaveBeenCalledTimes(1);
    // The limit covers the whole read: 5 s are used up already.
    await vi.advanceTimersByTimeAsync(BRIDGE_MOLFILE_TIMEOUT_MS - 5000 - 1);
    expect(fake.responses).toEqual([]);
    expect(aborts[0]!.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await flush();
    expect(fake.responses).toEqual([expect.objectContaining({ id: "r1", ok: false, code: "timeout" })]);
    expect(aborts[0]!.aborted).toBe(true);
    // The next request was waiting and now runs.
    expect(fake.fire).toHaveBeenCalledTimes(2);
    fake.serverDownload();
    await flush();
    expect(fake.responses[1]).toMatchObject({ id: "r2", ok: true, kind: "molfile", molfile: MOLFILE });
    expect(fake.responses).toHaveLength(2);
  });

  it("gives up a body that never ends, also where the download cannot be aborted", async () => {
    fake = setup(); // this window has no AbortController
    fake.fetch.mockImplementationOnce(
      async () =>
        ({ ok: true, status: 200, headers: { get: () => null }, text: () => new Promise<string>(() => {}) }) as unknown as Response,
    );
    request({ id: "r1", kind: "molfile", domId: "m1" });
    request({ id: "r2", kind: "molfile", domId: "m2" });
    await flush();
    fake.serverDownload();
    await flush();
    await vi.advanceTimersByTimeAsync(BRIDGE_MOLFILE_TIMEOUT_MS);
    await flush();
    expect(fake.responses).toEqual([expect.objectContaining({ id: "r1", ok: false, code: "timeout" })]);
    fake.serverDownload();
    await flush();
    expect(fake.responses[1]).toMatchObject({ id: "r2", ok: true });
  });

  it("does not time out a download that ends in time, and passes no signal it does not have", async () => {
    fake = setup();
    request({ id: "r1", kind: "molfile", domId: "m1" });
    await flush();
    fake.serverDownload();
    await flush();
    expect(fake.responses).toEqual([{ id: "r1", ok: true, kind: "molfile", molfile: MOLFILE }]);
    await vi.advanceTimersByTimeAsync(BRIDGE_MOLFILE_TIMEOUT_MS * 2);
    await flush();
    expect(fake.responses).toHaveLength(1);
    expect(Object.keys(fake.fetch.mock.calls[0]![1] as object).sort()).toEqual(["credentials", "mode"]);
  });

  it("recognises only same-origin /zkau/…/molfile.mol URLs", () => {
    const loc = { href: `${ORIGIN}/main`, origin: ORIGIN };
    expect(molfileDownloadUrl(MOL_URL, loc)).toBe(`${ORIGIN}${MOL_URL}`);
    expect(molfileDownloadUrl(`${ORIGIN}${MOL_URL}?x=1`, loc)).toBe(`${ORIGIN}${MOL_URL}?x=1`);
    expect(molfileDownloadUrl(42, loc)).toBeNull();
    expect(molfileDownloadUrl("//evil.example/zkau/a/molfile.mol", loc)).toBeNull();
  });
});

describe("bridge client with the real bridge (one document, as in jsdom)", () => {
  it("round-trips hello and molfile", async () => {
    fake = setup();
    const client = createDomBridgeClient(document);
    expect(await client.hello(1000)).toBe(true);
    const pending = client.molfile("m1", 60_000);
    await flush();
    fake.serverDownload();
    await flush();
    expect(await pending).toEqual({ ok: true, molfile: MOLFILE });
    client.dispose();
  });

  it("reports no answer after its own timeout", async () => {
    const client = createDomBridgeClient(document);
    const hello = client.hello(500);
    await vi.advanceTimersByTimeAsync(500);
    expect(await hello).toBeNull();
    const mol = client.molfile("m1", 500);
    await vi.advanceTimersByTimeAsync(500);
    expect(await mol).toMatchObject({ ok: false, code: "no_answer" });
    client.dispose();
  });

  it("takes the first answer per id and ignores forged or malformed ones", async () => {
    const client = createDomBridgeClient(document, { newId: () => "fixed" });
    const reply = (detail: unknown) => document.dispatchEvent(new CustomEvent(BRIDGE_RESPONSE_EVENT, { detail }));
    const p = client.molfile("m1", 1000);
    reply({ id: "fixed" }); // not a string
    reply("{bad json");
    reply(JSON.stringify({ id: "other", ok: true, kind: "molfile", molfile: "X" }));
    reply(JSON.stringify({ id: "fixed", ok: true, kind: "molfile", molfile: "FIRST\nM  END\n" }));
    reply(JSON.stringify({ id: "fixed", ok: true, kind: "molfile", molfile: "SECOND" }));
    expect(await p).toEqual({ ok: true, molfile: "FIRST\nM  END\n" });
    client.dispose();
  });

  it("parses responses strictly", () => {
    expect(parseBridgeResponse(JSON.stringify({ id: "a", ok: false, code: "weird", message: "m" }))).toEqual({
      id: "a",
      ok: false,
      code: "unexpected",
      message: "m",
    });
    expect(parseBridgeResponse(JSON.stringify({ id: "", ok: true, kind: "hello", zk: true }))).toBeNull();
    expect(parseBridgeResponse(JSON.stringify({ id: "a", ok: true, kind: "molfile", molfile: 3 }))).toBeNull();
  });
});
