/** The sentences the capture page shows for the host's codes. */

import type { CaptureOp, HostError, MoleculeWarning, RecognitionStatusResult, RecognitionWarning } from "../shared/protocol";

export const INSTALL_COMMAND = "compound-registry install-recognition";

export const MOLECULE_WARNING_TEXT: Readonly<Record<MoleculeWarning, string>> = {
  unparsable: "Could not be read as a structure - edit it or set it to Not used",
  placeholder: "Contains a placeholder (R group or unknown atom) - replace it with the real group",
  isotope: "Contains an isotope label - probably a misread",
  unusual_element: "Contains an unusual element - probably a misread",
  stereo: "Has stereochemistry - please check the wedges and the double-bond geometry (E/Z)",
  charged_or_salt: "Salt or charged - check the counter-ion",
  low_confidence: "The program is not sure about this one - check it carefully",
  duplicate: "The same structure appears more than once",
  smiles_molfile_differ: "The program read this molecule in two ways that disagree - compare the drawing with the picture",
};

export const RESULT_WARNING_TEXT: Readonly<Record<RecognitionWarning, string>> = {
  no_molecules: "No molecule was found in the selected area. Try a tighter selection around the drawing.",
  no_reaction: "Molecules were found, but no reaction arrow. Give the molecules their roles yourself, or select the area again.",
  unassigned_molecules: "Some molecules were found but belong to no reaction step. They are listed under \"Not used in a reaction\".",
  truncated: "The picture holds more molecules than can be shown; the rest was left out. Select a smaller area.",
};

/**
 * What a warning says on a molecule the user drew or corrected: there "probably
 * a misread" would be wrong (deuterium, a palladium complex drawn on purpose).
 */
export const EDITED_WARNING_TEXT: Readonly<Partial<Record<MoleculeWarning, string>>> = {
  isotope: "Contains an isotope label - check that you meant it",
  unusual_element: "Contains an unusual element - check that you meant it",
};

/** The sentence for a warning code on a card. */
export function warningText(code: MoleculeWarning, edited: boolean): string {
  return (edited ? EDITED_WARNING_TEXT[code] : undefined) ?? MOLECULE_WARNING_TEXT[code];
}

/**
 * "Program's own score: 0.93" or null. Deliberately not a percentage: the
 * value is the model's own score, not the chance that the structure is right
 * (in the trials wrong structures scored above 0.9 too).
 */
export function confidenceText(confidence: number | null): string | null {
  if (confidence === null || !Number.isFinite(confidence)) return null;
  return `Program's own score: ${Math.min(1, Math.max(0, confidence)).toFixed(2)}`;
}

/** Shown when the pointer rests on the score. */
export const CONFIDENCE_HELP =
  "How sure the program was of its own reading, from 0 to 1. It is not the chance that the structure is right: a misread structure can score high. Compare it with the picture.";

/**
 * What the capture page says when it was opened without a screenshot because a
 * capture did not start. The keys are the background's CAPTURE_FAIL_REASONS
 * (a test compares them); the page gets one of them in its address.
 */
export const CAPTURE_FAIL_TEXT: Readonly<Record<string, readonly string[]>> = {
  not_capturable: [
    "This tab could not be captured.",
    "Firefox and Chrome do not allow pictures of their own pages, such as settings, the new tab page or the add-on store.",
    "If it was a normal web page, the keyboard shortcut was not allowed to take the picture there: click the ChemLinker button in the toolbar and choose Capture reaction instead.",
  ],
  no_picture: ["No picture of the tab was returned. Go back to the tab with the reaction and try again."],
  private_window: [
    "The tab is a private (Incognito) one. The capture page cannot be shown in a private window, so the picture would appear in a normal window.",
    "Open the page with the reaction in a normal window and capture it there.",
  ],
  page_not_opened: ["The capture page could not be opened. Try again."],
};

/** The paragraphs for a reason from the page's address; null for anything that is not a known reason. */
export function captureFailText(reason: string | null | undefined): readonly string[] | null {
  return typeof reason === "string" && Object.hasOwn(CAPTURE_FAIL_TEXT, reason) ? CAPTURE_FAIL_TEXT[reason]! : null;
}

export interface StatusText {
  kind: "info" | "warn" | "error" | "ok";
  text: string;
  /** The command to show in a line of its own. */
  command?: string;
}

/** What to say about the state of the recognition part. `detail` is the host's own text. */
export function statusText(status: RecognitionStatusResult): StatusText {
  const detail = status.detail && status.detail.trim() !== "" ? ` ${status.detail.trim()}` : "";
  switch (status.state) {
    case "not_installed":
      // The program's own text names the folder and the exact command of this PC
      // (the command is not on the PATH), so it is shown as it stands.
      if (detail !== "") return { kind: "warn", text: `${detail.trim()} Then press Check again.` };
      return {
        kind: "warn",
        text:
          "ChemResolver is not installed on this PC. It is optional and large (about 2.8 GB of download). " +
          "To install it, open a terminal and run the command below, then press Check again.",
        command: INSTALL_COMMAND,
      };
    case "failed":
      return { kind: "error", text: `ChemResolver could not be started.${detail}` };
    case "ready":
      return { kind: "ok", text: "ChemResolver is ready." };
    case "loading":
    case "stopped": {
      const seconds = status.load_seconds !== undefined ? ` (${Math.round(status.load_seconds)} s so far)` : "";
      return {
        kind: "info",
        text:
          `ChemResolver is loading its models${seconds}. This takes 20 to 40 seconds, the very first time a few minutes. ` +
          "You can already select the reaction.",
      };
    }
  }
}

/** A host or extension error in words for this page. */
export function errorSentence(error: HostError, op?: CaptureOp): string {
  switch (error.code) {
    case "unknown_op":
      return "The ChemLinker program on this PC is older than this extension and cannot read pictures yet. Update the program, then try again.";
    case "recognition_not_installed":
      // The program's own message says where and how to type the command on this PC.
      if (error.message.includes("install-recognition")) return error.message;
      return `ChemResolver is not installed on this PC. Open a terminal and run: ${INSTALL_COMMAND}`;
    case "host_timeout":
      // Every operation can run out of time; only for a recognition is the selection the thing to change.
      if (op === "recognize") return `${error.message} ChemResolver took too long. Try a smaller selection.`;
      return `${error.message} The program may be busy reading another picture. Try again in a moment.`;
    default:
      return error.message;
  }
}
