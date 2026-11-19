import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_TIMEOUT_MS, NativeClient, OP_TIMEOUTS_MS, type NativePort } from "../src/background/native-client";
import { GENERATE_NAME_TIMEOUT_MS, type BackgroundReply } from "../src/shared/protocol";

class FakePort implements NativePort {
  sent: Record<string, unknown>[] = [];
  error: { message: string } | null = null;
  disconnected = false;
  throwOnPost = false;
  private messageListeners: ((message: unknown) => void)[] = [];
  private disconnectListeners: ((port: NativePort) => void)[] = [];
  onMessage = { addListener: (l: (message: unknown) => void) => void this.messageListeners.push(l) };
  onDisconnect = { addListener: (l: (port: NativePort) => void) => void this.disconnectListeners.push(l) };
  postMessage(message: unknown): void {
    if (this.throwOnPost || this.disconnected) throw new Error("Attempt to postMessage on disconnected port");
    this.sent.push(JSON.parse(JSON.stringify(message)));
  }
  disconnect(): void {
    this.disconnected = true;
  }
  /** The host sends a message. */
  emit(message: unknown): void {
    for (const l of this.messageListeners) l(message);
  }
  /** The host process ends / Firefox closes the port. */
  drop(reason: string | null): void {
    this.error = reason === null ? null : { message: reason };
    this.disconnected = true;
    for (const l of this.disconnectListeners) l(this);
  }
  lastId(): string {
    return this.sent[this.sent.length - 1]!.id as string;
  }
}

function setup(opts: { timeoutMs?: number; lastError?: string } = {}) {
  const ports: FakePort[] = [];
  let n = 0;
  const log = vi.fn();
  const client = new NativeClient({
    connect: () => {
      const port = new FakePort();
      ports.push(port);
      return port;
    },
    newId: () => `req-${++n}`,
    timeoutMs: opts.timeoutMs ?? 30_000,
    lastErrorMessage: () => opts.lastError,
    log,
  });
  return { client, ports, log };
}

function track<T>(promise: Promise<T>): { value: T | undefined; done: boolean } {
  const box = { value: undefined as T | undefined, done: false };
  promise.then((v) => {
    box.value = v;
    box.done = true;
  });
  return box;
}

const flush = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

