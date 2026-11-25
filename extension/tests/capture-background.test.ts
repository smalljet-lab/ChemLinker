import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CAPTURE_COMMAND,
  CAPTURE_FAILED_PARAM,
  CAPTURE_FAIL_REASONS,
  MAX_SCREENSHOTS,
  SCREENSHOT_TTL_MS,
  ScreenshotStore,
  activeTabIsPrivate,
  capturePageUrl,
  createCaptureStarter,
  createExplanationOpener,
  explanationUrl,
  registerCaptureCommand,
  takeScreenshot,
  type CaptureApi,
} from "../src/background/capture";
import {
  CAPTURE_IDLE_CLOSE_MS,
  CAPTURE_OP_TIMEOUTS_MS,
  DEFAULT_TIMEOUT_MS,
  NativeClient,
  type NativePort,
} from "../src/background/native-client";
import { CAPTURE_EXPIRED, CAPTURE_FAILED, CAPTURE_OPS, CAPTURE_PAGE, RECOGNIZE_TIMEOUT_MS } from "../src/shared/protocol";

const SHOT = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==";
const ID_A = "3f0c2a9e-1b7d-4c55-9e21-0a6d5b8f7c13";
const ID_B = "9a1d4e77-52c0-4b1e-8f3a-6c2b1d0e9f44";

afterEach(() => {
  vi.useRealTimers();
});

function idSource(...ids: string[]) {
  let i = 0;
  return () => ids[Math.min(i++, ids.length - 1)]!;
}

describe("screenshot store", () => {
  it("hands a screenshot out once, under its id only", () => {
    const store = new ScreenshotStore({ newId: idSource(ID_A) });
    const id = store.put(SHOT);
    expect(id).toBe(ID_A);
    expect(store.size).toBe(1);
    expect(store.take(ID_B)).toBeNull(); // wrong id
    expect(store.size).toBe(1);
    expect(store.take(id)).toBe(SHOT);
    expect(store.size).toBe(0);
    expect(store.take(id)).toBeNull(); // used
  });

  it.each([undefined, null, 7, {}, "", "short", "x".repeat(80), "__proto__", `${ID_A}\n`])("gives nothing for the id %j", (id) => {
    const store = new ScreenshotStore({ newId: idSource(ID_A) });
    store.put(SHOT);
    expect(store.take(id)).toBeNull();
    expect(store.size).toBe(1);
  });

  it("drops a screenshot nobody took after a short time", () => {
    vi.useFakeTimers();
    const store = new ScreenshotStore({ newId: idSource(ID_A) });
    const id = store.put(SHOT);
    vi.advanceTimersByTime(SCREENSHOT_TTL_MS - 1);
    expect(store.size).toBe(1);
    vi.advanceTimersByTime(1);
    expect(store.size).toBe(0);
    expect(store.take(id)).toBeNull();
    expect(SCREENSHOT_TTL_MS).toBeLessThanOrEqual(120_000);
  });

  it("stops the timer of a screenshot that was taken, and clear() forgets everything", () => {
    vi.useFakeTimers();
    const store = new ScreenshotStore({ newId: idSource(ID_A, ID_B) });
    store.take(store.put(SHOT));
    expect(vi.getTimerCount()).toBe(0);
    store.put(SHOT);
    store.clear();
    expect(store.size).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps only a few: the oldest goes first", () => {
    let n = 0;
    const store = new ScreenshotStore({ newId: () => `00000000-0000-4000-8000-00000000000${n++}` });
    const ids = Array.from({ length: MAX_SCREENSHOTS + 1 }, (_v, i) => store.put(`${SHOT}${i}`));
    expect(store.size).toBe(MAX_SCREENSHOTS);
    expect(store.take(ids[0])).toBeNull();
    expect(store.take(ids[MAX_SCREENSHOTS])).toBe(`${SHOT}${MAX_SCREENSHOTS}`);
  });

  it("never gives the same id twice", () => {
    const store = new ScreenshotStore({ newId: idSource(ID_A, ID_A, ID_B) });
    expect(store.put("data:image/png;base64,AAAA")).toBe(ID_A);
    expect(store.put("data:image/png;base64,BBBB")).toBe(ID_B);
    expect(store.take(ID_A)).toBe("data:image/png;base64,AAAA");
  });

  it("answers capture-image with the picture, then with capture_expired", () => {
    const store = new ScreenshotStore({ newId: idSource(ID_A) });
    store.put(SHOT);
    expect(takeScreenshot(store, ID_A)).toEqual({ ok: true, result: { image: SHOT } });
    const again = takeScreenshot(store, ID_A);
    expect(again).toMatchObject({ ok: false, error: { code: CAPTURE_EXPIRED } });
    expect(again.ok === false && again.error.message).toContain("Capture reaction again");
  });
});

