/**
 * The toolbar button's popup: two entries. It sends the background exactly two
 * messages, `capture-start` and `open-compounds`, and nothing to the host.
 * Both open a new tab, which closes the popup; an error stays visible here.
 */

import { makeEl } from "../content/ui/dom";
import type { CaptureStartRequest, OpenCompoundsRequest } from "../shared/protocol";

export type PopupMessage = CaptureStartRequest | OpenCompoundsRequest;

export interface PopupDeps {
  doc: Document;
  root: HTMLElement;
  /** `(message) => browser.runtime.sendMessage(message)` */
  send: (message: PopupMessage) => Promise<unknown>;
  /** `() => window.close()` */
  close: () => void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The error text of a background reply, or null when it says ok. */
export function replyProblem(reply: unknown): string | null {
  if (isRecord(reply) && reply.ok === true) return null;
  if (isRecord(reply) && isRecord(reply.error) && typeof reply.error.message === "string" && reply.error.message !== "") {
    return reply.error.message;
  }
  return "The extension's background script sent an unreadable answer. Reload the extension and try again.";
}

export function mountPopup(deps: PopupDeps): void {
  const { doc, root } = deps;
  const el = makeEl(doc);

  const problem = el("p", { class: "problem", attrs: { role: "alert" } });
  problem.hidden = true;

  const entry = (title: string, hint: string, message: PopupMessage): HTMLButtonElement => {
    const button = el(
      "button",
      { class: "menu-item", attrs: { type: "button" } },
      el("span", { class: "menu-title", text: title }),
      el("span", { class: "menu-hint", text: hint }),
    );
    button.addEventListener("click", () => {
      void run(message);
    });
    return button;
  };

  const capture = entry(
    "Capture reaction",
    "Reads a drawn reaction from this tab. You choose the area on the next page.",
    { type: "capture-start" },
  );
  const compounds = entry("Open compound catalogue", "All compounds and yields of your registry.", { type: "open-compounds" });
  const buttons = [capture, compounds];

  async function run(message: PopupMessage): Promise<void> {
    problem.hidden = true;
    for (const button of buttons) button.disabled = true;
    let text: string | null;
    try {
      text = replyProblem(await deps.send(message));
    } catch (error) {
      text = `The extension's background script did not answer (${error instanceof Error ? error.message : String(error)}). Reload the extension and try again.`;
    }
    for (const button of buttons) button.disabled = false;
    if (text === null) {
      deps.close();
      return;
    }
    problem.textContent = text;
    problem.hidden = false;
  }

  root.replaceChildren(
    el("h1", { text: "ChemLinker" }),
    el("div", { class: "menu" }, capture, compounds),
    problem,
  );
  capture.focus();
}
