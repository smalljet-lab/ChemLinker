/**
 * Entry of the toolbar popup (popup.html). It talks to the background only
 * (`browser.runtime.sendMessage`), which serves it two messages.
 */

import { mountPopup } from "./popup";

const root = document.getElementById("app");
if (root) {
  mountPopup({
    doc: document,
    root,
    send: (message) => browser.runtime.sendMessage(message),
    close: () => window.close(),
  });
}
