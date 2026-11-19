/** Words shown to the chemist. */

import type { AnalysisStatus, MatchReason, ModeOverride, StereoMode } from "../shared/protocol";

export const MODE_OVERRIDE_LABELS: Record<ModeOverride, string> = {
  abs: "Absolute (as drawn)",
  rac: "Racemic",
  rel: "Relative (single enantiomer, absolute configuration unknown)",
  undefined: "Undefined (ignore wedges)",
};

/** Labels for every stereo mode the registry can report. */
export function stereoModeLabel(mode: string): string {
  switch (mode as StereoMode) {
    case "abs":
    case "rac":
    case "rel":
    case "undefined":
      return MODE_OVERRIDE_LABELS[mode as ModeOverride];
    case "none":
      return "No stereo";
    case "partial":
      return "Partial (some stereocentres defined, others undefined)";
    case "mixed":
      return "Mixed (stereo groups)";
    default:
      return mode;
  }
}

export const STATUS_LABELS: Record<AnalysisStatus, string> = {
  registered_here: "Already registered for this experiment",
  known_compound: "Known compound: this stereo variant is registered, but not for this experiment",
  known_core: "Known constitution: only other stereo variants are registered",
  new: "New compound: nothing with this constitution is registered",
};

export const CREATE_LABELS: Record<Exclude<AnalysisStatus, "registered_here">, string> = {
  new: "Create ID (new compound)",
  known_core: "Create ID (new stereo variant)",
  known_compound: "Create ID (new batch)",
};

/** Why a CORE is in a search result (`matched_by`), in words. */
export const MATCH_REASON_LABELS: Record<MatchReason, string> = {
  id: "ID",
  inchikey: "InChIKey",
  structure: "structure (same constitution)",
  name: "name",
  formula: "formula",
  experiment: "ELN experiment",
  drawn: "drawn structure",
};

/** "Matched by ID and name"; null without reasons. Reasons this version does not know are shown as sent. */
export function matchedByText(reasons: readonly string[]): string | null {
  const words = reasons.map((r) =>
    Object.prototype.hasOwnProperty.call(MATCH_REASON_LABELS, r) ? MATCH_REASON_LABELS[r as MatchReason] : r,
  );
  if (words.length === 0) return null;
  const list = words.length === 1 ? words[0]! : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]!}`;
  return `Matched by ${list}`;
}

/** "2026-11-17T10:22:31+00:00" -> "2026-11-17 10:22"; anything else unchanged. */
export function shortDate(value: string): string {
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(value);
  return m ? `${m[1]} ${m[2]}` : value;
}
