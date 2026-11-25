/**
 * Entry of the extension's capture page (capture.html?id=..., opened in a tab
 * by the background after it took the screenshot, or capture.html?failed=...
 * when the keyboard shortcut could not take one). The page talks to the
 * background only (`browser.runtime.sendMessage`), which serves it the
 * screenshot and CAPTURE_OPS.
 */

import { createOclEditor, renderMolfileSvg } from "../compounds/ocl-editor";
import { createCaptureClient } from "./client";
import { createCanvasImaging } from "./imaging";
import { mountCapturePage } from "./page";

const root = document.getElementById("app");
if (root) {
  const params = new URLSearchParams(window.location.search);
  mountCapturePage({
    doc: document,
    root,
    client: createCaptureClient((message) => browser.runtime.sendMessage(message)),
    imaging: createCanvasImaging(document),
    createEditor: createOclEditor,
    renderStructure: renderMolfileSvg,
    captureId: params.get("id"),
    failed: params.get("failed"),
  });
}