describe("NativeClient", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("connects on demand and sends id, protocol version, op and params", async () => {
    const { client, ports } = setup();
    expect(ports).toHaveLength(0);
    void client.request("analyze", { smiles: "CCO", mode: null });
    expect(ports).toHaveLength(1);
    expect(ports[0]!.sent[0]).toEqual({ id: "req-1", v: 1, op: "analyze", params: { smiles: "CCO", mode: null } });
  });

  it("reuses one port and matches responses by id, in any order", async () => {
    const { client, ports } = setup();
    const a = track(client.request("ping", {}));
    const b = track(client.request("analyze", { smiles: "CCO" }));
    expect(ports).toHaveLength(1);
    const [first, second] = ports[0]!.sent;
    expect(first!.id).not.toBe(second!.id);
    ports[0]!.emit({ id: second!.id, ok: true, result: { which: "b" } });
    await flush();
    expect(b.done).toBe(true);
    expect(a.done).toBe(false);
    expect(b.value).toEqual({ ok: true, result: { which: "b" } });
    ports[0]!.emit({ id: first!.id, ok: false, error: { code: "structure_empty", message: "Empty." } });
    await flush();
    expect(a.value).toEqual({ ok: false, error: { code: "structure_empty", message: "Empty." } });
    expect(client.pendingCount).toBe(0);
  });

  it("passes existing_id of a slot_conflict through", async () => {
    const { client, ports } = setup();
    const r = track(client.request("analyze", { smiles: "CCO" }));
    ports[0]!.emit({ id: ports[0]!.lastId(), ok: false, error: { code: "slot_conflict", message: "Taken.", existing_id: "000001_01_001" } });
    await flush();
    expect(r.value).toEqual({ ok: false, error: { code: "slot_conflict", message: "Taken.", existing_id: "000001_01_001" } });
  });

  it("times out after the limit with host_timeout and ignores the late answer", async () => {
    const { client, ports, log } = setup({ timeoutMs: 30_000 });
    const r = track(client.request("ping", {}));
    const id = ports[0]!.lastId();
    await vi.advanceTimersByTimeAsync(29_999);
    expect(r.done).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(r.value?.ok).toBe(false);
    expect((r.value as Extract<BackgroundReply, { ok: false }>).error.code).toBe("host_timeout");
    expect(client.pendingCount).toBe(0);
    ports[0]!.emit({ id, ok: true, result: {} });
    expect(log).toHaveBeenCalled();
    expect(client.connected).toBe(true); // the port is kept
  });

  it("fails every pending request on disconnect with Firefox's reason, then reconnects", async () => {
    const { client, ports } = setup();
    const a = track(client.request("ping", {}));
    const b = track(client.request("analyze", { smiles: "C" }));
    ports[0]!.drop("No such native application compound_registry");
    await flush();
    for (const r of [a, b]) {
      expect(r.value?.ok).toBe(false);
      const error = (r.value as Extract<BackgroundReply, { ok: false }>).error;
      expect(error.code).toBe("host_unavailable");
      expect(error.message).toContain("No such native application compound_registry");
    }
    expect(client.pendingCount).toBe(0);
    void client.request("ping", {});
    expect(ports).toHaveLength(2);
    expect(ports[1]!.sent).toHaveLength(1);
  });

  it("uses runtime.lastError when the port has no error", async () => {
    const { client, ports } = setup({ lastError: "Native application tried to send a message of 2000000 bytes" });
    const r = track(client.request("ping", {}));
    ports[0]!.drop(null);
    await flush();
    expect((r.value as Extract<BackgroundReply, { ok: false }>).error.message).toContain("2000000 bytes");
  });

  it("answers host_bad_response for a malformed answer to a pending request", async () => {
    const { client, ports } = setup();
    const a = track(client.request("ping", {}));
    const b = track(client.request("ping", {}));
    const c = track(client.request("ping", {}));
    const [ia, ib, ic] = ports[0]!.sent.map((m) => m.id);
    ports[0]!.emit({ id: ia, ok: true }); // no result
    ports[0]!.emit({ id: ib, ok: false, error: { code: 3 } }); // bad error
    ports[0]!.emit({ id: ic, ok: "yes", result: {} }); // bad ok
    await flush();
    for (const r of [a, b, c]) {
      expect((r.value as Extract<BackgroundReply, { ok: false }>).error.code).toBe("host_bad_response");
    }
  });

  it("ignores messages with an unknown id or no object shape", async () => {
    const { client, ports, log } = setup();
    const r = track(client.request("ping", {}));
    const id = ports[0]!.lastId();
    ports[0]!.emit({ id: "someone-else", ok: true, result: {} });
    ports[0]!.emit({ id: null, ok: false, error: { code: "bad_request", message: "x" } });
    ports[0]!.emit("garbage");
    ports[0]!.emit(null);
    ports[0]!.emit([1, 2]);
    await flush();
    expect(r.done).toBe(false);
    expect(log).toHaveBeenCalledTimes(5);
    ports[0]!.emit({ id, ok: true, result: { fine: true } });
    await flush();
    expect(r.value).toEqual({ ok: true, result: { fine: true } });
  });

  it("reports host_unavailable when connectNative throws", async () => {
    const client = new NativeClient({
      connect: () => {
        throw new Error("nativeMessaging permission missing");
      },
      log: () => {},
    });
    const reply = await client.request("ping", {});
    expect(reply.ok).toBe(false);
    expect((reply as Extract<BackgroundReply, { ok: false }>).error.code).toBe("host_unavailable");
    expect((reply as Extract<BackgroundReply, { ok: false }>).error.message).toContain("nativeMessaging permission missing");
  });

  it("drops a port whose postMessage throws and reconnects next time", async () => {
    const { client, ports } = setup();
    void client.request("ping", {});
    ports[0]!.throwOnPost = true;
    const r = track(client.request("ping", {}));
    await flush();
    expect((r.value as Extract<BackgroundReply, { ok: false }>).error.code).toBe("host_unavailable");
    expect(client.pendingCount).toBe(0);
    expect(ports[0]!.disconnected).toBe(true);
    void client.request("ping", {});
    expect(ports).toHaveLength(2);
  });

  it("close() disconnects the port and fails pending requests", async () => {
    const { client, ports } = setup();
    const r = track(client.request("ping", {}));
    client.close();
    await flush();
    expect(ports[0]!.disconnected).toBe(true);
    expect((r.value as Extract<BackgroundReply, { ok: false }>).error.code).toBe("host_unavailable");
    expect(client.connected).toBe(false);
  });

  it("ignores events of a port that was already replaced", async () => {
    const { client, ports } = setup();
    void client.request("ping", {});
    ports[0]!.drop("gone");
    const r = track(client.request("ping", {}));
    const id = ports[1]!.lastId();
    ports[0]!.emit({ id, ok: true, result: { from: "old" } });
    ports[0]!.drop("gone again");
    await flush();
    expect(r.done).toBe(false);
    ports[1]!.emit({ id, ok: true, result: { from: "new" } });
    await flush();
    expect(r.value).toEqual({ ok: true, result: { from: "new" } });
  });

  it("gives every request a fresh id by default", () => {
    const sent: string[] = [];
    const port = new FakePort();
    const client = new NativeClient({ connect: () => port, log: () => {} });
    for (let i = 0; i < 20; i++) void client.request("ping", {});
    for (const m of port.sent) sent.push(m.id as string);
    expect(new Set(sent).size).toBe(20);
    for (const id of sent) {
      expect(id.length).toBeGreaterThan(0);
      expect(id.length).toBeLessThanOrEqual(64);
    }
  });
});

