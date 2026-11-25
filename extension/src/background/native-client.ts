/**
 * Client for the native-messaging host.
 *
 * One port per client, opened on demand and reused. The background has two
 * clients, so two host processes: one for the registry operations and one for
 * the "Capture reaction" operations (CAPTURE_OPS). The host answers one
 * request at a time and a recognition takes up to minutes; on a port of its
 * own it never holds up the ELN panel's lookups. Every request gets a fresh id and is
 * matched to its response by that id. A request ends in exactly one
 * `BackgroundReply`: the host's answer, `host_timeout`, `host_unavailable` or
 * `host_bad_response`. The promise never rejects.
 *
 * `connect` is injected so tests can drive the client with a fake port.
 */

import {
  GENERATE_NAME_TIMEOUT_MS,
  PROTOCOL_VERSION,
  RECOGNIZE_TIMEOUT_MS,
  type BackgroundReply,
  type HostError,
  type HostRequest,
  type Op,
  type ParamsByOp,
} from "../shared/protocol";

export interface PortEvent<L> {
  addListener(listener: L): void;
}

/** The part of `runtime.Port` the client uses. */
export interface NativePort {
  postMessage(message: unknown): void;
  disconnect(): void;
  readonly error?: { message?: string } | null | undefined;
  onMessage: PortEvent<(message: unknown) => void>;
  onDisconnect: PortEvent<(port: NativePort) => void>;
}

export interface NativeClientOptions {
  /** Opens a new port to the native-messaging host HOST_NAME (the background's index.ts does that). */
  connect: () => NativePort;
  /** Per-request time limit. Default 30 s (the host loads a chemistry library at start). */
  timeoutMs?: number;
  /**
   * Time limits of single operations that may take longer than `timeoutMs`;
   * default OP_TIMEOUTS_MS (`generate_name` waits for the local naming program,
   * `route` writes and opens a ChemDraw file).
   */
  opTimeoutsMs?: Partial<Record<Op, number>>;
  /** Unique request ids; default `crypto.randomUUID()`. */
  newId?: () => string;
  /** `runtime.lastError?.message`, used when the port carries no error. */
  lastErrorMessage?: () => string | undefined;
  log?: (message: string, detail?: unknown) => void;
  /** The browser named in the host_unavailable message; default "Firefox". */
  browserName?: string;
  /**
   * Disconnects the port when no request was sent or pending for this long, so
   * the host process (and what it started) ends. Default: never; the port then
   * stays open until `close()`.
   */
  idleCloseMs?: number;
  /**
   * True when requests on this port may wait behind a long one (the capture
   * port: the host answers one request at a time and a `recognize` takes up to
   * minutes). A request's time limit then starts when the requests sent before
   * it have been answered or have run out of time at the latest, not when it is
   * sent, so a short request queued behind a recognition does not fail with
   * host_timeout while the host has not even read it. Default false: every
   * limit starts at once.
   */
  queued?: boolean;
}

export const DEFAULT_TIMEOUT_MS = 30_000;

/** Operations with their own, longer time limit. All others get `timeoutMs`. */
// `route` draws the route, writes a ChemDraw file and asks Windows to open it: a few seconds
// normally, but a long route on a busy PC gets the same generous limit as `generate_name`.
export const OP_TIMEOUTS_MS: Readonly<Partial<Record<Op, number>>> = {
  generate_name: GENERATE_NAME_TIMEOUT_MS,
  route: GENERATE_NAME_TIMEOUT_MS,
};

/**
 * Time limits of the client for the "Capture reaction" operations: `recognize`
 * waits for the models to load and for the recognition itself; the other three
 * get the default. The capture client is `queued`: all capture pages share its
 * port, so each limit starts when the requests sent before have been answered.
 */
export const CAPTURE_OP_TIMEOUTS_MS: Readonly<Partial<Record<Op, number>>> = {
  recognize: RECOGNIZE_TIMEOUT_MS,
};

/**
 * The capture client's port is closed after this long without a request. The
 * host stops its recognition worker after 10 idle minutes; one minute later
 * the host process itself is no longer needed either.
 */
export const CAPTURE_IDLE_CLOSE_MS = 11 * 60_000;

interface Pending {
  op: Op;
  /** When this request's time limit ends (ms since the epoch). */
  deadline: number;
  resolve: (reply: BackgroundReply) => void;
  timer: ReturnType<typeof setTimeout>;
}

