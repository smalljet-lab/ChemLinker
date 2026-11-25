/**
 * "Capture reaction", the background's part: take a screenshot of the visible
 * part of the active tab (`tabs.captureVisibleTab`, allowed by the `activeTab`
 * permission after the user opened the popup or pressed the shortcut), keep it
 * in memory under a random id and open the extension's capture page with that
 * id. The page fetches the picture once with a `capture-image` message.
 *
 * A screenshot is never written anywhere and never logged: it lives in this
 * script's memory until the capture page takes it, or for SCREENSHOT_TTL_MS.
 */

import { CAPTURE_EXPIRED, CAPTURE_FAILED, CAPTURE_PAGE, type CaptureImageReply, type HostError } from "../shared/protocol";
import { errorText } from "./native-client";

/** How long a screenshot waits for its capture page. The page asks for it as soon as it has loaded. */
export const SCREENSHOT_TTL_MS = 60_000;
/** At most this many screenshots are kept; the oldest goes first. */
export const MAX_SCREENSHOTS = 3;

const PNG_DATA_URL = "data:image/png;base64,";
/** A capture id as `crypto.randomUUID()` writes it. */
const ID_PATTERN = /^[0-9a-f-]{16,64}$/;

export interface ScreenshotStoreOptions {
  ttlMs?: number;
  max?: number;
  /** Unique, unguessable ids; default `crypto.randomUUID()`. */
  newId?: () => string;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
}

interface Kept {
  image: string;
  timer: unknown;
}

/** Screenshots in memory: each can be taken once, and is dropped when its time is up. */
export class ScreenshotStore {
  private readonly kept = new Map<string, Kept>();
  private readonly ttlMs: number;
  private readonly max: number;
  private readonly newId: () => string;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;

  constructor(options: ScreenshotStoreOptions = {}) {
    this.ttlMs = options.ttlMs ?? SCREENSHOT_TTL_MS;
    this.max = options.max ?? MAX_SCREENSHOTS;
    this.newId = options.newId ?? (() => crypto.randomUUID());
    this.setTimer = options.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = options.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  }

  get size(): number {
    return this.kept.size;
  }

  /** Keeps `image` and returns the id to take it with. */
  put(image: string): string {
    while (this.kept.size >= this.max) {
      const oldest = this.kept.keys().next().value;
      if (oldest === undefined) break;
      this.drop(oldest);
    }
    let id = this.newId();
    while (this.kept.has(id)) id = this.newId();
    const timer = this.setTimer(() => {
      this.kept.delete(id);
    }, this.ttlMs);
    this.kept.set(id, { image, timer });
    return id;
  }

  /** The screenshot kept under `id`, which is then forgotten; null when there is none (any more). */
  take(id: unknown): string | null {
    if (typeof id !== "string" || !ID_PATTERN.test(id)) return null;
    const entry = this.kept.get(id);
    if (!entry) return null;
    this.drop(id);
    return entry.image;
  }

  /** Forgets everything (the background script is being stopped). */
  clear(): void {
    for (const id of [...this.kept.keys()]) this.drop(id);
  }

  private drop(id: string): void {
    const entry = this.kept.get(id);
    if (!entry) return;
    this.clearTimer(entry.timer);
    this.kept.delete(id);
  }
}

export interface CaptureApi {
  runtime: { getURL(path: string): string };
  tabs: {
    /** The visible part of the active tab of the current window as a PNG data URL, in device pixels. */
    captureVisibleTab(windowId: number | undefined, options: { format: "png" }): Promise<string>;
    create(properties: { url: string }): Promise<unknown>;
    /** Only used for the explanation tab (`createExplanationOpener`); needs no permission for a tab of this extension. */
    update?(tabId: number, properties: { url: string; active: boolean }): Promise<unknown>;
    /** Only used to see whether the active tab is a private one (`activeTabIsPrivate`); `incognito` needs no permission. */
    query?(filter: { active: true; lastFocusedWindow: true }): Promise<unknown>;
  };
}

/**
 * Why a capture did not start, as a short fixed word. It is the only thing that
 * goes into the address of the explanation page (`capture.html?failed=...`);
 * the message, which holds what Firefox or Chrome reported, never does.
 */
export const CAPTURE_FAIL_REASONS = ["not_capturable", "no_picture", "private_window", "page_not_opened"] as const;
export type CaptureFailReason = (typeof CAPTURE_FAIL_REASONS)[number];

/** The name of the address parameter that carries a CaptureFailReason. */
export const CAPTURE_FAILED_PARAM = "failed";

/**
 * `moz-extension://<uuid>/capture.html` or `chrome-extension://<id>/capture.html`:
 * what the browser reports as the page's `sender.url` (plus the query).
 */
export function capturePageUrl(api: Pick<CaptureApi, "runtime">): string {
  return api.runtime.getURL(CAPTURE_PAGE);
}

export type CaptureStartReply = { ok: true } | { ok: false; error: HostError; reason: CaptureFailReason };

export interface CaptureStarterOptions {
  /** Named in the messages ("Firefox" or "Chrome"); default "Firefox". */
  browserName?: string;
  /**
   * Asked before the screenshot is taken; true refuses the capture. Chrome
   * only (see `activeTabIsPrivate`). A rejection counts as "not private".
   */
  isPrivateTab?: () => Promise<boolean>;
}

/**
 * True when the active tab of the focused window is an Incognito one.
 *
 * Chrome cannot load an extension page in an Incognito window (the manifest
 * has no "incognito" key, so Chrome uses "spanning" mode): it would open the
 * capture page, with the picture of the private tab, in a normal window and
 * record its address in the normal history. Such a capture is refused instead.
 * Firefox loads the extension's pages in private windows, so it is not asked.
 */
