/**
 * Content-script side of the page bridge (`shared/bridge.ts`): sends request
 * events on `document` and waits for the matching response event.
 *
 * Everything that comes back is untrusted text: the page can see and forge
 * these events. A molfile is only ever sent to the host, which parses it.
 */

import {
  BRIDGE_REQUEST_EVENT,
  BRIDGE_RESPONSE_EVENT,
  type BridgeErrorCode,
  type BridgeRequest,
  type PageReaction,
} from "../shared/bridge";
import { MAX_STRUCTURE_CHARS } from "../shared/protocol";
import { readPageReaction } from "./reaction";

export type MolfileResult =
  | { ok: true; molfile: string }
  | { ok: false; code: BridgeErrorCode | "no_answer"; message: string };

export type ReactionResult =
  | { ok: true; reaction: PageReaction }
  | { ok: false; code: BridgeErrorCode | "no_answer"; message: string };

export interface BridgeClient {
  /** true: bridge loaded and ZK present; false: no ZK in this frame; null: no answer in time. */
  hello(timeoutMs: number): Promise<boolean | null>;
  molfile(domId: string, timeoutMs: number): Promise<MolfileResult>;
  dispose(): void;
}

/** The bridge client of the page: also reads the experiment's reaction (`reaction` request). */
export interface DomBridgeClient extends BridgeClient {
  reaction(timeoutMs: number): Promise<ReactionResult>;
}

const ERROR_CODES: readonly BridgeErrorCode[] = [
  "bad_request",
  "no_zk",
  "no_widget",
  "timeout",
  "fetch_failed",
  "too_large",
  "unexpected",
];
const MAX_MESSAGE = 500;

type Parsed =
  | { id: string; ok: true; kind: "hello"; zk: boolean }
  | { id: string; ok: true; kind: "molfile"; molfile: string }
  | { id: string; ok: true; kind: "reaction"; reaction: PageReaction }
  | { id: string; ok: false; code: BridgeErrorCode; message: string };

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Parses a response event's detail; null when it is not a valid response. */
export function parseBridgeResponse(detail: unknown): Parsed | null {
  if (typeof detail !== "string" || detail.length > MAX_STRUCTURE_CHARS * 2 + 1000) return null;
  let v: unknown;
  try {
    v = JSON.parse(detail);
  } catch {
    return null;
  }
  if (!isRecord(v) || typeof v.id !== "string" || v.id === "") return null;
  if (v.ok === true && v.kind === "hello" && typeof v.zk === "boolean") return { id: v.id, ok: true, kind: "hello", zk: v.zk };
  if (v.ok === true && v.kind === "molfile" && typeof v.molfile === "string") {
    return { id: v.id, ok: true, kind: "molfile", molfile: v.molfile };
  }
  if (v.ok === true && v.kind === "reaction") {
    const reaction = readPageReaction(v.reaction);
    return reaction === null ? null : { id: v.id, ok: true, kind: "reaction", reaction };
  }
  if (v.ok === false && typeof v.code === "string" && typeof v.message === "string") {
    const code = (ERROR_CODES as readonly string[]).includes(v.code) ? (v.code as BridgeErrorCode) : "unexpected";
    return { id: v.id, ok: false, code, message: v.message.slice(0, MAX_MESSAGE) };
  }
  return null;
}

export interface DomBridgeOptions {
  newId?: () => string;
}

export function createDomBridgeClient(doc: Document, options: DomBridgeOptions = {}): DomBridgeClient {
  let counter = 0;
  const newId =
    options.newId ??
    (() => {
      counter += 1;
      const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Math.random());
      return `${random}-${counter}`;
    });
  const pending = new Map<string, (response: Parsed | null) => void>();

  const onResponse = (event: Event): void => {
    const parsed = parseBridgeResponse((event as CustomEvent).detail);
    if (!parsed) return;
    const resolve = pending.get(parsed.id);
    if (!resolve) return;
    pending.delete(parsed.id); // first answer wins; later ones are ignored
    resolve(parsed);
  };
  doc.addEventListener(BRIDGE_RESPONSE_EVENT, onResponse);

  function send(request: BridgeRequest, timeoutMs: number): Promise<Parsed | null> {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (pending.delete(request.id)) resolve(null);
      }, timeoutMs);
      pending.set(request.id, (response) => {
        clearTimeout(timer);
        resolve(response);
      });
      try {
        const view = doc.defaultView;
        const Ctor = (view?.CustomEvent ?? CustomEvent) as typeof CustomEvent;
        doc.dispatchEvent(new Ctor(BRIDGE_REQUEST_EVENT, { detail: JSON.stringify(request) }));
      } catch {
        if (pending.delete(request.id)) {
          clearTimeout(timer);
          resolve(null);
        }
      }
    });
  }

  return {
    async hello(timeoutMs) {
      const response = await send({ id: newId(), kind: "hello" }, timeoutMs);
      if (response === null) return null;
      if (response.ok && response.kind === "hello") return response.zk;
      return null;
    },
    async molfile(domId, timeoutMs) {
      const response = await send({ id: newId(), kind: "molfile", domId }, timeoutMs);
      if (response === null) {
        return { ok: false, code: "no_answer", message: "The page did not answer in time." };
      }
      if (!response.ok) return { ok: false, code: response.code, message: response.message };
      if (response.kind !== "molfile") return { ok: false, code: "unexpected", message: "Unexpected answer from the page." };
      if (response.molfile.trim() === "") return { ok: false, code: "unexpected", message: "The page returned an empty structure." };
      if (response.molfile.length > MAX_STRUCTURE_CHARS) {
        return { ok: false, code: "too_large", message: "The structure is too large." };
      }
      return { ok: true, molfile: response.molfile };
    },
    async reaction(timeoutMs) {
      const response = await send({ id: newId(), kind: "reaction" }, timeoutMs);
      if (response === null) {
        return { ok: false, code: "no_answer", message: "The page did not answer in time." };
      }
      if (!response.ok) return { ok: false, code: response.code, message: response.message };
      if (response.kind !== "reaction") return { ok: false, code: "unexpected", message: "Unexpected answer from the page." };
      return { ok: true, reaction: response.reaction };
    },
    dispose() {
      doc.removeEventListener(BRIDGE_RESPONSE_EVENT, onResponse);
      for (const resolve of pending.values()) resolve(null);
      pending.clear();
    },
  };
}

/**
 * The same client with every `molfile` request queued behind the previous
 * one. The product source and the reaction capture share it, so this side
 * never has two molfile requests open at once (the bridge's download hook is
 * global; the bridge queues as well).
 */
export function queueMolfiles(client: DomBridgeClient): DomBridgeClient {
  let chain: Promise<unknown> = Promise.resolve();
  return {
    hello: (timeoutMs) => client.hello(timeoutMs),
    reaction: (timeoutMs) => client.reaction(timeoutMs),
    dispose: () => client.dispose(),
    molfile(domId, timeoutMs) {
      const next = chain.then(() => client.molfile(domId, timeoutMs));
      chain = next.catch(() => undefined);
      return next.catch((error: unknown) => ({ ok: false as const, code: "unexpected" as const, message: String(error) }));
    },
  };
}
