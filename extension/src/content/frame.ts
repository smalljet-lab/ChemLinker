/**
 * One frame document: the panel exists only while the page source shows an
 * experiment (state not `idle`), and is removed again when it becomes idle.
 * No experiment in a frame: no UI at all in that frame.
 *
 * Exception (`alwaysMounted`): the ELN's main content frame keeps its panel
 * all the time, so the search by ID can be used from anywhere in the ELN; the
 * same panel instance then keeps its open/closed state and its last search
 * result while experiments come and go.
 */

import type { ProductSource } from "../shared/adapter";
import type { PanelHandle } from "./ui/panel";

export interface FrameController {
  /** The mounted panel, or null. */
  panel(): PanelHandle | null;
  stop(): void;
}

export interface FrameOptions {
  /** Keep the panel mounted even while no experiment is shown. */
  alwaysMounted?: boolean;
}

/** The ELN shows its pages in an iframe with this name. */
export const CONTENT_FRAME_NAME = "contentFrame";

export function controlFrame(source: ProductSource, mount: () => PanelHandle, options: FrameOptions = {}): FrameController {
  let panel: PanelHandle | null = null;
  const sync = (): void => {
    const idle = !options.alwaysMounted && source.getState().kind === "idle";
    if (!idle && panel === null) {
      panel = mount();
    } else if (idle && panel !== null) {
      panel.destroy();
      panel = null;
    }
  };
  const unsubscribe = source.subscribe(sync);
  sync();
  return {
    panel: () => panel,
    stop: () => {
      unsubscribe();
      panel?.destroy();
      panel = null;
    },
  };
}