export async function activeTabIsPrivate(api: CaptureApi): Promise<boolean> {
  if (!api.tabs.query) return false;
  const tabs = await api.tabs.query({ active: true, lastFocusedWindow: true });
  if (!Array.isArray(tabs)) return false;
  return tabs.some((tab) => typeof tab === "object" && tab !== null && (tab as { incognito?: unknown }).incognito === true);
}

/**
 * Takes the screenshot and opens the capture page for it. Never rejects. The
 * error message holds only what the browser said, never anything from the page.
 *
 * The messages name Firefox or Chrome (`browserName`) and never say "the
 * browser": the Chrome bundles must not contain that word as an identifier,
 * and a test checks the whole text of the bundle for it.
 */
export function createCaptureStarter(
  api: CaptureApi,
  store: ScreenshotStore,
  options: CaptureStarterOptions | string = {},
): () => Promise<CaptureStartReply> {
  const { browserName = "Firefox", isPrivateTab }: CaptureStarterOptions = typeof options === "string" ? { browserName: options } : options;
  return async () => {
    if (isPrivateTab) {
      let isPrivate = false;
      try {
        isPrivate = await isPrivateTab();
      } catch {
        // Not known: the capture goes on as before.
      }
      if (isPrivate) {
        return {
          ok: false,
          reason: "private_window",
          error: {
            code: CAPTURE_FAILED,
            message:
              `This is a private (Incognito) tab. ${browserName} cannot show ChemLinker's capture page in a private window, ` +
              "so the picture would appear in a normal window. Open the page with the reaction in a normal window and capture it there.",
          },
        };
      }
    }
    let image: unknown;
    try {
      image = await api.tabs.captureVisibleTab(undefined, { format: "png" });
    } catch (error) {
      return {
        ok: false,
        reason: "not_capturable",
        error: {
          code: CAPTURE_FAILED,
          message:
            `This tab cannot be captured (${browserName} reports: ${errorText(error)}). ` +
            `${browserName} does not allow pictures of its own pages (settings, new tab, add-on store). ` +
            "Open the page with the reaction and try again.",
        },
      };
    }
    if (typeof image !== "string" || !image.startsWith(PNG_DATA_URL) || image.length <= PNG_DATA_URL.length) {
      return {
        ok: false,
        reason: "no_picture",
        error: { code: CAPTURE_FAILED, message: `${browserName} gave no picture of this tab. Open the page with the reaction and try again.` },
      };
    }
    const id = store.put(image);
    try {
      await api.tabs.create({ url: `${capturePageUrl(api)}?id=${encodeURIComponent(id)}` });
    } catch (error) {
      store.take(id);
      return {
        ok: false,
        reason: "page_not_opened",
        error: { code: CAPTURE_FAILED, message: `The capture page could not be opened (${browserName} reports: ${errorText(error)}).` },
      };
    }
    return { ok: true };
  };
}

/** The answer to `capture-image`. */
export function takeScreenshot(store: ScreenshotStore, id: unknown): CaptureImageReply {
  const image = store.take(id);
  if (image === null) {
    return {
      ok: false,
      error: {
        code: CAPTURE_EXPIRED,
        message:
          "This screenshot is no longer kept: it can be opened once, shortly after it was taken. " +
          "Go to the tab with the reaction and choose Capture reaction again.",
      },
    };
  }
  return { ok: true, result: { image } };
}

export interface CommandsApi {
  onCommand: { addListener(listener: (command: string) => void): void };
}

/** The manifest's keyboard shortcut for "Capture reaction". */
export const CAPTURE_COMMAND = "capture-reaction";

/** The address of the capture page that explains why a capture did not start. */
export function explanationUrl(api: Pick<CaptureApi, "runtime">, reason: CaptureFailReason): string {
  return `${capturePageUrl(api)}?${CAPTURE_FAILED_PARAM}=${reason}`;
}

/**
 * Opens the capture page with the reason why a capture did not start. Pressing
 * the shortcut again does not open one tab after the other: the explanation
 * tab opened last is used again while it still exists (its number is kept in
 * memory only; after a restart of the background script a new tab opens).
 */
export function createExplanationOpener(api: CaptureApi): (reason: CaptureFailReason) => Promise<void> {
  let lastTab: number | null = null;
  return async (reason) => {
    const url = explanationUrl(api, reason);
    if (lastTab !== null && api.tabs.update) {
      try {
        await api.tabs.update(lastTab, { url, active: true });
        return;
      } catch {
        // That tab was closed: open a new one.
        lastTab = null;
      }
    }
    const tab = await api.tabs.create({ url });
    const id = typeof tab === "object" && tab !== null ? (tab as { id?: unknown }).id : undefined;
    lastTab = typeof id === "number" ? id : null;
  };
}

/**
 * The keyboard shortcut does what the popup's "Capture reaction" does. When the
 * capture does not start there is no popup to say so: the capture page is
 * opened without a screenshot, with the reason as a fixed word in its address,
 * and explains it.
 */
export function registerCaptureCommand(
  commands: CommandsApi | undefined,
  start: () => Promise<CaptureStartReply>,
  openExplanation: (reason: CaptureFailReason) => Promise<unknown>,
): void {
  commands?.onCommand.addListener((command) => {
    if (command !== CAPTURE_COMMAND) return;
    start()
      .then((reply) => (reply.ok ? undefined : openExplanation(reply.reason)))
      .catch(() => {
        // Nothing to show it in.
      });
  });
}
