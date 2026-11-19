/**
 * The PDF viewer of the compounds page: an overlay with a dialog showing one
 * PDF in an <iframe> (Firefox shows it with its built-in viewer), a link that
 * opens it in a new tab, a link that saves a copy, and Close.
 *
 * The only URL ever made here is a blob URL of bytes the host sent, typed
 * application/pdf. It is revoked when the viewer closes, unless the new-tab or
 * save link was used (the new tab, or the download, still reads it): then it
 * is revoked when the page is left (`revokeAll`). Only one viewer is open at a
 * time. While it is open the page behind it is inert; Esc, Close and a click
 * on the backdrop close it, and focus goes back to where it came from.
 */

import { makeEl } from "../content/ui/dom";

export interface ObjectUrls {
  create(blob: Blob): string;
  revoke(url: string): void;
}

export interface PdfViewerDeps {
  doc: Document;
  /** Made inert while the viewer is open (the page's root). */
  background: HTMLElement;
  urls: ObjectUrls;
}

export interface OpenOptions {
  /** The file name as the host sent it (shown as text). */
  name: string;
  bytes: Uint8Array;
  /** Where focus goes back on close (looked up when closing, it may have been re-rendered). */
  returnFocus?: () => HTMLElement | null;
}

export const PDF_FALLBACK_TEXT = "If the PDF does not appear here, use Open in a new tab or Save a copy.";

/** A file name that is safe to offer for saving, always ending in ".pdf". */
export function safePdfFileName(name: string): string {
  const base = name.split(/[\\/]/).pop() ?? "";
  let clean = base
    .replace(/[\u0000-\u001f\u007f<>:"|?*]/g, "_")
    .replace(/^[\s.]+|[\s.]+$/g, "")
    .slice(0, 200);
  if (clean === "") clean = "spectrum";
  return /\.pdf$/i.test(clean) ? clean : `${clean}.pdf`;
}

export function defaultObjectUrls(doc: Document): ObjectUrls {
  const view = doc.defaultView;
  return {
    create: (blob) => {
      if (!view) throw new Error("no window");
      return view.URL.createObjectURL(blob);
    },
    revoke: (url) => view?.URL.revokeObjectURL(url),
  };
}

interface Open {
  overlay: HTMLElement;
  url: string;
  /** The new-tab or save link was used: keep the URL until the page is left. */
  kept: boolean;
  returnFocus?: () => HTMLElement | null;
  onKey: (event: KeyboardEvent) => void;
}

export class PdfViewer {
  private current: Open | null = null;
  /** URLs still in use by a new tab or a download. */
  private readonly keptUrls: string[] = [];
  private readonly el;

  constructor(private readonly deps: PdfViewerDeps) {
    this.el = makeEl(deps.doc);
  }

  get isOpen(): boolean {
    return this.current !== null;
  }

  /** The dialog element while open (for tests). */
  get dialog(): HTMLElement | null {
    return this.current?.overlay.querySelector<HTMLElement>("[role=dialog]") ?? null;
  }

  open(options: OpenOptions): void {
    this.close(false);
    const { doc, urls } = this.deps;
    const el = this.el;
    const blob = new Blob([options.bytes as BlobPart], { type: "application/pdf" });
    const url = urls.create(blob);
    const titleId = "pdf-viewer-title";
    const fileName = safePdfFileName(options.name);

    const openLink = el("a", {
      class: "btn pdf-open",
      attrs: { href: url, target: "_blank", rel: "noopener" },
      text: "Open in a new tab",
    });
    const saveLink = el("a", { class: "btn pdf-save", attrs: { href: url, download: fileName }, text: "Save a copy" });
    const closeButton = el("button", { class: "btn pdf-close", attrs: { type: "button" }, text: "Close" });
    const frame = el("iframe", { class: "pdf-frame", attrs: { src: url, title: `PDF ${options.name}` } });
    const dialog = el(
      "div",
      { class: "pdf-dialog", attrs: { role: "dialog", "aria-modal": "true", "aria-labelledby": titleId, tabindex: "-1" } },
      el(
        "div",
        { class: "pdf-head" },
        el("h2", { class: "pdf-title mono", attrs: { id: titleId }, text: options.name }),
        el("div", { class: "row pdf-actions" }, openLink, saveLink, closeButton),
      ),
      el("p", { class: "small pdf-fallback", text: PDF_FALLBACK_TEXT }),
      frame,
    );
    const overlay = el("div", { class: "pdf-overlay" }, dialog);

    const state: Open = {
      overlay,
      url,
      kept: false,
      returnFocus: options.returnFocus,
      onKey: (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          this.close();
        }
      },
    };
    const keep = (): void => {
      state.kept = true;
    };
    openLink.addEventListener("click", keep);
    saveLink.addEventListener("click", keep);
    closeButton.addEventListener("click", () => this.close());
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) this.close();
    });
    doc.addEventListener("keydown", state.onKey, true);

    this.current = state;
    this.deps.background.setAttribute("inert", "");
    this.deps.background.setAttribute("aria-hidden", "true");
    doc.body.classList.add("modal-open");
    doc.body.append(overlay);
    closeButton.focus();
  }

  /** Closes the viewer (if open); `restoreFocus` false when another viewer replaces it. */
  close(restoreFocus = true): void {
    const state = this.current;
    if (!state) return;
    this.current = null;
    const { doc, urls } = this.deps;
    doc.removeEventListener("keydown", state.onKey, true);
    state.overlay.remove();
    this.deps.background.removeAttribute("inert");
    this.deps.background.removeAttribute("aria-hidden");
    doc.body.classList.remove("modal-open");
    if (state.kept) this.keptUrls.push(state.url);
    else urls.revoke(state.url);
    if (restoreFocus) {
      const target = state.returnFocus?.() ?? null;
      if (target && target.isConnected) target.focus();
    }
  }

  /** The page is left: close and revoke every URL made here. */
  revokeAll(): void {
    this.close(false);
    for (const url of this.keptUrls.splice(0)) this.deps.urls.revoke(url);
  }
}
