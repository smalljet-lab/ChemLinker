/**
 * Import of many experiments from an experiment list exported by the ELN.
 *
 * CONTRACT between the two halves of the import:
 *
 *   the compounds page   (`compounds.html`, "Import" view) reads the CSV file
 *                        the user exported from the ELN's experiment list,
 *                        opens the listed experiments one after the other in
 *                        ONE browser tab of its own, and shows what happened;
 *   the content script   in that tab reads the experiment page exactly as the
 *                        panel does and, when told to, looks the products up
 *                        and creates their IDs.
 *
 * Why two halves: the CSV holds no structures (only numbers, formulas and
 * yields), so every structure still comes from the experiment page; and IDs
 * are only ever created from the ELN page by the content script (the page
 * "never analyses or registers", see PAGE_OPS). The page therefore sends no
 * structure and no host request for the import; it only says WHICH products of
 * the experiment shown are to be handled.
 *
 * Transport: the page calls `browser.tabs.sendMessage(tabId, request)` (all
 * frames of the tab). A frame answers ONLY when it shows an experiment (or
 * something that should be one but cannot be read); every other frame does not
 * answer at all, so that the experiment frame's answer is the one delivered.
 * No answer (undefined, or a rejected call because no content script is there
 * yet) means "still loading" to the page.
 *
 * The content script serves these messages only when
 * `sender.id === browser.runtime.id` and `sender.url` is the extension's
 * COMPOUNDS_PAGE (query and fragment ignored). Everything else: no answer.
 *
 * Nothing here changes the host protocol.
 */

import type { PageYields } from "./adapter";
import type { AnalysisStatus, StereoMode } from "./protocol";
import { SITE_ORIGIN } from "./site";

/**
 * The link that opens one experiment read-only (slow: the ELN needs 10 to 20 s).
 * Sciformation is expected at the root of the ELN host.
 */
export function experimentPageUrl(ref: string): string {
  return `${SITE_ORIGIN}/startUseCase?useCase=performSearch&table=ElnReaction&pk=${encodeURIComponent(ref)}&viewMode=1`;
}

// ------------------------------------------------------------------ status

/** "What do you show right now?" Read-only, cheap, asked about once a second. */
export interface ImportStatusRequest {
  type: "import-status";
  /** The experiment the page expects (Sciformation's internal key as text, e.g. "4180"). */
  ref: string;
}

/** One product row of the experiment shown. */
export interface ImportPageProduct {
  /** 1-based row position: the registry's product number. */
  index: number;
  /** True when the row's structure was read and can be looked up. */
  structure: boolean;
  /** Why there is no usable structure (for the user); null when `structure` is true. */
  problem: string | null;
  /**
   * The yields the row shows in its saved state; null when the yield fields
   * must not be used now (the experiment is being edited or they could not be
   * read). Same meaning as `ProductInput.yields`.
   */
  yields: PageYields | null;
}

export type ImportStatusReply =
  /** The frame shows another experiment than `ref` (e.g. the previous one, before the tab navigated). */
  | { state: "other"; ref: string }
  /** The experiment `ref` is shown but its structures are still being read. */
  | { state: "reading" }
  /** The frame should show an experiment but it cannot be read (layout changed, page scripts missing). */
  | { state: "error"; message: string }
  /**
   * Everything that can be read was read: no product is pending any more.
   * `products` lists EVERY product row in page order (also rows without a
   * structure). The page itself waits until two answers in a row are equal
   * before it trusts one (the ELN renders in steps).
   */
  | {
      state: "ready";
      experiment: { ref: string; code: string | null };
      products: ImportPageProduct[];
    };

// ------------------------------------------------------------------ apply

/**
 * "Handle these products of the experiment you show." The content script
 * works through `products` in ascending order, one at a time, and answers
 * once, when all are done.
 *
 * For each product:
 *   1. `analyze` with the molfile read from the page, the experiment and the
 *      product number, stereo mode as detected (no override).
 *   2. status "registered_here": outcome "exists" (nothing is created).
 *   3. otherwise, when the analysis needs a human decision, outcome
 *      "attention" and nothing is created. That is the case when
 *        - `slot_conflict` is set (the row already holds another compound's ID),
 *        - `structure.ignored_stereo` is not empty (it would need an explicit
 *          acceptance),
 *        - `structure.warnings` is not empty and `allow_warnings` is false.
 *   4. otherwise, with `dry_run`: outcome "would_create", nothing is sent.
 *   5. otherwise: the structure is re-read from the page (`recheck` must say
 *      "unchanged", as before every "Create ID"), then `register` is sent once
 *      with `confirm` from that analysis: outcome "created". Any failure:
 *      outcome "failed" with the message; the remaining products are still
 *      handled.
 *   6. for "exists" and "created", not in a dry run: when the row's yields are
 *      usable (`yields` not null) and differ from what the registry holds for
 *      that ID, `set_yield` is sent (the registry mirrors the page).
 *   7. for "created" with `stereo_created`, when `names` is true:
 *      `generate_name` once (no name is never an error).
 *
 * After the products, not in a dry run, when at least one product of the
 * experiment is "exists" or "created": the experiment's reaction is stored
 * once (`set_reaction`, exactly what the panel stores after "Create ID").
 *
 * The request is refused (`ok: false`) when the frame does not show `ref`,
 * when its products are not all read yet, or while another apply is running
 * in this frame. A product number that the page does not show gets the
 * outcome "failed".
 */
