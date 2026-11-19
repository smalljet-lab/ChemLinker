/**
 * Background script: Firefox's event page (dist/) and Chrome's service worker
 * (chrome-extension/, where the build defines `browser` as Chrome's
 * promise-returning `chrome`). Listeners are registered synchronously at top
 * level because both browsers stop the script when idle and restart it on the
 * next event.
 */

import { HOST_NAME } from "../shared/protocol";
import { compoundsPageUrl, createPageOpener, registerToolbarButton } from "./compounds-page";
import { createMessageHandler, type SenderLike } from "./message-handler";
import { NativeClient, errorText, type NativePort } from "./native-client";

/** Named in "could not be reached" errors; only Firefox has runtime.getBrowserInfo. */
const browserName = typeof browser.runtime.getBrowserInfo === "function" ? "Firefox" : "Chrome";

const client = new NativeClient({
  connect: () => browser.runtime.connectNative(HOST_NAME) as unknown as NativePort,
  lastErrorMessage: () => browser.runtime.lastError?.message,
  browserName,
});

const openCompoundsPage = createPageOpener({ runtime: browser.runtime, tabs: browser.tabs });

const handleMessage = createMessageHandler({
  runtimeId: browser.runtime.id,
  pageUrl: compoundsPageUrl({ runtime: browser.runtime }),
  request: (op, params) => client.request(op, params),
  openCompoundsPage,
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

registerToolbarButton(browser.action, openCompoundsPage);

// Firefox fires onSuspend before it unloads the event page; Chrome may stop a
// service worker without it (the open host port then closes with the worker).
browser.runtime.onSuspend?.addListener(() => {
  client.close();
});
