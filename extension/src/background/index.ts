/**
 * Background script: Firefox's event page (dist/) and Chrome's service worker
 * (chrome-extension/, where the build defines `browser` as Chrome's
 * promise-returning `chrome`). Listeners are registered synchronously at top
 * level because both browsers stop the script when idle and restart it on the
 * next event.
 */

import { CAPTURE_PAGE, HOST_NAME, POPUP_PAGE } from "../shared/protocol";
import {
  ScreenshotStore,
  activeTabIsPrivate,
  createCaptureStarter,
  createExplanationOpener,
  registerCaptureCommand,
  takeScreenshot,
  type CaptureApi,
  type CommandsApi,
} from "./capture";
import { compoundsPageUrl, createPageOpener } from "./compounds-page";
import { createMessageHandler, type SenderLike } from "./message-handler";
import { CAPTURE_IDLE_CLOSE_MS, CAPTURE_OP_TIMEOUTS_MS, NativeClient, errorText, type NativePort } from "./native-client";

/** Named in "could not be reached" errors; only Firefox has runtime.getBrowserInfo. */
const browserName = typeof browser.runtime.getBrowserInfo === "function" ? "Firefox" : "Chrome";

const connect = () => browser.runtime.connectNative(HOST_NAME) as unknown as NativePort;
const lastErrorMessage = () => browser.runtime.lastError?.message;

/** The registry operations. */
const client = new NativeClient({ connect, lastErrorMessage, browserName });

/**
 * "Capture reaction": a second connection, so a second host process. The host
 * answers one request at a time and a recognition takes up to minutes; the
 * panel's lookups must not wait for it. Closed when unused, which ends that
 * host process and its recognition worker.
 */
const captureClient = new NativeClient({
  connect,
  lastErrorMessage,
  browserName,
  opTimeoutsMs: CAPTURE_OP_TIMEOUTS_MS,
  idleCloseMs: CAPTURE_IDLE_CLOSE_MS,
  // All capture pages share this port: a short request of one page may wait behind another page's recognition.
  queued: true,
});

const openCompoundsPage = createPageOpener({ runtime: browser.runtime, tabs: browser.tabs });

const screenshots = new ScreenshotStore();
const captureApi = { runtime: browser.runtime, tabs: browser.tabs } as unknown as CaptureApi;
const startCapture = createCaptureStarter(captureApi, screenshots, {
  browserName,
  // Chrome cannot show the capture page in an Incognito window; Firefox can.
  ...(browserName === "Chrome" ? { isPrivateTab: () => activeTabIsPrivate(captureApi) } : {}),
});

const handleMessage = createMessageHandler({
  runtimeId: browser.runtime.id,
  pageUrl: compoundsPageUrl({ runtime: browser.runtime }),
  captureUrl: browser.runtime.getURL(CAPTURE_PAGE),
  popupUrl: browser.runtime.getURL(POPUP_PAGE),
  request: (op, params) => client.request(op, params),
  captureRequest: (op, params) => captureClient.request(op, params),
  openCompoundsPage,
  startCapture,
  takeScreenshot: (id) => takeScreenshot(screenshots, id),
});

/**
 * Answers through `sendResponse` and returns `true` (the answer comes later).
 * Chrome does not reliably take a Promise returned from an onMessage listener;
 * both browsers take this form.
 */
function onRuntimeMessage(message: unknown, sender: SenderLike | undefined, sendResponse: (reply: unknown) => void): true {
  const reply = (value: unknown) => {
    try {
      sendResponse(value);
    } catch {
      // The sender is gone (tab closed or navigated); nobody waits for the answer.
    }
  };
  handleMessage(message, sender).then(reply, (error: unknown) =>
    reply({ ok: false, error: { code: "internal_error", message: `Unexpected error in the extension: ${errorText(error)}` } }),
  );
  return true;
}

browser.runtime.onMessage.addListener(onRuntimeMessage);

// The toolbar button opens the popup (manifest `action.default_popup`); the
// keyboard shortcut (manifest `commands`) starts a capture directly.
registerCaptureCommand(browser.commands as CommandsApi | undefined, startCapture, createExplanationOpener(captureApi));

// Firefox fires onSuspend before it unloads the event page; Chrome may stop a
// service worker without it (the open host ports then close with the worker).
browser.runtime.onSuspend?.addListener(() => {
  client.close();
  captureClient.close();
  screenshots.clear();
});
