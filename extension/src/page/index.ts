/**
 * Entry of dist/page-bridge.js (content script with `"world": "MAIN"`, all
 * frames). Only adds one event listener; everything else happens on request.
 */

import { installBridge, type BridgeWindow } from "./bridge";

try {
  installBridge(window as unknown as BridgeWindow);
} catch {
  // never throw into the page
}
