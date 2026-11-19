/**
 * Content script entry. Runs in every frame of the Sciformation site (the
 * experiment form lives in the `contentFrame` iframe). Each frame reads its
 * own document; the panel appears only in a frame that shows an experiment.
 * The only way out of the page is `browser.runtime.sendMessage` to the
 * background, which talks to the native host. The only messages taken in are
 * the import's, from the extension's own compounds page (`import-run.ts`).
 */

import { COMPOUNDS_PAGE, type BackgroundRequest, type OpenCompoundsRequest } from "../shared/protocol";
import { createDomBridgeClient, queueMolfiles } from "./bridge-client";
import { CONTENT_FRAME_NAME, controlFrame } from "./frame";
import { createRuntimeHostClient, requestOpenCompounds } from "./host-client";
import { ImportResponder, importMessageListener } from "./import-run";
import { ManualSource } from "./manual-source";
import { storeReactionOnce } from "./reaction-capture";
import { SiteSource } from "./site-source";
import { createManualForm } from "./ui/manual-form";
import { HOST_TAG, mountPanel } from "./ui/panel";

function main(): void {
  // A previous copy of the content script (e.g. before an extension reload) may have left its panel.
  for (const old of Array.from(document.getElementsByTagName(HOST_TAG))) old.remove();

  const client = createRuntimeHostClient((message: BackgroundRequest) => browser.runtime.sendMessage(message));
  // One bridge client for the products and the reaction: molfiles are read one at a time.
  const bridge = queueMolfiles(createDomBridgeClient(document));
  const site = new SiteSource({ doc: document, bridge });
  // In the ELN's content frame the launcher is always there (search by ID);
  // in every other frame only while an experiment is shown.
  const alwaysMounted = window.name === CONTENT_FRAME_NAME;
  controlFrame(site, () => {
    const manualSource = new ManualSource();
    return mountPanel({
      doc: document,
      source: site,
      client,
      openCompounds: () => requestOpenCompounds((message: OpenCompoundsRequest) => browser.runtime.sendMessage(message)),
      manual: { source: manualSource, form: createManualForm(document, manualSource) },
      reactions: { bridge },
    });
  }, { alwaysMounted });
  site.start();
  // The import of many experiments (compounds page -> this frame, see shared/import.ts).
  // It never stops the panel from working.
  try {
    const importer = new ImportResponder({
      source: site,
      client,
      storeReaction: (experiment) => storeReactionOnce({ bridge, client, experiment }),
      runtimeId: browser.runtime.id,
      pageUrl: browser.runtime.getURL(COMPOUNDS_PAGE),
    });
    browser.runtime.onMessage.addListener(importMessageListener(importer));
  } catch {
    // No import from this frame.
  }
}

main();