describe("NativeClient privacy and fatal host errors", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const SECRET_SMILES = "C[C@H](N)C(=O)OSECRETSMILES";
  const SECRET_SVG = "<svg>SECRETSVG</svg>";
  const SECRET_MOL = "\n  SECRETMOLFILE\n\nM  END\n";

  function anyLogged(log: ReturnType<typeof vi.fn>): string {
    return JSON.stringify(log.mock.calls, (_k, v) => (v instanceof Error ? v.message : v));
  }

  it("never passes host message content to the log", async () => {
    const { client, ports, log } = setup();
    void client.request("analyze", { molfile: SECRET_MOL });
    const result = { structure: { smiles: SECRET_SMILES, svg: SECRET_SVG } };
    const port = ports[0]!;
    port.emit({ id: "unknown-id", ok: true, result }); // unknown id
    port.emit({ id: SECRET_SMILES.repeat(5), ok: true, result }); // over-long id
    port.emit({ id: `x\n${SECRET_SMILES}`, ok: true, result }); // id with control characters
    port.emit([SECRET_SMILES, SECRET_SVG]); // not an object
    port.emit({ id: null, ok: false, error: { code: "forbidden_caller", message: SECRET_SMILES } }); // id-less error
    port.emit({ id: port.lastId(), ok: true, result: SECRET_SVG }); // malformed answer to a pending request
    void client.request("analyze", { smiles: SECRET_SMILES });
    port.drop("gone");
    void client.request("ping", {});
    ports[0]!.emit({ id: "z", ok: true, result }); // message from an old port
    await flush();
    expect(log).toHaveBeenCalled();
    const logged = anyLogged(log);
    for (const secret of ["SECRETSMILES", "SECRETSVG", "SECRETMOLFILE"]) expect(logged).not.toContain(secret);
    expect(logged).toContain("forbidden_caller"); // the error code may be logged
  });

  it("the default logger only receives summaries too", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const port = new FakePort();
    const client = new NativeClient({ connect: () => port, newId: () => "a" });
    void client.request("ping", {});
    port.emit({ id: "other", ok: true, result: { svg: SECRET_SVG } });
    port.emit({ id: "a", ok: true, result: SECRET_SMILES });
    await flush();
    expect(warn).toHaveBeenCalled();
    expect(JSON.stringify(warn.mock.calls)).not.toMatch(/SECRET/);
  });

  it("fails pending requests with the host's id-less error when the port then disconnects", async () => {
    const { client, ports } = setup();
    const a = track(client.request("register", {} as never));
    const b = track(client.request("ping", {}));
    ports[0]!.emit({ id: null, ok: false, error: { code: "frame_too_large", message: "The request is larger than 1 MB." } });
    await flush();
    expect(a.done).toBe(false); // nothing fails before the host is gone
    ports[0]!.drop(null);
    await flush();
    for (const r of [a, b]) {
      expect(r.value).toEqual({
        ok: false,
        error: { code: "frame_too_large", message: "The request is larger than 1 MB." },
      });
    }
  });

  it("keeps only the most recent well-formed id-less error", async () => {
    const { client, ports } = setup();
    const a = track(client.request("ping", {}));
    ports[0]!.emit({ id: null, ok: false, error: { code: "bad_request", message: "first" } });
    ports[0]!.emit({ id: null, ok: false, error: { code: "forbidden_caller", message: "Started by another add-on." } });
    ports[0]!.emit({ id: null, ok: false, error: { code: 5 } }); // malformed: ignored
    ports[0]!.drop("exited");
    await flush();
    expect((a.value as Extract<BackgroundReply, { ok: false }>).error).toEqual({
      code: "forbidden_caller",
      message: "Started by another add-on.",
    });
  });

  it("forgets the id-less error when a new port is opened", async () => {
    const { client, ports } = setup();
    void client.request("ping", {});
    ports[0]!.emit({ id: null, ok: false, error: { code: "forbidden_caller", message: "x" } });
    ports[0]!.drop(null);
    const r = track(client.request("ping", {}));
    ports[1]!.drop("No such native application compound_registry");
    await flush();
    const error = (r.value as Extract<BackgroundReply, { ok: false }>).error;
    expect(error.code).toBe("host_unavailable");
    expect(error.message).toContain("No such native application");
  });

  it("uses the generic error when no id-less error came", async () => {
    const { client, ports } = setup();
    const r = track(client.request("ping", {}));
    ports[0]!.drop("crashed");
    await flush();
    expect((r.value as Extract<BackgroundReply, { ok: false }>).error.code).toBe("host_unavailable");
  });
});

