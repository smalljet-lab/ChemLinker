/**
 * Where the panel gets its products from.
 *
 * CONTRACT between the panel and whatever supplies structures:
 *   the Sciformation site source (reads the experiment page; the normal case)
 *   the manual form (paste / drop a molfile or SMILES; the fallback).
 *
 * The panel renders one card per `ProductInput` and never looks at the page
 * itself; a source never talks to the host.
 */

import type { StructureInput } from "./protocol";

/** One Sciformation experiment. */
export interface ExperimentContext {
  /**
   * Stable identifier used for idempotency: Sciformation's internal key as
   * text, e.g. "4180". Never the human code.
   */
  ref: string;
  /** Human code, e.g. "ABC-008". */
  code: string | null;
  /** Link that opens the experiment; must be on SITE_ORIGIN. */
  url: string | null;
}

/**
 * The yields the experiment page shows for one product row, in percent as
 * shown (e.g. 76.2); null = that field is empty.
 */
export interface PageYields {
  isolated: number | null;
  analytical: number | null;
}

/** One product whose ID can be looked up or created. */
export interface ProductInput {
  /** 1-based position of the product in the reaction. */
  productIndex: number;
  structure: StructureInput;
  /** Short text shown on the card, e.g. "Product 2" or a file name. */
  label: string;
  /**
   * Changes whenever the structure may have changed. A card whose token
   * changed is analysed again, and any confirmation the user gave is dropped.
   */
  changeToken: string;
  /**
   * What the page shows as this product's yields RIGHT NOW in its saved state:
   *   an object   the yield fields were read while the experiment is shown
   *               read-only; the panel stores them with the product's ID when
   *               they differ from what the registry holds (also when both are
   *               null: the registry mirrors the page);
   *   null        the page has yield fields, but they must not be used now
   *               (the experiment is being edited, so a value may be half
   *               typed or unsaved, or the fields could not be read): the
   *               panel stores nothing and keeps what the registry holds;
   *   absent      this source knows no yields (the manual form).
   * A change of the yields alone is published as a new state WITHOUT changing
   * `changeToken`: the structure is the same, nothing is analysed again.
   */
  yields?: PageYields | null;
}

/** A product the source saw but could not turn into a structure. */
export interface SkippedProduct {
  productIndex: number;
  label: string;
  /** For the user, e.g. "No structure is drawn for this product." */
  reason: string;
  /** True while the source is still working on it (reading the structure). */
  pending?: boolean;
}

export type SourceState =
  /** No experiment page / nothing entered yet. `message` says what is missing. */
  | { kind: "idle"; message: string }
  /**
   * `experiment` null: products can be looked up but no ID can be created
   * (for example an experiment that was never saved). `notice` explains why.
   */
  | {
      kind: "ready";
      experiment: ExperimentContext | null;
      products: ProductInput[];
      /** Products without a usable structure, in page order. */
      skipped?: SkippedProduct[];
      notice?: string;
    }
  /** The source failed, e.g. "page layout changed - extraction failed". */
  | { kind: "error"; message: string };

export interface ProductSource {
  /** Shown as the source's name, e.g. "Manual input". */
  readonly name: string;
  /**
   * True: the panel looks a product up as soon as it appears or its
   * `changeToken` changes (lookups are read-only). False: the user presses
   * "Look up".
   */
  readonly autoLookup: boolean;
  /**
   * True: "Create ID" first shows a confirmation naming what will be created
   * (used where the experiment is typed by hand). False: one click creates
   * the ID; experiment and product number come from the page itself.
   */
  readonly confirmBeforeCreate: boolean;
  /**
   * Re-reads this product from its origin NOW and compares it with
   * `product.structure`:
   *   "unchanged"    the origin still shows exactly this structure;
   *   "changed"      it shows something else (the source then publishes its new state);
   *   "unavailable"  it could not be checked (`message` says why).
   * The panel calls it immediately before creating an ID and creates nothing
   * unless the answer is "unchanged": an ID is permanent, and a page can change
   * behind the panel without the source noticing. Sources whose structure
   * cannot change behind the panel (the manual form) leave it out.
   */
  recheck?(product: ProductInput): Promise<{ result: "unchanged" | "changed" } | { result: "unavailable"; message: string }>;
  /** Current state; cheap, called after every change notification. */
  getState(): SourceState;
  /** Register a listener called whenever `getState()` would return something new. Returns an unsubscribe function. */
  subscribe(listener: () => void): () => void;
}