export interface ImportApplyRequest {
  type: "import-apply";
  ref: string;
  /** Product numbers to handle (1-based, each at most once). May be empty: then only `ok: true` with no outcomes. */
  products: number[];
  /** True: look up only, create and store nothing. */
  dry_run: boolean;
  /** True: products whose structure analysis carries warnings may get an ID too. */
  allow_warnings: boolean;
  /** True: ask the naming program for a name after a new stereo variant was created. */
  names: boolean;
}

export type ImportOutcomeKind = "exists" | "created" | "would_create" | "attention" | "failed";

export type ImportYieldResult =
  /** `set_yield` stored other values than before. */
  | "saved"
  /** The registry already held these values (or there is nothing to store and nothing stored). */
  | "unchanged"
  /** `set_yield` failed (the outcome's `notes` say why). */
  | "failed"
  /** Not attempted: dry run, no ID, or the page's yields are not usable. */
  | "none";

export interface ImportOutcome {
  index: number;
  kind: ImportOutcomeKind;
  /** The full ID that exists or was created; null for the other kinds. */
  id: string | null;
  /** Its CORE_STEREO; for "would_create" / "attention" the variant the compound already has, if registered. */
  core_stereo_id: string | null;
  /** How the structure related to the registry BEFORE this product was handled; null when the analysis failed. */
  status: AnalysisStatus | null;
  /**
   * Groups products that are the same compound for the registry (the
   * analysis' `key_digest`); null when the analysis failed. Only compared for
   * equality, never shown.
   */
  compound_key: string | null;
  inchikey: string | null;
  /** Sum formula of the structure read from the page (Hill order, as the registry shows it). */
  formula: string | null;
  stereo_mode: StereoMode | null;
  /** For "created": true when this product opened a new CORE / a new stereo variant. */
  core_created: boolean;
  stereo_created: boolean;
  /** For "attention" and "failed": the reason, for the user. Null otherwise. */
  message: string | null;
  /** Chemistry warnings of the analysis plus anything else worth showing (failed yield save, folder warning, ...). */
  notes: string[];
  /** The yields the page shows for the row (as in the status answer). */
  yields: PageYields | null;
  yield_result: ImportYieldResult;
  /** The name stored for the compound after this product was handled, if known. */
  name: string | null;
}

export type ImportReactionResult =
  /** `set_reaction` was sent and accepted. */
  | "stored"
  /** Sent, but refused or failed (`reaction_message` says why). */
  | "failed"
  /** Not attempted (dry run, or no product of the experiment has an ID). */
  | "none";

export type ImportApplyReply =
  | {
      ok: true;
      experiment: { ref: string; code: string | null };
      /** One entry per requested product number, in ascending order. */
      products: ImportOutcome[];
      reaction: ImportReactionResult;
      reaction_message: string | null;
    }
  | { ok: false; message: string };

export type ImportRequest = ImportStatusRequest | ImportApplyRequest;

// ------------------------------------------------------------------ limits and timing

/** Product numbers per apply request (the host's MAX_PRODUCT_INDEX is far above any real experiment). */
export const MAX_IMPORT_PRODUCTS = 50;
/** How often the page asks for the status while an experiment loads. */
export const IMPORT_POLL_MS = 1000;
/** After this long without a "ready" answer the experiment counts as not opened. */
export const IMPORT_OPEN_TIMEOUT_MS = 120_000;
/** After this long without an answer to an apply request the experiment counts as failed. */
export const IMPORT_APPLY_TIMEOUT_MS = 300_000;
/** Pause between two experiments, so that the ELN server is not hammered. */
export const IMPORT_PAUSE_MS = 1500;
/** The run stops by itself after this many experiments in a row that could not be opened (ELN logged out?). */
export const IMPORT_MAX_OPEN_FAILURES = 3;