describe("starting a capture", () => {
  function fakeApi(over: Partial<CaptureApi["tabs"]> = {}) {
    const captureVisibleTab = vi.fn(async (_windowId: number | undefined, _options: { format: "png" }) => SHOT);
    const create = vi.fn(async (_props: { url: string }) => ({ id: 5 }));
    const api: CaptureApi = {
      runtime: { getURL: (path) => `moz-extension://0b2c7d1e-uuid/${path}` },
      tabs: { captureVisibleTab, create, ...over },
    };
    return { api, captureVisibleTab, create };
  }

  it("captures the visible tab as PNG, keeps it and opens the capture page with its id", async () => {
    const { api, captureVisibleTab, create } = fakeApi();
    const store = new ScreenshotStore({ newId: idSource(ID_A) });
    expect(await createCaptureStarter(api, store)()).toEqual({ ok: true });
    expect(captureVisibleTab).toHaveBeenCalledWith(undefined, { format: "png" });
    expect(capturePageUrl(api)).toBe(`moz-extension://0b2c7d1e-uuid/${CAPTURE_PAGE}`);
    expect(create).toHaveBeenCalledWith({ url: `moz-extension://0b2c7d1e-uuid/${CAPTURE_PAGE}?id=${ID_A}` });
    // The picture is in the store only: not in the address of the page.
    expect(create.mock.calls[0]![0].url).not.toContain("base64");
    expect(store.take(ID_A)).toBe(SHOT);
  });

  it("answers capture_failed, keeps nothing and opens nothing when the tab cannot be captured", async () => {
    const { api, create } = fakeApi({
      captureVisibleTab: async () => {
        throw new Error("Missing activeTab permission");
      },
    });
    const store = new ScreenshotStore();
    const reply = await createCaptureStarter(api, store)();
    expect(reply).toMatchObject({ ok: false, error: { code: CAPTURE_FAILED } });
    expect(reply.ok === false && reply.error.message).toContain("(Firefox reports: Missing activeTab permission)");
    const chrome = await createCaptureStarter(api, store, "Chrome")();
    expect(chrome.ok === false && chrome.error.message).toContain("(Chrome reports: Missing activeTab permission). Chrome does not allow");
    expect(store.size).toBe(0);
    expect(create).not.toHaveBeenCalled();
  });

  it.each([undefined, null, "", "data:image/png;base64,", "data:image/jpeg;base64,AAAA", "https://example.org/x.png", 5])(
    "refuses %j as a picture",
    async (value) => {
      const { api, create } = fakeApi({ captureVisibleTab: async () => value as never });
      const store = new ScreenshotStore();
      expect(await createCaptureStarter(api, store)()).toMatchObject({ ok: false, error: { code: CAPTURE_FAILED } });
      expect(store.size).toBe(0);
      expect(create).not.toHaveBeenCalled();
    },
  );

  it("forgets the picture when the capture page cannot be opened", async () => {
    const { api } = fakeApi({
      create: async () => {
        throw new Error("no window");
      },
    });
    const store = new ScreenshotStore();
    const reply = await createCaptureStarter(api, store)();
    expect(reply).toMatchObject({ ok: false, error: { code: CAPTURE_FAILED } });
    expect(store.size).toBe(0);
  });

  it("the keyboard shortcut starts a capture; when that fails the capture page is opened to explain", async () => {
    let listener: ((command: string) => void) | null = null;
    const commands = { onCommand: { addListener: (fn: (command: string) => void) => (listener = fn) } };
    const start = vi.fn(async () => ({ ok: true }) as const);
    const explain = vi.fn(async () => {});
    registerCaptureCommand(commands, start, explain);
    listener!("something-else");
    expect(start).not.toHaveBeenCalled();
    listener!(CAPTURE_COMMAND);
    await vi.waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    expect(explain).not.toHaveBeenCalled();

    const failing = vi.fn(async () => ({ ok: false, reason: "not_capturable", error: { code: CAPTURE_FAILED, message: "no" } }) as const);
    registerCaptureCommand(commands, failing, explain);
    listener!(CAPTURE_COMMAND);
    await vi.waitFor(() => expect(explain).toHaveBeenCalledTimes(1));
    // The reason is handed on as its fixed word; the message (what Firefox or Chrome said) is not.
    expect(explain).toHaveBeenCalledWith("not_capturable");
  });

  it("every way a capture fails has a reason word", async () => {
    const store = new ScreenshotStore();
    const refused = fakeApi({
      captureVisibleTab: async () => {
        throw new Error("Missing activeTab permission");
      },
    });
    expect(await createCaptureStarter(refused.api, store)()).toMatchObject({ ok: false, reason: "not_capturable" });
    const empty = fakeApi({ captureVisibleTab: async () => "" });
    expect(await createCaptureStarter(empty.api, store)()).toMatchObject({ ok: false, reason: "no_picture" });
    const closed = fakeApi({
      create: async () => {
        throw new Error("no window");
      },
    });
    expect(await createCaptureStarter(closed.api, store)()).toMatchObject({ ok: false, reason: "page_not_opened" });
    expect([...CAPTURE_FAIL_REASONS].sort()).toEqual(["no_picture", "not_capturable", "page_not_opened", "private_window"]);
    expect(CAPTURE_FAILED_PARAM).toBe("failed");
  });

  it("the explanation page gets the reason word in its address, never the message", async () => {
    const { api, create } = fakeApi();
    const open = createExplanationOpener(api);
    await open("not_capturable");
    expect(create).toHaveBeenCalledWith({ url: `moz-extension://0b2c7d1e-uuid/${CAPTURE_PAGE}?failed=not_capturable` });
    expect(explanationUrl(api, "private_window")).toBe(`moz-extension://0b2c7d1e-uuid/${CAPTURE_PAGE}?failed=private_window`);
  });

  it("pressing the shortcut again uses the explanation tab that is already there, instead of opening one more", async () => {
    const update = vi.fn(async (_tabId: number, _props: { url: string; active: boolean }) => ({}));
    const { api, create } = fakeApi({ update });
    const open = createExplanationOpener(api);
    await open("not_capturable");
    await open("not_capturable");
    await open("no_picture");
    expect(create).toHaveBeenCalledTimes(1);
    expect(update).toHaveBeenCalledTimes(2);
    expect(update).toHaveBeenLastCalledWith(5, { url: `moz-extension://0b2c7d1e-uuid/${CAPTURE_PAGE}?failed=no_picture`, active: true });

    // The user closed that tab: a new one opens, and is used from then on.
    update.mockRejectedValueOnce(new Error("No tab with id: 5"));
    create.mockResolvedValueOnce({ id: 9 });
    await open("not_capturable");
    expect(create).toHaveBeenCalledTimes(2);
    update.mockClear();
    await open("not_capturable");
    expect(create).toHaveBeenCalledTimes(2);
    expect(update).toHaveBeenCalledWith(9, expect.objectContaining({ active: true }));
  });

  it("a private tab is refused before any picture is taken, when the starter is told to ask (Chrome)", async () => {
    const { api, captureVisibleTab, create } = fakeApi();
    const store = new ScreenshotStore();
    const reply = await createCaptureStarter(api, store, { browserName: "Chrome", isPrivateTab: async () => true })();
    expect(reply).toMatchObject({ ok: false, reason: "private_window", error: { code: CAPTURE_FAILED } });
    expect(reply.ok === false && reply.error.message).toContain("Chrome cannot show ChemLinker's capture page in a private window");
    expect(captureVisibleTab).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
    expect(store.size).toBe(0);

    // A normal tab, or a question that fails, does not stop the capture.
    expect(await createCaptureStarter(api, store, { isPrivateTab: async () => false })()).toEqual({ ok: true });
    const failing = async () => {
      throw new Error("no query");
    };
    expect(await createCaptureStarter(api, store, { isPrivateTab: failing })()).toEqual({ ok: true });
    // Without the option (Firefox) nothing is asked.
    expect(await createCaptureStarter(api, store)()).toEqual({ ok: true });
  });

  it("knows a private tab by the incognito flag of the active tab of the focused window", async () => {
    const query = vi.fn(async (_filter: { active: true; lastFocusedWindow: true }): Promise<unknown> => [{ id: 3, incognito: true }]);
    const { api } = fakeApi({ query });
    expect(await activeTabIsPrivate(api)).toBe(true);
    expect(query).toHaveBeenCalledWith({ active: true, lastFocusedWindow: true });
    query.mockResolvedValueOnce([{ id: 3, incognito: false }]);
    expect(await activeTabIsPrivate(api)).toBe(false);
    for (const odd of [[], undefined, null, "x", [null], [{ id: 3 }]]) {
      query.mockResolvedValueOnce(odd);
      expect(await activeTabIsPrivate(api)).toBe(false);
    }
    expect(await activeTabIsPrivate(fakeApi().api)).toBe(false); // no query function at all
  });

  it("a browser without the commands API is fine, and a throwing start does not escape", async () => {
    expect(() => registerCaptureCommand(undefined, async () => ({ ok: true }), async () => {})).not.toThrow();
    let listener: ((command: string) => void) | null = null;
    registerCaptureCommand(
      { onCommand: { addListener: (fn) => (listener = fn) } },
      async () => {
        throw new Error("boom");
      },
      async () => {},
    );
    expect(() => listener!(CAPTURE_COMMAND)).not.toThrow();
    await Promise.resolve();
  });
});

