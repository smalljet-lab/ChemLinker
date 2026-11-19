/**
 * The structure editor of the compounds page, behind a small interface so the
 * page logic can be tested without a canvas (tests pass a fake). The real one
 * is OpenChemLib's canvas editor (`ocl-editor.ts`, bundled into compounds.js only).
 */

export interface StructureEditor {
  /** The drawing as a molfile, or null when nothing is drawn. */
  getMolfile(): string | null;
  /** Removes the drawing. */
  clear(): void;
  destroy(): void;
}

/** Creates the editor inside `container` (which is visible and has its size). May throw. */
export type EditorFactory = (container: HTMLElement) => StructureEditor;
