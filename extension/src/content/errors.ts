/**
 * How an error is shown: a short heading per known code, an optional hint, and
 * ALWAYS the host's own message (written for the user) plus the code.
 */

import { FILE_UNAVAILABLE, REGISTRY_CHANGED, REGISTRY_MISSING, YIELD_MISMATCH, type HostError, type Op } from "../shared/protocol";

/** How the registry program is put back when it cannot be reached. */
export const RUN_INSTALLER_HINT =
  "Run the installer again (install.ps1 on Windows, install.sh on macOS/Linux), then press Retry.";

/** How extension and registry program are brought to the same release. */
export const SAME_RELEASE_HINT =
  "Run the update script (update.ps1 on Windows, update.sh on macOS/Linux), then reload the extension, " +
  "so that both come from the same release.";

/** How an ID is retired: with the command-line tool only, never from the extension. */
export function retireHint(id: string | null): string {
  return `An ID is retired in a terminal with: compound-registry retire ${id ?? "<ID>"} --reason "<why>"`;
}

export interface ErrorView {
  heading: string;
  hint: string | null;
  message: string;
  code: string;
  existingId: string | null;
}

/** Codes after which a `register` may or may not have issued the ID. */
export const REGISTER_OUTCOME_UNKNOWN: readonly string[] = [
  "host_timeout",
  "host_unavailable",
  "host_bad_response",
  "internal_error",
  "frame_too_large",
];

const OUTCOME_UNKNOWN_HINT =
  "The ID may or may not have been created: the request did not end with a clear answer. " +
  "Look up again: if the ID was created, the lookup shows it.";

const CREATED_BUT_TOO_LARGE_HINT =
  "The ID WAS created (the message below names it), but the answer was too large to deliver. " +
  "Look up again to see it with its details.";

const GENERATE_TIMEOUT_HINT =
  "The naming program on this PC did not finish in time. Try again later, or type the name with Add name / Edit.";

const RELOOK = "Nothing was registered. Look up again and check the result before creating an ID.";

const HEADINGS: Record<string, { heading: string; hint?: string }> = {
  // extension side
  host_unavailable: {
    heading: "Registry program not reachable",
    hint:
      "The local registry program is not installed or could not start. " + RUN_INSTALLER_HINT,
  },
  host_timeout: {
    heading: "No answer from the registry program",
    hint: "It may still be starting (it loads a chemistry library). Try again in a moment.",
  },
  host_bad_response: { heading: "Unexpected answer from the registry program" },
  not_allowed: { heading: "Request refused by the extension" },
  // host
  protocol_mismatch: {
    heading: "Extension and registry program do not match",
    hint: SAME_RELEASE_HINT,
  },
  bad_request: { heading: "The registry program refused the request" },
  unknown_op: {
    heading: "Operation not supported by the registry program",
    hint: `The registry program is probably older than this extension. ${SAME_RELEASE_HINT}`,
  },
  frame_too_large: { heading: "The request is too large" },
  response_too_large: { heading: "The answer is too large" },
  forbidden_caller: { heading: "The registry program refuses this extension" },
  internal_error: { heading: "Internal error in the registry program", hint: "Details are in the host log." },
  confirmation_mismatch: { heading: "The structure is not what you confirmed", hint: RELOOK },
  // chemistry
  structure_empty: { heading: "The structure is empty" },
  structure_parse_failed: { heading: "The structure could not be read" },
  structure_has_placeholders: {
    heading: "The structure contains placeholders",
    hint: "R-groups, query atoms, '*' atoms and abbreviations cannot be registered. Expand them first.",
  },
  structure_unsupported: { heading: "This kind of structure is not supported" },
  atropisomer_unsupported: { heading: "This chiral axis cannot be registered safely" },
  inchi_failed: { heading: "No InChI could be computed for this structure" },
  too_many_stereo_groups: { heading: "Too many stereo groups" },
  invalid_stereo_groups: { heading: "Invalid stereo groups" },
  invalid_mode: { heading: "Unknown stereo mode" },
  // registry
  slot_conflict: { heading: "This product slot is already taken" },
  stereo_not_in_key: { heading: "Stereo that the ID would not distinguish must be accepted first" },
  id_format_mismatch: { heading: "The ID format differs from the one stored in the database" },
  id_overflow: { heading: "The ID format has no numbers left" },
  database_older_than_journal: {
    heading: "The database is older than the issued-ID journal",
    hint: "Nothing was registered. The database was probably restored from an older backup.",
  },
  journal_conflict: { heading: "The issued-ID journal contradicts the database", hint: "Nothing was registered." },
  journal_unreadable: { heading: "The issued-ID journal cannot be read" },
  chemistry_version_changed: {
    heading: "The chemistry software changed",
    hint: "Registering is blocked until the stored structures have been checked.",
  },
  [REGISTRY_MISSING]: {
    heading: "The registry does not exist",
    hint:
      "The registry folder or database the registry program is configured for was not found. " +
      "The registry program never creates one by itself.",
  },
  [REGISTRY_CHANGED]: {
    heading: "The registry configuration changed",
    hint:
      "The registry program noticed a different registry configuration, refused the request and stopped. " +
      "The panel checks again which registry is in use; look up again before creating an ID.",
  },
  config_invalid: { heading: "The registry configuration is invalid" },
  storage_error: { heading: "The registry database could not be read or written" },
  invalid_argument: { heading: "Invalid input" },
  not_found: { heading: "Nothing found" },
  [YIELD_MISMATCH]: { heading: "The yield belongs to another product or experiment", hint: "Nothing was stored." },
  [FILE_UNAVAILABLE]: { heading: "The file cannot be shown" },
};

export const GENERIC_HEADING = "Error from the registry program";

export function presentError(error: HostError, op?: Op): ErrorView {
  const known = Object.prototype.hasOwnProperty.call(HEADINGS, error.code) ? HEADINGS[error.code] : undefined;
  let hint = known?.hint ?? null;
  if (op === "register" && REGISTER_OUTCOME_UNKNOWN.includes(error.code)) {
    hint = OUTCOME_UNKNOWN_HINT + (error.code === "host_unavailable" && known?.hint ? ` ${known.hint}` : "");
  } else if (op === "register" && error.code === "response_too_large") {
    hint = CREATED_BUT_TOO_LARGE_HINT;
  } else if (op === "generate_name" && error.code === "host_timeout") {
    hint = GENERATE_TIMEOUT_HINT;
  }
  const existingId = typeof error.existing_id === "string" && error.existing_id ? error.existing_id : null;
  if (error.code === "slot_conflict") {
    hint = existingId
      ? `This product slot already holds ${existingId}. It must be retired before another compound can take the slot. ${retireHint(existingId)}`
      : `This product slot already holds another compound. It must be retired first. ${retireHint(null)}`;
  }
  return {
    heading: known?.heading ?? GENERIC_HEADING,
    hint,
    message: String(error.message ?? ""),
    code: String(error.code ?? ""),
    existingId,
  };
}