describe("the capture connection to the host", () => {
  class FakePort implements NativePort {
    posted: Array<{ id: string; op: string }> = [];
    disconnected = false;
    private messageListeners: Array<(message: unknown) => void> = [];
    onMessage = { addListener: (l: (message: unknown) => void) => void this.messageListeners.push(l) };
    onDisconnect = { addListener: () => {} };
    postMessage(message: unknown): void {
      this.posted.push(message as { id: string; op: string });
    }
    disconnect(): void {
      this.disconnected = true;
    }
    answer(index = this.posted.length - 1): void {
      const id = this.posted[index]!.id;
      for (const l of this.messageListeners) l({ id, ok: true, result: {} });
    }
  }

  function setup() {
    const ports: FakePort[] = [];
    const client = new NativeClient({
      connect: () => {
        const port = new FakePort();
        ports.push(port);
        return port;
      },
      opTimeoutsMs: CAPTURE_OP_TIMEOUTS_MS,
      idleCloseMs: CAPTURE_IDLE_CLOSE_MS,
      queued: true,
      log: () => {},
    });
    return { client, ports };
  }

  it("a short request sent while a recognition runs waits for it instead of timing out after 30 s", async () => {
    vi.useFakeTimers();
    const { client, ports } = setup();
    // Page A recognises (the models still load); page B opens and asks for the status.
    const recognize = client.request("recognize", { image: "AAAA", mode: "reaction" });
    vi.advanceTimersByTime(10_000);
    let statusReply: unknown = null;
    const status = client.request("recognition_status", { start: true }).then((reply) => (statusReply = reply));
    vi.advanceTimersByTime(120_000); // far beyond 30 s; the host has not even read the status request yet
    await Promise.resolve();
    expect(statusReply).toBeNull();
    expect(client.pendingCount).toBe(2);
    // The host answers in order.
    ports[0]!.answer(0);
    expect((await recognize).ok).toBe(true);
    ports[0]!.answer(1);
    await status;
    expect(statusReply).toMatchObject({ ok: true });
  });

  it("a queued request still ends: 30 s after the latest moment the earlier one can end", async () => {
    vi.useFakeTimers();
    const { client } = setup();
    void client.request("recognize", { image: "AAAA", mode: "reaction" });
    vi.advanceTimersByTime(100_000);
    const describe = client.request("describe_structures", { structures: [{ smiles: "CCO" }] });
    // The recognition may run until 300 s; the queued request until 330 s.
    vi.advanceTimersByTime(229_999);
    expect(client.pendingCount).toBe(1); // the recognition has timed out, the other still waits
    vi.advanceTimersByTime(1);
    const reply = await describe;
    expect(reply).toMatchObject({ ok: false, error: { code: "host_timeout" } });
    expect(reply.ok === false && reply.error.message).toContain("was busy with an earlier request");
    expect(client.pendingCount).toBe(0);
  });

  it("with nothing before it a request has its own limit, and the registry client never queues", async () => {
    vi.useFakeTimers();
    const { client } = setup();
    const alone = client.request("recognition_status", {});
    vi.advanceTimersByTime(30_000);
    const reply = await alone;
    expect(reply).toMatchObject({ ok: false, error: { code: "host_timeout" } });
    expect(reply.ok === false && reply.error.message).toBe("The local registry program did not answer within 30 seconds.");

    const registry = new NativeClient({ connect: () => new FakePort(), log: () => {} });
    void registry.request("generate_name", { compound_id: "X" } as never); // 120 s or more
    const ping = registry.request("ping", {});
    vi.advanceTimersByTime(30_000);
    expect(await ping).toMatchObject({ ok: false, error: { code: "host_timeout" } });
  });

  it("waits 300 s for recognize and 30 s for the other capture operations", () => {
    const { client } = setup();
    expect(RECOGNIZE_TIMEOUT_MS).toBe(300_000);
    expect(DEFAULT_TIMEOUT_MS).toBe(30_000);
    for (const op of CAPTURE_OPS) expect(client.timeoutFor(op)).toBe(op === "recognize" ? 300_000 : 30_000);
  });

  it("a recognize still unanswered after 300 s ends with host_timeout", async () => {
    vi.useFakeTimers();
    const { client } = setup();
    const pending = client.request("recognize", { image: "AAAA", mode: "reaction" });
    vi.advanceTimersByTime(299_999);
    expect(client.pendingCount).toBe(1);
    vi.advanceTimersByTime(1);
    expect(await pending).toMatchObject({ ok: false, error: { code: "host_timeout" } });
  });

  it("closes its port after 11 idle minutes (longer than the host keeps its worker), and reopens on the next request", async () => {
    vi.useFakeTimers();
    const { client, ports } = setup();
    expect(CAPTURE_IDLE_CLOSE_MS).toBeGreaterThan(10 * 60_000);
    const first = client.request("recognition_status", { start: true });
    ports[0]!.answer();
    await first;
    vi.advanceTimersByTime(CAPTURE_IDLE_CLOSE_MS - 1);
    expect(ports[0]!.disconnected).toBe(false);
    vi.advanceTimersByTime(1);
    expect(ports[0]!.disconnected).toBe(true);
    expect(client.connected).toBe(false);

    const second = client.request("recognition_status", {});
    expect(ports).toHaveLength(2);
    ports[1]!.answer();
    expect((await second).ok).toBe(true);
  });

  it("does not close while a request is pending, and counts the idle time from the last answer", async () => {
    vi.useFakeTimers();
    const { client, ports } = setup();
    const status = client.request("recognition_status", {});
    ports[0]!.answer();
    await status;
    vi.advanceTimersByTime(CAPTURE_IDLE_CLOSE_MS - 1000);
    const slow = client.request("recognize", { image: "AAAA", mode: "molecule" });
    vi.advanceTimersByTime(200_000); // well past the old idle deadline
    expect(ports[0]!.disconnected).toBe(false);
    ports[0]!.answer();
    await slow;
    vi.advanceTimersByTime(CAPTURE_IDLE_CLOSE_MS - 1);
    expect(ports[0]!.disconnected).toBe(false);
    vi.advanceTimersByTime(1);
    expect(ports[0]!.disconnected).toBe(true);
  });

  it("the registry client (no idle time) keeps its port open", async () => {
    vi.useFakeTimers();
    const port = new FakePort();
    const client = new NativeClient({ connect: () => port, log: () => {} });
    const ping = client.request("ping", {});
    port.answer();
    await ping;
    vi.advanceTimersByTime(24 * 3600_000);
    expect(port.disconnected).toBe(false);
    expect(client.timeoutFor("recognize")).toBe(30_000); // capture operations never go through it
  });
});
