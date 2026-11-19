/**
 * Entry of the extension's compounds page (compounds.html, opened in a tab by
 * the background). The page talks to the registry only through the background
 * (`browser.runtime.sendMessage`), which serves it PAGE_OPS. The import opens
 * experiments in a tab of its own and talks to the content script there
 * (`browser.tabs`, see src/shared/import.ts).
 */

import type { BackgroundRequest } from "../shared/protocol";
import { createRuntimeHostClient } from "../content/host-client";
import { browserImportTabs } from "./import";
import { createOclEditor } from "./ocl-editor";
import { mountCompoundsPage } from "./page";

const root = document.getElementById("app");
if (root) {
  mountCompoundsPage({
    doc: document,
    root,
    client: createRuntimeHostClient((message: BackgroundRequest) => browser.runtime.sendMessage(message)),
    createEditor: createOclEditor,
    tabs: browserImportTabs(browser.tabs),
  });
}
