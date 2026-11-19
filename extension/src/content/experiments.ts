/**
 * Pure helpers about experiments in host answers: the "Also made in" line of
 * a product card and the texts of the search result.
 */

import type { AnalyzeResult, FindResult, YieldInfo } from "../shared/protocol";
import { hasYield } from "./yields";

export interface ExperimentEntry {
  ref: string;
  /** What is shown: the ELN number, or "key 4180" without one. */
  text: string;
  /** The stored URL (checked against SITE_ORIGIN only when rendered). */
  url: string | null;
  /** The yield stored for the compound's batch in that experiment (the first one that has one); null when none. */
  yield: YieldInfo | null;
}

export const ALSO_MADE_IN_MAX = 5;

export function experimentText(ref: string, code: string | null | undefined): string {
  return code && code.trim() !== "" ? code : `key ${ref}`;
}

/**
 * The OTHER experiments in which this exact compound was made (active
 * batches only), one entry per experiment, oldest first.
 */
export function otherExperiments(result: AnalyzeResult, currentRef: string | null): ExperimentEntry[] {
  const seen = new Map<string, ExperimentEntry>();
  for (const batch of result.batches) {
    if (batch.retired || batch.experiment_ref === currentRef) continue;
    const known = seen.get(batch.experiment_ref);
    if (!known) {
      seen.set(batch.experiment_ref, {
        ref: batch.experiment_ref,
        text: experimentText(batch.experiment_ref, batch.experiment_code),
        url: batch.experiment_url,
        yield: hasYield(batch.yield) ? batch.yield : null,
      });
    } else {
      // A later batch may know the code / link of a renamed experiment.
      if (batch.experiment_code) known.text = batch.experiment_code;
      if (batch.experiment_url) known.url = batch.experiment_url;
      if (known.yield === null && hasYield(batch.yield)) known.yield = batch.yield;
    }
  }
  return [...seen.values()];
}

/** At most `max` entries, and how many more there are. */
export function capEntries<T>(entries: T[], max: number = ALSO_MADE_IN_MAX): { shown: T[]; more: number } {
  return { shown: entries.slice(0, max), more: Math.max(0, entries.length - max) };
}

/** "000042_02" -> "000042". */
export function corePart(coreStereoId: string): string {
  const cut = coreStereoId.lastIndexOf("_");
  return cut > 0 ? coreStereoId.slice(0, cut) : coreStereoId;
}

/** The heading of a search result, by level. */
export function findHeading(result: FindResult): string {
  const first = result.variants[0]?.core_stereo_id ?? null;
  if (result.level === "core") {
    const core = first ? corePart(first) : String(result.core_no);
    return `CORE ${core}, all stereo variants`;
  }
  const compound = first ?? result.query;
  if (result.level === "batch" && result.matched_id) return `Compound ${compound} (from ${result.matched_id})`;
  return `Compound ${compound}`;
}

/** "2026-11-17T10:22:31+00:00" -> "2026-11-17"; anything else unchanged. */
export function datePart(value: string): string {
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(value);
  return m ? m[1]! : value;
}
