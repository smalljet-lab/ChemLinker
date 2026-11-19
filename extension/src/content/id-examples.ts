/**
 * Example IDs for hints and placeholders, in the format of the registry in
 * use (`ping.id_format`, e.g. "EX{core:04d}_{stereo:02d}_{batch:03d}").
 * Display only: IDs are never built or parsed by the extension.
 */

import type { PingState } from "./ping-state";

export interface IdExamples {
  /** A CORE, e.g. "EX0042". */
  core: string;
  /** A compound (CORE_STEREO), e.g. "EX0042_02". */
  coreStereo: string;
  /** A full ID, e.g. "EX0042_02_003". */
  full: string;
}

export const DEFAULT_ID_FORMAT = "{core:06d}_{stereo:02d}_{batch:03d}";

const TEMPLATE_RE = /^([^{}0-9]*)\{core:0([1-9][0-9]?)d\}([^{}0-9]*)\{stereo:0([1-9][0-9]?)d\}([^{}0-9]*)\{batch:0([1-9][0-9]?)d\}([^{}0-9]*)$/u;

function pad(value: number, width: number): string {
  return String(value).padStart(width, "0");
}

/** Examples for a template of the host; an unreadable one gives the default format's examples. */
export function idExamples(template: string | null | undefined): IdExamples {
  const m = typeof template === "string" && template.length <= 200 ? TEMPLATE_RE.exec(template) : null;
  if (m === null) {
    return template === DEFAULT_ID_FORMAT
      ? { core: "000042", coreStereo: "000042_02", full: "000042_02_003" }
      : idExamples(DEFAULT_ID_FORMAT);
  }
  const core = `${m[1]}${pad(42, Number(m[2]))}`;
  const coreStereo = `${core}${m[3]}${pad(2, Number(m[4]))}`;
  return { core, coreStereo, full: `${coreStereo}${m[5]}${pad(3, Number(m[6]))}${m[7]}` };
}

/** Examples for the registry a ping answered for; the default format until then. */
export function idExamplesOf(ping: PingState): IdExamples {
  return idExamples(ping.kind === "ok" ? ping.result.id_format : null);
}
