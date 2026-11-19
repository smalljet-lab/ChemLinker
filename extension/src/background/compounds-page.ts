/**
 * Opening the extension's compounds page: from the panel's "All compounds"
 * link (an `open-compounds` message) and from the toolbar button. Both open a
 * new tab with `tabs.create`, which needs no permission.
 */

import { COMPOUNDS_PAGE } from "../shared/protocol";

export interface PageOpenerApi {
  runtime: { getURL(path: string): string };
  tabs: { create(properties: { url: string }): Promise<unknown> };
}

export interface ToolbarAction {
  onClicked: { addListener(listener: () => void): void };
}

/**
 * The page's URL: `moz-extension://<uuid>/compounds.html` in Firefox,
 * `chrome-extension://<CHROME_EXTENSION_ID>/compounds.html` in Chrome. Both are
 * what the browser reports as `sender.url` for messages from the page.
 */
export function compoundsPageUrl(api: Pick<PageOpenerApi, "runtime">): string {
  return api.runtime.getURL(COMPOUNDS_PAGE);
}

export function createPageOpener(api: PageOpenerApi): () => Promise<void> {
  return async () => {
    await api.tabs.create({ url: compoundsPageUrl(api) });
  };
}

/** The toolbar button (manifest `action` without a popup) opens the page. */
export function registerToolbarButton(action: ToolbarAction, open: () => Promise<void>): void {
  action.onClicked.addListener(() => {
    open().catch(() => {
      // Nothing to show it in: a toolbar click has no page of its own.
    });
  });
}
