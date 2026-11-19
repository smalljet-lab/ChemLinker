/**
 * "Find experiments by ID": one compact row (label, input, Find), and below
 * it the answer of the latest search until the next search or "Clear".
 *
 * The row is built once (typing never re-renders it); only the result area is
 * re-rendered from the `SearchController` state. Text only via text nodes.
 */

import type { FindResult, FoundExperiment, FoundVariant } from "../../shared/protocol";
import { datePart, experimentText, findHeading } from "../experiments";
import { stereoModeLabel } from "../labels";
import { svgDataUrl } from "../safe-values";
import type { SearchController } from "../search-state";
import { firstYield, shortYield } from "../yields";
import { experimentAnchor, renderErrorBox } from "./common";
import { makeEl, type El } from "./dom";
import { idExamples, type IdExamples } from "../id-examples";

export interface SearchView {
  element: HTMLElement;
  render(): void;
  /** Show example IDs in the format of the registry in use (known after a ping). */
  setExamples(examples: IdExamples): void;
}

function placeholderFor(examples: IdExamples): string {
  return `e.g. ${examples.coreStereo} or ${examples.full}`;
}

let counter = 0;

function batchesText(n: number): string {
  return n === 1 ? "1 batch" : `${n} batches`;
}

function variantRow(el: El, variant: FoundVariant): HTMLElement {
  const src = svgDataUrl(variant.svg);
  return el(
    "li",
    { class: "variant" },
    src ? el("img", { class: "thumb small", attrs: { src, alt: `Structure of ${variant.core_stereo_id}` } }) : null,
    el(
      "span",
      null,
      el("span", { class: "mono", text: variant.core_stereo_id }),
      ` · ${stereoModeLabel(variant.stereo_mode)} · ${batchesText(variant.batch_count)}`,
    ),
  );
}

function experimentItem(el: El, experiment: FoundExperiment): HTMLElement {
  const y = shortYield(firstYield(experiment.batches));
  return el(
    "li",
    { class: "found-experiment" },
    el(
      "div",
      { class: "found-code" },
      experimentAnchor(el, experimentText(experiment.experiment_ref, experiment.experiment_code), experiment.experiment_url),
      y ? el("span", { class: "found-yield", text: ` (${y})` }) : null,
    ),
    el(
      "ul",
      { class: "found-batches" },
      ...experiment.batches.map((b) =>
        el(
          "li",
          { class: b.retired ? "retired-batch" : "" },
          el("span", { class: "mono", text: b.full_id }),
          ` · product ${b.product_index} · ${datePart(b.created_at)}`,
          b.retired ? el("span", { class: "retired", text: " · retired" }) : null,
        ),
      ),
    ),
  );
}

function resultBlock(el: El, result: FindResult, onSearch: (id: string) => void, busy: boolean): HTMLElement {
  return el(
    "div",
    { class: "found" },
    el("h3", { text: findHeading(result) }),
    result.warnings.length > 0
      ? el(
          "div",
          { class: "banner warn", attrs: { role: "note" } },
          el("strong", { text: "Registry warnings" }),
          el("ul", null, ...result.warnings.map((w) => el("li", { text: w }))),
        )
      : null,
    el("ul", { class: "list variants" }, ...result.variants.map((v) => variantRow(el, v))),
    result.experiments.length > 0
      ? el(
          "div",
          null,
          el("h4", {
            text: `Made in ${result.experiments.length} ${result.experiments.length === 1 ? "experiment" : "experiments"}`,
          }),
          el("ul", { class: "list experiments" }, ...result.experiments.map((e) => experimentItem(el, e))),
        )
      : el("p", { class: "small", text: "No experiment is recorded for it." }),
    result.other_variants.length > 0
      ? el(
          "div",
          { class: "other-variants" },
          el("p", { class: "small", text: "Other stereo variants of this CORE:" }),
          el(
            "div",
            { class: "row" },
            ...result.other_variants.map((v) => {
              const button = el("button", {
                class: "btn small-btn",
                attrs: { type: "button" },
                text: `${v.core_stereo_id} · ${stereoModeLabel(v.stereo_mode)} · ${batchesText(v.batch_count)}`,
              });
              button.disabled = busy;
              button.addEventListener("click", () => onSearch(v.core_stereo_id));
              return button;
            }),
          ),
        )
      : null,
  );
}

export function buildSearchView(doc: Document, controller: SearchController): SearchView {
  const el = makeEl(doc);
  const id = `crm-find-${++counter}`;
  const input = el("input", {
    key: "find-input",
    attrs: {
      id,
      type: "text",
      placeholder: placeholderFor(idExamples(null)),
      autocomplete: "off",
      spellcheck: "false",
      maxlength: "200",
    },
  });
  const find = el("button", { class: "btn", key: "find", attrs: { type: "button" }, text: "Find" });
  const resultArea = el("div", { class: "search-result", attrs: { "aria-live": "polite" } });

  const run = (text: string): void => {
    if (controller.state.pending) return; // one search at a time
    void controller.search(text);
  };
  find.addEventListener("click", () => run(input.value));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      run(input.value);
    }
  });
  const searchFor = (coreStereoId: string): void => {
    input.value = coreStereoId;
    run(coreStereoId);
  };

  function render(): void {
    const s = controller.state;
    find.disabled = s.pending !== null;
    const children: (HTMLElement | null)[] = [];
    if (s.pending) children.push(el("p", { class: "small busy", text: `Searching for ${s.pending.query}…` }));
    if (s.result || s.error) {
      const clear = el("button", { class: "btn small-btn", key: "find-clear", attrs: { type: "button" }, text: "Clear" });
      clear.addEventListener("click", () => controller.clear());
      if (s.error) children.push(renderErrorBox(el, s.error, "find"));
      if (s.result) children.push(resultBlock(el, s.result, searchFor, s.pending !== null));
      children.push(el("div", { class: "row" }, clear));
    }
    resultArea.replaceChildren(...children.filter((c): c is HTMLElement => c !== null));
  }

  const element = el(
    "section",
    { class: "section search", attrs: { "aria-label": "Find experiments by ID" } },
    el("label", { class: "field", attrs: { for: id }, text: "Find experiments by ID" }),
    el("div", { class: "search-row" }, input, find),
    resultArea,
  );
  render();
  const setExamples = (examples: IdExamples): void => {
    input.setAttribute("placeholder", placeholderFor(examples));
  };
  return { element, render, setExamples };
}
