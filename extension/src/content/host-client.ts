/**
 * The panel's only way to reach the host: `browser.runtime.sendMessage` to the
 * background. The panel depends on the `HostClient` interface so tests inject
 * a fake.
 *
 * No time limit of its own: `runtime.sendMessage` waits for the background,
 * whose native client ends every request (`host_timeout` after its limit;
 * GENERATE_NAME_TIMEOUT_MS for `generate_name`, which waits for ChemDraw).
 */

import {
  PROTOCOL_VERSION,
  type BackgroundReply,
  type BackgroundRequest,
  type HostError,
  type OpenCompoundsRequest,
  type Op,
  type ParamsByOp,
} from "../shared/protocol";
import { isHostError, isRecord, isResultOf, readReply } from "./validate";

export interface HostClient {
  request<K extends Op>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>>;
}

function errorText(value: unknown): string {
  if (typeof value === "object" && value !== null && "message" in value) {
    const message = (value as { message: unknown }).message;
    if (typeof message === "string" && message) return message;
  }
  return String(value);
}

export type OpenReply = { ok: true } | { ok: false; error: HostError };

/**
 * Asks the background to open the extension's page with all compounds in a
 * new tab (the panel's "All compounds" link). Never rejects.
 * @param send `(message) => browser.runtime.sendMessage(message)`
 */
export async function requestOpenCompounds(send: (message: OpenCompoundsRequest) => Promise<unknown>): Promise<OpenReply> {
  let raw: unknown;
  try {
    raw = await send({ type: "open-compounds" });
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "host_unavailable",
        message:
          `The extension's background script did not answer (${errorText(error)}). ` +
          "Reload this page; if that does not help, reload the extension.",
      },
    };
  }
  if (isRecord(raw) && raw.ok === true) return { ok: true };
  if (isRecord(raw) && raw.ok === false && isHostError(raw.error)) {
    return { ok: false, error: { code: raw.error.code, message: raw.error.message } };
  }
  return {
    ok: false,
    error: { code: "host_bad_response", message: "The extension's background script sent an unreadable answer." },
  };
}

/**
 * @param send `(message) => browser.runtime.sendMessage(message)`
 */
export function createRuntimeHostClient(send: (message: BackgroundRequest) => Promise<unknown>): HostClient {
  return {
    async request<K extends Op>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>> {
      const message: BackgroundRequest<K> = { type: "host-request", op, params };
      let raw: unknown;
      try {
        raw = await send(message as BackgroundRequest);
      } catch (error) {
        return {
          ok: false,
          error: {
            code: "host_unavailable",
            message:
              `The extension's background script did not answer (${errorText(error)}). ` +
              "Reload this page; if that does not help, reload the extension.",
          },
        };
      }
      const reply = readReply(raw);
      if (reply === null) {
        return {
          ok: false,
          error: { code: "host_bad_response", message: "The extension's background script sent an unreadable answer." },
        };
      }
      if (reply.ok && !isResultOf(op, reply.result, PROTOCOL_VERSION)) {
        return {
          ok: false,
          error: {
            code: "host_bad_response",
            message: `The local registry program sent a '${op}' answer that this extension does not understand.`,
          },
        };
      }
      return reply as BackgroundReply<K>;
    },
  };
}
