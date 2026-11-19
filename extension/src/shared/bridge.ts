/**
 * Messages between the content script (isolated world) and the page bridge
 * (`src/page/bridge.ts`, runs in the page's own world, where the ELN's `zk`
 * and `zAu` objects live).
 *
 * CONTRACT. Transport: `CustomEvent`s dispatched on `document`, with
 * `detail` set to a JSON STRING (strings cross the world boundary unchanged).
 * The bridge answers every request exactly once, with the request's `id`.
 *
 * The event names end with the extension version. The bridge lives in the
 * page and so outlives an extension update: a tab that was open during the
 * update still holds the old bridge. The new content script then talks to the
 * new bridge (installed next to the old one, which never hears its requests),
 * not to code of another release.
 *
 * The page can see and forge these events. That is acceptable: the bridge
 * only does what the page can already do, and everything the content script
 * receives is treated as untrusted text (it goes to the host, which parses it
 * as a structure, and is never interpreted as HTML or code).
 */

import { EXTENSION_VERSION } from "./site";

/** Names the bridge of one release; "dev" where the code was not built by build.mjs. */
export const BRIDGE_VERSION = EXTENSION_VERSION ?? "dev";

/** content script -> bridge */
export const BRIDGE_REQUEST_EVENT = `compound-registry-bridge-request/${BRIDGE_VERSION}`;
/** bridge -> content script */
export const BRIDGE_RESPONSE_EVENT = `compound-registry-bridge-response/${BRIDGE_VERSION}`;
/** Property of the page's `window` that marks this release's bridge as installed. */
export const BRIDGE_INSTALLED_FLAG = `__compoundRegistryBridgeInstalled/${BRIDGE_VERSION}`;

export type BridgeRequest =
  /** Is the bridge loaded, and does this frame have the ELN's ZK runtime? */
  | { id: string; kind: "hello" }
  /**
   * Molfile of the structure shown by the ZK macro whose root element has this
   * DOM id (the `div.z-macro[smiles]` of a product row). Read-only on the server.
   */
  | { id: string; kind: "molfile"; domId: string }
  /**
   * What the experiment in this frame shows about its reaction, read through
   * the ELN's own component ids (`$elnReactionReactants`, `$elnReactionReagents`,
   * `$elnReactionSolvents`, `$solvent`, `$solventAmount`, `$solvent_volume_unit`,
   * `$temperature`, `$refAmount`, `$displayActual`, `$reactant_amount_unit`,
   * `$reactant_mass_unit`, `$reactant_volume_unit`). Read-only; texts only;
   * structures are NOT included (ask `molfile` with each row's `handle`).
   */
  | { id: string; kind: "reaction" };

export type BridgeErrorCode =
  | "bad_request" // unreadable request
  | "no_zk" // this frame has no ZK runtime (not an ELN page)
  | "no_widget" // no ZK widget for that element (the page was re-rendered)
  | "timeout" // the server did not answer in time
  | "fetch_failed" // the molfile download failed
  | "too_large" // molfile over MAX_STRUCTURE_CHARS
  | "unexpected"; // anything else

export type BridgeResponse =
  | { id: string; ok: true; kind: "hello"; zk: boolean }
  | { id: string; ok: true; kind: "molfile"; molfile: string }
  | { id: string; ok: true; kind: "reaction"; reaction: PageReaction }
  | { id: string; ok: false; code: BridgeErrorCode; message: string };

/**
 * How long the bridge gives one molfile read, from the request to the server
 * until the downloaded text is complete, before answering `timeout`.
 */
export const BRIDGE_MOLFILE_TIMEOUT_MS = 15_000;

/**
 * The amounts of one grid row exactly as its inputs show them (texts, e.g.
 * "2313", "0,915", ""), for the "Actual amounts" or the "Planned amounts"
 * set. Each cell of a row holds two inputs: the first is "Actual", the
 * second "Plan" (verified on the live site, also while one of them is hidden).
 */
export interface PageAmounts {
  eq: string | null;
  n: string | null;
  m: string | null;
  v: string | null;
}

/** One row of the reactants or reagents grid, as shown. */
export interface PageComponent {
  /** 1-based position in its grid. */
  row: number;
  /** DOM id of the row's structure macro (`div.z-macro[smiles]`) for a `molfile` request; null when none. */
  handle: string | null;
  /** That macro's `smiles` attribute: change detector only, never a structure. */
  smiles: string | null;
  /** First label of the name cell (container text excluded, trailing comma removed). */
  name: string | null;
  formula: string | null;
  cas: string | null;
  /** From a link `table=ElnReaction&pk=<key>` in the name cell: the key and the link's text. */
  sourceRef: string | null;
  sourceCode: string | null;
  actual: PageAmounts;
  planned: PageAmounts;
}

/** What the page shows about the reaction (bridge answer to `reaction`). */
export interface PageReaction {
  /** True when every input read is read-only (the experiment is shown saved, not edited). */
  readOnly: boolean;
  /** Which amounts radio is selected; null when it cannot be told. */
  amountsShown: "actual" | "planned" | null;
  /** Units selected for the reactants grid (the reagents grid uses the same columns). */
  units: { n: string | null; m: string | null; v: string | null };
  reactants: PageComponent[];
  reagents: PageComponent[];
  /**
   * Rows of the solvents grid ({name: first label, volume text}), or, when the
   * grid is empty, the `$solvent` field with `$solventAmount` (one item, or
   * none when the field is empty). `unit` from `$solvent_volume_unit`.
   */
  solvents: { name: string; volume: string | null; unit: string | null }[];
  temperature: string | null;
  referenceAmount: { value: string; unit: string | null } | null;
}
