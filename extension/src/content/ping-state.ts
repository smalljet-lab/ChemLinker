/** What the panel header shows about the host, derived from a `ping` reply. */

import {
  PROTOCOL_VERSION,
  type BackgroundReply,
  type HostError,
  type NameGeneratorInfo,
  type PingResult,
} from "../shared/protocol";
import { EXTENSION_VERSION } from "../shared/site";
import { SAME_RELEASE_HINT } from "./errors";
import { NO_NAME_GENERATOR, readNameGenerator } from "./validate";

export type PingState =
  | { kind: "idle" }
  | { kind: "pending" }
  | {
      kind: "ok";
      result: PingResult;
      dbPath: string;
      /** Set when the registry folder is not the default one. */
      nonDefaultHome: string | null;
      warnings: string[];
      /** Whether names can be generated (ChemDraw on this PC); not available for an older host. */
      nameGenerator: NameGeneratorInfo;
      /** The registry program's version; null when it sent none. */
      hostVersion: string | null;
      /**
       * Set when extension and registry program come from different releases
       * (see `sameRelease`). A warning only: both speak the same protocol.
       */
      versionNotice: string | null;
    }
  | { kind: "mismatch"; hostProtocol: number; hostVersion: string | null; message: string }
  | { kind: "error"; error: HostError };

/**
 * True when two version numbers belong to the same release: their first two
 * numbers are equal ("0.10.0" and "0.10.3"). Extension and registry program
 * are released together under one number; the third number is for corrections
 * of one side. Null when either is not a version number.
 */
export function sameRelease(a: string, b: string): boolean | null {
  const release = (v: string): string | null => /^\s*(\d+)\.(\d+)(?:\D|$)/.exec(v)?.slice(1, 3).join(".") ?? null;
  const ra = release(a);
  const rb = release(b);
  return ra === null || rb === null ? null : ra === rb;
}

/** "Extension 0.10.0 · registry program 0.10.0", with the parts that are known; null when none is. */
export function versionLine(extensionVersion: string | null, hostVersion: string | null): string | null {
  const parts: string[] = [];
  if (extensionVersion) parts.push(`Extension ${extensionVersion}`);
  if (hostVersion) parts.push(`${parts.length === 0 ? "Registry" : "registry"} program ${hostVersion}`);
  return parts.length === 0 ? null : parts.join(" · ");
}

/** The registry program's version as far as a ping told it. */
export function hostVersionOf(ping: PingState): string | null {
  return ping.kind === "ok" || ping.kind === "mismatch" ? ping.hostVersion : null;
}

export function pingStateFromReply(
  reply: BackgroundReply<"ping">,
  ours: number = PROTOCOL_VERSION,
  extensionVersion: string | null = EXTENSION_VERSION,
): PingState {
  if (!reply.ok) return { kind: "error", error: reply.error };
  const result = reply.result;
  const hostVersion = typeof result.host_version === "string" && result.host_version !== "" ? result.host_version : null;
  if (result.protocol_version !== ours) {
    return {
      kind: "mismatch",
      hostProtocol: result.protocol_version,
      hostVersion,
      message:
        `The registry program speaks protocol version ${result.protocol_version}, this extension version ${ours}. ` +
        SAME_RELEASE_HINT,
    };
  }
  const differ = extensionVersion !== null && hostVersion !== null && sameRelease(extensionVersion, hostVersion) === false;
  return {
    kind: "ok",
    result,
    dbPath: result.db_path,
    nonDefaultHome: result.home_source !== "default" ? result.home : null,
    warnings: result.warnings,
    nameGenerator: readNameGenerator(result),
    hostVersion,
    versionNotice: differ
      ? `The extension is version ${extensionVersion}, the registry program version ${hostVersion}: ` +
        `they come from different releases, and some functions may not work. ${SAME_RELEASE_HINT}`
      : null,
  };
}

/** The name generator as far as known: only a successful ping of the same protocol can make it available. */
export function nameGeneratorOf(ping: PingState): NameGeneratorInfo {
  return ping.kind === "ok" ? ping.nameGenerator : NO_NAME_GENERATOR;
}