export function errorText(value: unknown): string {
  if (typeof value === "object" && value !== null && "message" in value) {
    const message = (value as { message: unknown }).message;
    if (typeof message === "string" && message) return message;
  }
  if (typeof value === "string" && value) return value;
  return "unknown reason";
}

function failure(code: string, message: string): BackgroundReply {
  return { ok: false, error: { code, message } };
}

export function unavailableReply(reason: string, browserName = "Firefox"): BackgroundReply {
  return failure(
    "host_unavailable",
    `The local registry program could not be reached (${browserName} reports: ${reason}).`,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readHostError(value: unknown): HostError | null {
  if (!isRecord(value) || typeof value.code !== "string" || typeof value.message !== "string") return null;
  const error: HostError = { code: value.code, message: value.message };
  if (typeof value.existing_id === "string") error.existing_id = value.existing_id;
  return error;
}

/**
 * What may be logged about a host message: never the message itself, which
 * can contain structures (molfile, SMILES, SVG) or experiment data. Only the
 * id (when it is a short id), the ok flag and the error code.
 */
export interface MessageSummary {
  id: string | number | null;
  ok: boolean | null;
  code: string | null;
}

const MAX_LOGGED_TEXT = 64;

function hasControlChars(text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c < 0x20 || c === 0x7f) return true;
  }
  return false;
}

function shortText(value: unknown): string | null {
  return typeof value === "string" && value.length <= MAX_LOGGED_TEXT && !hasControlChars(value)
    ? value
    : null;
}

export function summarizeMessage(message: unknown): MessageSummary {
  if (!isRecord(message)) return { id: null, ok: null, code: null };
  const id = message.id;
  return {
    id: typeof id === "number" && Number.isFinite(id) ? id : shortText(id),
    ok: typeof message.ok === "boolean" ? message.ok : null,
    code: isRecord(message.error) ? shortText(message.error.code) : null,
  };
}

export class NativeClient {
  private port: NativePort | null = null;
  /**
   * The latest well-formed error the current port sent without an id. The host
   * sends such an error for fatal problems (frame_too_large, forbidden_caller)
   * and then exits; pending requests then fail with it instead of the generic
   * host_unavailable.
   */
  private portFatalError: HostError | null = null;
  private readonly pending = new Map<string, Pending>();
  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly timeoutMs: number;
  private readonly opTimeoutsMs: Readonly<Partial<Record<Op, number>>>;
  private readonly newId: () => string;
  private readonly log: (message: string, detail?: unknown) => void;

  constructor(private readonly options: NativeClientOptions) {
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.opTimeoutsMs = options.opTimeoutsMs ?? OP_TIMEOUTS_MS;
    this.newId = options.newId ?? (() => crypto.randomUUID());
    this.log = options.log ?? ((message, detail) => console.warn(`[compound-registry] ${message}`, detail ?? ""));
  }

  /** Number of requests waiting for an answer (for tests and diagnostics). */
  get pendingCount(): number {
    return this.pending.size;
  }

  get connected(): boolean {
    return this.port !== null;
  }

  /** The time limit of one request of `op`. */
  timeoutFor(op: Op): number {
    return this.opTimeoutsMs[op] ?? this.timeoutMs;
  }

  request<K extends Op>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>> {
    return new Promise<BackgroundReply<K>>((resolveTyped) => {
      const resolve = resolveTyped as (reply: BackgroundReply) => void;
      let port: NativePort;
      try {
        port = this.ensurePort();
      } catch (error) {
        resolve(unavailableReply(errorText(error), this.options.browserName));
        return;
      }
      this.stopIdleTimer();
      let id = this.newId();
      while (this.pending.has(id)) id = this.newId();
      const timeoutMs = this.timeoutFor(op);
      // On a queued port the host first works off what was sent before.
      const waitMs = this.options.queued ? this.queueWaitMs() : 0;
      const timer = setTimeout(() => {
        if (this.pending.delete(id)) {
          this.log(`request ${id} (${op}) timed out`);
          resolve(
            failure(
              "host_timeout",
              waitMs > 0
                ? `The local registry program was busy with an earlier request and did not answer within ${Math.round((waitMs + timeoutMs) / 1000)} seconds.`
                : `The local registry program did not answer within ${Math.round(timeoutMs / 1000)} seconds.`,
            ),
          );
          this.startIdleTimer();
        }
      }, waitMs + timeoutMs);
      this.pending.set(id, { op, resolve, timer, deadline: Date.now() + waitMs + timeoutMs });
      const message: HostRequest<K> = { id, v: PROTOCOL_VERSION, op, params };
      try {
        port.postMessage(message);
      } catch (error) {
        // The port is unusable: fail this request and every other one on it.
        this.dropPort(port, errorText(error), true);
      }
    });
  }

