/**
 * Small pieces shared by the panel and the compounds page: the error box and
 * the experiment link. Text only via text nodes; links only via `experimentLink`.
 */

import type { HostError, Op } from "../../shared/protocol";
import { presentError } from "../errors";
import { experimentLink } from "../safe-values";
import type { Child, El } from "./dom";

export function renderErrorBox(el: El, error: HostError, op?: Op): HTMLElement {
  const view = presentError(error, op);
  return el(
    "div",
    { class: "banner error", attrs: { role: "alert" } },
    el("strong", { text: view.heading }),
    view.hint ? el("p", { text: view.hint }) : null,
    view.message ? el("p", { text: view.message }) : null,
    el("p", { class: "code", text: `Code: ${view.code}` }),
  );
}

/**
 * An experiment as a link that opens in a NEW TAB (the stored URL is a
 * top-level ELN address; inside the experiment frame it would load the whole
 * application in the frame), or as plain text when the URL is not on the site.
 */
export function experimentAnchor(el: El, text: string, url: string | null | undefined): Child {
  const href = experimentLink(url);
  if (href === null) return text;
  return el("a", { attrs: { href, target: "_blank", rel: "noopener noreferrer" }, text });
}
