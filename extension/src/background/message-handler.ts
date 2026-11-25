/**
 * Serves messages from exactly four kinds of senders and refuses everything
 * else with `not_allowed`:
 *
 *   the site's content script  (sender.url on SITE_ORIGIN): SITE_OPS, and
 *                              `open-compounds` (the panel's "All compounds" link);
 *   the compounds page         (sender.url is this extension's COMPOUNDS_PAGE): PAGE_OPS;
 *   the capture page           (sender.url is this extension's CAPTURE_PAGE): CAPTURE_OPS,
 *                              and `capture-image` (the screenshot it was opened for);
 *   the toolbar popup          (sender.url is this extension's POPUP_PAGE): only the two
 *                              messages `capture-start` and `open-compounds`, no host operation.
 *
 * CAPTURE_OPS go to the host through a native-messaging port of their own
 * (`captureRequest`), so a recognition never holds up the registry operations.
 *
 * Answers every message with a value and never rejects.
 */

import {
  CAPTURE_OPS,
  PAGE_OPS,
  type BackgroundReply,
  type BackgroundRequest,
  type CaptureImageReply,
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

export type SenderKind = "site" | "page" | "capture" | "popup";

/** The message types that are not host requests, and who may send each. */
export const MESSAGE_SENDERS: Readonly<Record<string, readonly SenderKind[]>> = {
  "open-compounds": ["site", "popup"],
  "capture-start": ["popup"],
  "capture-image": ["capture"],
};

/** The host operations of each kind of sender. The popup has none. */
export const OPS_BY_SENDER: Readonly<Record<SenderKind, readonly string[]>> = {
  site: SITE_OPS,
  page: PAGE_OPS,
  capture: CAPTURE_OPS,
  popup: [],
};

/** What the background answers to `open-compounds` and `capture-start`. */
export type OpenCompoundsReply = { ok: true } | { ok: false; error: HostError };

export interface MessageHandlerDeps {
  runtimeId: string;
  /** `browser.runtime.getURL(COMPOUNDS_PAGE)`. */
  pageUrl: string;
  /** `browser.runtime.getURL(CAPTURE_PAGE)`. */
  captureUrl: string;
  /** `browser.runtime.getURL(POPUP_PAGE)`. */
  popupUrl: string;
  /** The registry operations (SITE_OPS, PAGE_OPS). */
  request: <K extends Op>(op: K, params: ParamsByOp[K]) => Promise<BackgroundReply<K>>;
  /** CAPTURE_OPS, on a host connection of their own. */
  captureRequest: <K extends Op>(op: K, params: ParamsByOp[K]) => Promise<BackgroundReply<K>>;
  /** Opens COMPOUNDS_PAGE in a new tab. */
  openCompoundsPage: () => Promise<void>;
  /** Takes the screenshot of the active tab and opens CAPTURE_PAGE for it. */
  startCapture: () => Promise<OpenCompoundsReply>;
  /** The screenshot kept under an id, once. */
  takeScreenshot: (id: unknown) => CaptureImageReply;
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

/**
 * Who sent the message, or null when it is none of the four senders. The three
 * extension pages are told apart by their exact address (query and fragment
 * aside); an empty address matches nothing.
 */
export function senderKind(
  sender: SenderLike | undefined,
  runtimeId: string,
  pageUrl: string,
  captureUrl: string = "",
  popupUrl: string = "",
): SenderKind | null {
  if (!sender || sender.id !== runtimeId || typeof sender.url !== "string") return null;
  if (originOf(sender.url) === SITE_ORIGIN) return "site";
  const url = withoutQuery(sender.url);
  if (pageUrl !== "" && url === pageUrl) return "page";
  if (captureUrl !== "" && url === captureUrl) return "capture";
  if (popupUrl !== "" && url === popupUrl) return "popup";
  return null;
}

/** Why a message is refused, or null when it is served. */
export function refusalReason(
  message: unknown,
  sender: SenderLike | undefined,
  runtimeId: string,
  pageUrl: string = "",
  captureUrl: string = "",
  popupUrl: string = "",
): string | null {
  if (!sender || sender.id !== runtimeId) return "The message does not come from this extension.";
  const kind = senderKind(sender, runtimeId, pageUrl, captureUrl, popupUrl);
  if (kind === null) return "The message does not come from the Sciformation site or one of the extension's own pages.";
  if (!isRecord(message)) return "Unknown message type.";
  if (typeof message.type === "string" && Object.hasOwn(MESSAGE_SENDERS, message.type)) {
    if (!MESSAGE_SENDERS[message.type]!.includes(kind)) return "This message is not available here.";
    if (message.type === "capture-image" && typeof message.id !== "string") return "The screenshot id is missing.";
    return null;
  }
  if (message.type !== "host-request") return "Unknown message type.";
  if (typeof message.op !== "string" || !OPS_BY_SENDER[kind].includes(message.op)) return "This operation is not available.";
  if (!isRecord(message.params)) return "The request parameters must be an object.";
  return null;
}

export function createMessageHandler(deps: MessageHandlerDeps) {
  return async function handleMessage(
    message: unknown,
    sender: SenderLike | undefined,
  ): Promise<BackgroundReply | OpenCompoundsReply | CaptureImageReply> {
    try {
      const reason = refusalReason(message, sender, deps.runtimeId, deps.pageUrl, deps.captureUrl, deps.popupUrl);
      if (reason !== null) return { ok: false, error: { code: "not_allowed", message: reason } };
      const type = (message as { type: string }).type;
      if (type === "open-compounds") {
        await deps.openCompoundsPage();
        return { ok: true };
      }
      if (type === "capture-start") return await deps.startCapture();
      if (type === "capture-image") return deps.takeScreenshot((message as { id: unknown }).id);
      const request = message as BackgroundRequest;
      // Only the capture page gets this far with a capture operation; those have a host connection of their own.
      const viaCapture = (CAPTURE_OPS as readonly string[]).includes(request.op);
      return await (viaCapture ? deps.captureRequest : deps.request)(request.op, request.params);
    } catch (error) {
      return {
        ok: false,
        error: { code: "internal_error", message: `Unexpected error in the extension: ${errorText(error)}` },
      };
    }
  };
}
