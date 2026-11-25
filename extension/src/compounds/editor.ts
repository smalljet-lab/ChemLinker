/**
 * The structure editor of the compounds page and of the capture page, behind a
 * small interface so the page logic can be tested without a canvas (tests pass
 * a fake). The real one is OpenChemLib's canvas editor (`ocl-editor.ts`,
 * bundled into compounds.js and capture.js only).
 */

export interface StructureEditor {
  /** The drawing as a molfile, or null when nothing is drawn. */
  getMolfile(): string | null;
  /** Replaces the drawing with the structure of `molfile`. Throws when it cannot be read. */
  setMolfile(molfile: string): void;
  /**
   * A text that stays the same while the structure is the same, however it is
   * moved or scaled, and changes with every atom, bond, charge, isotope or
   * stereo change; "" for an empty drawing. Used to tell whether the user
   * changed a drawing. (The molfile cannot be compared for that: the editor
   * rescales the coordinates when it first shows a structure.) Optional: an
   * editor without it has its molfiles compared.
   */
  getSignature?(): string;
  /** Removes the drawing. */
  clear(): void;
  destroy(): void;
}

/** Creates the editor inside `container` (which is visible and has its size). May throw. */
export type EditorFactory = (container: HTMLElement) => StructureEditor;

/**
 * Draws a molfile as an SVG document (text), or returns null when it cannot be
 * read. The text is only ever used as `<img src="data:image/svg+xml,...">`.
 */
export type StructureRenderer = (molfile: string, width: number, height: number) => string | null;
