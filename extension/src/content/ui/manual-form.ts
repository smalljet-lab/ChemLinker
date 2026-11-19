/**
 * The form of the manual source: structure textarea, .mol drop zone / file
 * picker, experiment key, experiment code, product no.
 *
 * The form is built once and pushes every edit into the `ManualSource`; it is
 * never rebuilt while the user types.
 */

import { MAX_PRODUCT_INDEX, MAX_REF_TEXT } from "../../shared/protocol";
import type { FileLike, ManualSource } from "../manual-source";
import { makeEl } from "./dom";

let formCounter = 0;

export interface ManualFormHandle {
  element: HTMLElement;
  /**
   * Fills the experiment key and code from the detected experiment. A field
   * the user has edited (no longer the last pre-filled value) is left alone.
   */
  prefill(key: string | null, code: string | null): void;
}

export function createManualForm(doc: Document, source: ManualSource): ManualFormHandle {
  const el = makeEl(doc);
  const uid = `crm${++formCounter}`;
  const ids = {
    text: `${uid}-text`,
    file: `${uid}-file`,
    key: `${uid}-key`,
    code: `${uid}-code`,
    index: `${uid}-index`,
    detected: `${uid}-detected`,
  };

  const detected = el("p", { class: "small", attrs: { id: ids.detected } });
  const fileMessage = el("p", { class: "small", attrs: { role: "status" } });

  const textarea = el("textarea", {
    attrs: {
      id: ids.text,
      spellcheck: "false",
      autocomplete: "off",
      wrap: "off",
      "aria-describedby": ids.detected,
    },
    key: "structure-text",
  });
  textarea.addEventListener("input", () => {
    source.setStructureText(textarea.value);
    fileMessage.textContent = "";
  });

  const fileInput = el("input", {
    class: "file-input",
    attrs: { id: ids.file, type: "file", accept: ".mol,chemical/x-mdl-molfile" },
  });

  async function takeFiles(files: FileList | null | undefined): Promise<void> {
    if (!files || files.length === 0) return;
    if (files.length > 1) {
      fileMessage.textContent = "Drop one .mol file at a time.";
      return;
    }
    const file = files[0] as FileLike;
    const result = await source.loadFile(file);
    if (result.ok) {
      // Show the file; the source keeps the file's own text, unchanged.
      textarea.value = source.structureText;
      fileMessage.textContent = `Loaded ${file.name}.`;
    } else {
      fileMessage.textContent = result.message;
    }
  }

  fileInput.addEventListener("change", () => {
    void takeFiles(fileInput.files).finally(() => {
      fileInput.value = "";
    });
  });

  const dropzone = el(
    "div",
    { class: "dropzone" },
    "Drop one .mol file here or ",
    fileInput,
    el("label", { class: "file-label", attrs: { for: ids.file }, text: "choose a file" }),
  );
  dropzone.addEventListener("dragover", (event) => {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    dropzone.classList.add("over");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("over"));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    event.stopPropagation();
    dropzone.classList.remove("over");
    void takeFiles(event.dataTransfer?.files);
  });

  const keyInput = el("input", {
    attrs: { id: ids.key, type: "text", maxlength: String(MAX_REF_TEXT), autocomplete: "off", spellcheck: "false" },
    key: "experiment-key",
  });
  keyInput.addEventListener("input", () => source.setExperimentKey(keyInput.value));

  const codeInput = el("input", {
    attrs: { id: ids.code, type: "text", maxlength: String(MAX_REF_TEXT), autocomplete: "off", spellcheck: "false" },
    key: "experiment-code",
  });
  codeInput.addEventListener("input", () => source.setExperimentCode(codeInput.value));

  const indexInput = el("input", {
    attrs: {
      id: ids.index,
      type: "number",
      min: "1",
      max: String(MAX_PRODUCT_INDEX),
      step: "1",
      inputmode: "numeric",
      value: "1",
    },
    key: "product-index",
  });
  indexInput.addEventListener("input", () => source.setProductIndex(indexInput.value));

  function updateDetected(): void {
    const d = source.detected();
    const from = source.loadedFileName ? ` from ${source.loadedFileName}` : "";
    switch (d.kind) {
      case "empty":
        detected.textContent = "A text with line breaks is read as a molfile, a single line as SMILES.";
        break;
      case "too_large":
        detected.textContent = "Too large.";
        break;
      case "molfile":
        detected.textContent = `Read as a molfile${from} (${d.lines} lines), sent exactly as entered.`;
        break;
      case "smiles":
        detected.textContent = "Read as SMILES.";
        break;
    }
  }
  source.subscribe(updateDetected);
  updateDetected();

  let prefilledKey = "";
  let prefilledCode = "";
  function prefill(key: string | null, code: string | null): void {
    if (key !== null && (keyInput.value === "" || keyInput.value === prefilledKey)) {
      keyInput.value = key;
      prefilledKey = key;
      source.setExperimentKey(key);
    }
    if (code !== null && (codeInput.value === "" || codeInput.value === prefilledCode)) {
      codeInput.value = code;
      prefilledCode = code;
      source.setExperimentCode(code);
    }
  }

  const element = el(
    "div",
    { class: "manual-form" },
    el("label", { class: "field", attrs: { for: ids.text }, text: "Molfile or SMILES" }),
    textarea,
    detected,
    dropzone,
    fileMessage,
    el(
      "div",
      { class: "fields-inline" },
      el(
        "div",
        null,
        el(
          "label",
          { class: "field", attrs: { for: ids.key } },
          "Experiment key ",
          el("span", { class: "hint", text: "(internal number, not the ELN number)" }),
        ),
        keyInput,
      ),
      el(
        "div",
        null,
        el(
          "label",
          { class: "field", attrs: { for: ids.code } },
          "Experiment code ",
          el("span", { class: "hint", text: "(ELN number, optional)" }),
        ),
        codeInput,
      ),
      el("div", null, el("label", { class: "field", attrs: { for: ids.index }, text: "Product no." }), indexInput),
    ),
  );
  return { element, prefill };
}
