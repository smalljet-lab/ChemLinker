/**
 * OpenChemLib's canvas editor (openchemlib on npm, BSD-3-Clause), bundled
 * locally into compounds.js. The editor draws its toolbar from images embedded
 * in the library and needs no resource files (`Resources` is only used by the
 * 3D / force-field / toxicity parts, which this page does not call), no
 * network and no eval.
 *
 * The ONLY file that imports openchemlib; only `src/compounds/index.ts`
 * imports this file (checked by tests/static-checks.test.ts and by build.mjs).
 */

import { CanvasEditor } from "openchemlib";
import type { EditorFactory, StructureEditor } from "./editor";

export const createOclEditor: EditorFactory = (container: HTMLElement): StructureEditor => {
  const editor = new CanvasEditor(container, { initialMode: "molecule" });
  return {
    getMolfile(): string | null {
      const molecule = editor.getMolecule();
      if (molecule.getAllAtoms() === 0) return null;
      try {
        return molecule.toMolfile();
      } catch {
        // V2000 cannot hold every drawing (e.g. more than 999 atoms).
        return molecule.toMolfileV3();
      }
    },
    clear(): void {
      editor.clearAll();
    },
    destroy(): void {
      if (!editor.isDestroyed) editor.destroy();
    },
  };
};
