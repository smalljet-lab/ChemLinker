/**
 * Serves messages from exactly two kinds of senders and refuses everything
 * else with `not_allowed`:
 *
 *   the site's content script  (sender.url on SITE_ORIGIN): SITE_OPS, and
 *                              `open-compounds` (the panel's "All compounds" link);
 *   the compounds page         (sender.url is this extension's COMPOUNDS_PAGE): PAGE_OPS.
 *
 * Answers every message with a value and never rejects.
 */

import {
  PAGE_OPS,
  type BackgroundReply,
  type BackgroundRequest,
  type HostError,
  type Op,
  type ParamsByOp,
} from "../shared/protocol";
import { SITE_ORIGIN } from "../shared/site";
import { errorText } from "./native-client";

/**
 * The operations the background forwards to the host for the site's content
 * script. `retire` is part of the protocol but never served: IDs are retired
 * with the command-line tool only (`compound-registry retire`).
 * `compounds` belongs to the compounds page only. `generate_name` asks the
 * naming program on this PC (through the host) for the name of one CORE_STEREO.
 * `set_yield` stores the yields the experiment page shows with a product's ID
 * (the host refuses values read from another experiment or product row);
 * the table of all yields (`yields`) belongs to the compounds page only.
 * `set_reaction` stores what the experiment page shows about its reaction (the
 * host refuses it for an experiment without a registered product); drawing a
 * route (`route`) belongs to the compounds page only.
 */
export const SITE_OPS: readonly Op[] = [
  "ping", "analyze", "register", "find", "set_name", "generate_name", "set_yield", "set_reaction",
];

/** The fields of `runtime.MessageSender` the handler looks at. */
export interface SenderLike {
  id?: string | undefined;
  url?: string | undefined;
}

export type SenderKind = "site" | "page";

/** What the background answers to `open-compounds`. */
export type OpenCompoundsReply = { ok: true } | { ok: false; error: HostError };

export interface MessageHandlerDeps {
  runtimeId: string;
  /** `browser.runtime.getURL(COMPOUNDS_PAGE)`. */
  pageUrl: string;
  request: <K extends Op>(op: K, params: ParamsByOp[K]) => Promise<BackgroundReply<K>>;
  /** Opens COMPOUNDS_PAGE in a new tab. */
  openCompoundsPage: () => Promise<void>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function originOf(url: unknown): string | null {
  if (typeof url !== "string") return null;
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/** The URL without its query and fragment. */
function withoutQuery(url: string): string {
  const cut = url.search(/[?#]/);
  return cut < 0 ? url : url.slice(0, cut);
}

/** Who sent the message, or null when it is neither the site's content script nor the compounds page. */
export function senderKind(sender: SenderLike | undefined, runtimeId: string, pageUrl: string): SenderKind | null {
  if (!sender || sender.id !== runtimeId || typeof sender.url !== "string") return null;
  if (originOf(sender.url) === SITE_ORIGIN) return "site";
  if (pageUrl !== "" && withoutQuery(sender.url) === pageUrl) return "page";
  return null;
}

/** Why a message is refused, or null when it is served. */
export function refusalReason(
  message: unknown,
  sender: SenderLike | undefined,
  runtimeId: string,
  pageUrl: string = "",
): string | null {
  if (!sender || sender.id !== runtimeId) return "The message does not come from this extension.";
  const kind = senderKind(sender, runtimeId, pageUrl);
  if (kind === null) return "The message does not come from the Sciformation site or the extension's compounds page.";
  if (!isRecord(message)) return "Unknown message type.";
  if (message.type === "open-compounds") {
    return kind === "site" ? null : "This message is not available here.";
  }
  if (message.type !== "host-request") return "Unknown message type.";
  const ops = kind === "site" ? SITE_OPS : PAGE_OPS;
  if (typeof message.op !== "string" || !(ops as readonly string[]).includes(message.op)) return "This operation is not available.";
  if (!isRecord(message.params)) return "The request parameters must be an object.";
  return null;
}

export function createMessageHandler(deps: MessageHandlerDeps) {
  return async function handleMessage(
    message: unknown,
    sender: SenderLike | undefined,
  ): Promise<BackgroundReply | OpenCompoundsReply> {
    try {
      const reason = refusalReason(message, sender, deps.runtimeId, deps.pageUrl);
      if (reason !== null) return { ok: false, error: { code: "not_allowed", message: reason } };
      if ((message as { type: string }).type === "open-compounds") {
        await deps.openCompoundsPage();
        return { ok: true };
      }
      const request = message as BackgroundRequest;
      return await deps.request(request.op, request.params);
    } catch (error) {
      return {
        ok: false,
        error: { code: "internal_error", message: `Unexpected error in the extension: ${errorText(error)}` },
      };
    }
  };
}