describe("NativeClient: time limit of generate_name", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("gives generate_name and route GENERATE_NAME_TIMEOUT_MS by default and every other op the normal limit", () => {
    const client = new NativeClient({ connect: () => new FakePort() });
    expect(GENERATE_NAME_TIMEOUT_MS).toBe(60_000);
    expect(client.timeoutFor("generate_name")).toBe(GENERATE_NAME_TIMEOUT_MS);
    expect(client.timeoutFor("route")).toBe(GENERATE_NAME_TIMEOUT_MS);
    for (const op of ["ping", "analyze", "register", "find", "compounds", "set_name", "retire", "set_reaction"] as const) {
      expect(client.timeoutFor(op)).toBe(DEFAULT_TIMEOUT_MS);
    }
    expect(OP_TIMEOUTS_MS).toEqual({ generate_name: GENERATE_NAME_TIMEOUT_MS, route: GENERATE_NAME_TIMEOUT_MS });
  });

  it("a generate_name request is still waiting after 30 s and times out only after 60 s", async () => {
    const { client, ports } = setup({ timeoutMs: 30_000 });
    const name = track(client.request("generate_name", { core_stereo_id: "000042_01", replace: false }));
    const ping = track(client.request("ping", {}));
    await vi.advanceTimersByTimeAsync(30_000);
    expect((ping.value as Extract<BackgroundReply, { ok: false }>).error.code).toBe("host_timeout");
    expect(name.done).toBe(false);
    await vi.advanceTimersByTimeAsync(29_999);
    expect(name.done).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    const error = (name.value as Extract<BackgroundReply, { ok: false }>).error;
    expect(error.code).toBe("host_timeout");
    expect(error.message).toContain("60 seconds");
    expect(ports[0]!.sent[0]).toMatchObject({ op: "generate_name", params: { core_stereo_id: "000042_01", replace: false } });
  });

  it("an answer within the longer limit is delivered", async () => {
    const { client, ports } = setup();
    const name = track(client.request("generate_name", { core_stereo_id: "000042_01" }));
    const id = ports[0]!.lastId();
    await vi.advanceTimersByTimeAsync(45_000);
    ports[0]!.emit({ id, ok: true, result: { core_stereo_id: "000042_01", generated: true } });
    await flush();
    expect(name.value).toEqual({ ok: true, result: { core_stereo_id: "000042_01", generated: true } });
  });

  it("per-op limits can be set", async () => {
    const client = new NativeClient({ connect: () => new FakePort(), timeoutMs: 1000, opTimeoutsMs: { generate_name: 5000 } });
    expect(client.timeoutFor("generate_name")).toBe(5000);
    expect(client.timeoutFor("analyze")).toBe(1000);
  });
});