  /** How long the requests already sent may still take, at most. */
  private queueWaitMs(): number {
    let latest = 0;
    for (const entry of this.pending.values()) latest = Math.max(latest, entry.deadline);
    return Math.max(0, latest - Date.now());
  }

  /** Disconnects the port (event page suspend); pending requests fail. */
  close(reason = "the extension's background page was suspended"): void {
    if (this.port) this.dropPort(this.port, reason);
  }

  private ensurePort(): NativePort {
    if (this.port) return this.port;
    const port = this.options.connect();
    this.port = port;
    this.portFatalError = null;
    port.onMessage.addListener((message) => this.onMessage(port, message));
    port.onDisconnect.addListener((p) => this.onDisconnect(port, p));
    return port;
  }

  private settle(id: string, reply: BackgroundReply): void {
    const entry = this.pending.get(id);
    if (!entry) return;
    this.pending.delete(id);
    clearTimeout(entry.timer);
    entry.resolve(reply);
    this.startIdleTimer();
  }

  private stopIdleTimer(): void {
    if (this.idleTimer !== null) clearTimeout(this.idleTimer);
    this.idleTimer = null;
  }

  /** Starts the idle countdown when nothing is pending on an open port. */
  private startIdleTimer(): void {
    this.stopIdleTimer();
    const idleCloseMs = this.options.idleCloseMs;
    if (idleCloseMs === undefined || this.port === null || this.pending.size > 0) return;
    this.idleTimer = setTimeout(() => {
      this.idleTimer = null;
      if (this.pending.size === 0) this.close("it was not used for a while");
    }, idleCloseMs);
    // Under Node (tests) a waiting timer must not keep the process alive; browsers return a number.
    (this.idleTimer as { unref?: () => void }).unref?.();
  }

  private failAll(reply: BackgroundReply): void {
    for (const id of [...this.pending.keys()]) this.settle(id, reply);
  }

  /** The reply for requests that die with the port. */
  private portGoneReply(reason: string, preferHostError: boolean): BackgroundReply {
    const fatal = preferHostError ? this.portFatalError : null;
    this.portFatalError = null;
    return fatal ? { ok: false, error: fatal } : unavailableReply(reason, this.options.browserName);
  }

  private dropPort(port: NativePort, reason: string, preferHostError = false): void {
    if (this.port !== port) return;
    this.port = null;
    this.stopIdleTimer();
    try {
      port.disconnect();
    } catch {
      // already disconnected
    }
    this.failAll(this.portGoneReply(reason, preferHostError));
  }

  private onMessage(port: NativePort, message: unknown): void {
    if (port !== this.port) {
      this.log("ignored a message from an old port", summarizeMessage(message));
      return;
    }
    if (!isRecord(message)) {
      this.log("ignored a host message that is not an object", summarizeMessage(message));
      return;
    }
    const id = message.id;
    if (id === null && message.ok === false) {
      const error = readHostError(message.error);
      if (error) {
        this.portFatalError = error;
        this.log("host error without a request id", summarizeMessage(message));
        return;
      }
    }
    if (typeof id !== "string" || !this.pending.has(id)) {
      this.log("ignored a host message with an unknown id", summarizeMessage(message));
      return;
    }
    if (message.ok === true && isRecord(message.result)) {
      this.settle(id, { ok: true, result: message.result as never });
      return;
    }
    if (message.ok === false) {
      const error = readHostError(message.error);
      if (error) {
        this.settle(id, { ok: false, error });
        return;
      }
    }
    this.log("host answer does not follow the protocol", summarizeMessage(message));
    this.settle(
      id,
      failure("host_bad_response", "The local registry program sent an answer that does not follow the protocol."),
    );
  }

  private onDisconnect(port: NativePort, eventPort: NativePort | undefined): void {
    if (port !== this.port) return;
    this.port = null;
    this.stopIdleTimer();
    const reason =
      eventPort?.error?.message || port.error?.message || this.options.lastErrorMessage?.() || "the connection was closed";
    this.log(`native port disconnected: ${reason}`);
    this.failAll(this.portGoneReply(reason, true));
  }
}
