/**
 * OpenChemLib's canvas editor (openchemlib on npm, BSD-3-Clause), bundled
 * locally into compounds.js and capture.js. The editor draws its toolbar from
 * images embedded in the library and needs no resource files (`Resources` is
 * only used by the 3D / force-field / toxicity parts, which these pages do not
 * call), no network and no eval.
 *
 * The ONLY file that imports openchemlib; only `src/compounds/index.ts` and
 * `src/capture/index.ts` import this file (checked by
 * tests/static-checks.test.ts and by build.mjs).
 */

import { CanvasEditor, Molecule } from "openchemlib";
import type { EditorFactory, StructureEditor, StructureRenderer } from "./editor";

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
    setMolfile(molfile: string): void {
      editor.setMolecule(Molecule.fromMolfile(molfile));
    },
    getSignature(): string {
      const molecule = editor.getMolecule();
      // OpenChemLib's canonical code of the structure: it does not depend on the coordinates.
      return molecule.getAllAtoms() === 0 ? "" : molecule.getIDCode();
    },
    clear(): void {
      editor.clearAll();
    },
    destroy(): void {
      if (!editor.isDestroyed) editor.destroy();
    },
  };
};

let svgCount = 0;

/** The largest drawing a card asks for (CSS pixels); `renderMolfileSvg` never returns a wider or taller one. */
export const CARD_DRAWING = { width: 260, height: 170 } as const;
/** White space kept around the drawing, so that a label at the edge ("NH2", "OTf") is not cut. */
const DRAWING_MARGIN = 10;

/**
 * Marks every stereocentre and stereo bond the molfile defines as "this
 * configuration" (OpenChemLib's ABS).
 *
 * A molfile without the chiral flag is, to OpenChemLib, a racemate: every
 * stereocentre is put into the group "&1", and `suppressESR` (needed to keep
 * that label off the card) then also takes the WEDGE away: the centre was
 * drawn with plain bonds while the SMILES on the same card had "@". The
 * SMILES is one enantiomer, and the wedges are what the user has to compare
 * with the picture, so the card always draws them.
 */
function asDrawn(molecule: Molecule): void {
  molecule.ensureHelperArrays(Molecule.cHelperCIP);
  const atoms: number[] = [];
  const bonds: number[] = [];
  for (let atom = 0; atom < molecule.getAllAtoms(); atom += 1) {
    if (molecule.getAtomParity(atom) !== Molecule.cAtomParityNone && molecule.getAtomESRType(atom) !== Molecule.cESRTypeAbs) atoms.push(atom);
  }
  for (let bond = 0; bond < molecule.getAllBonds(); bond += 1) {
    if (molecule.getBondParity(bond) !== Molecule.cBondParityNone && molecule.getBondESRType(bond) !== Molecule.cESRTypeAbs) bonds.push(bond);
  }
  for (const atom of atoms) molecule.setAtomESR(atom, Molecule.cESRTypeAbs, -1);
  for (const bond of bonds) molecule.setBondESR(bond, Molecule.cESRTypeAbs, -1);
}

/**
 * The structure of a molfile as SVG text, for the capture page's cards.
 *
 * The atoms stay where the molfile has them (the host puts them where the
 * picture has them). Size: a bond is at most OpenChemLib's standard length
 * (24 px next to 14 px labels), so a small molecule is drawn small, not blown
 * up; a large one is shrunk until it fits `width` x `height`. The SVG is cut
 * to the drawing plus a margin and says its own size, which is never larger
 * than `width` x `height`; the page shows it at that size or smaller.
 */
export const renderMolfileSvg: StructureRenderer = (molfile, width, height) => {
  try {
    const molecule = Molecule.fromMolfile(molfile);
    if (molecule.getAllAtoms() === 0) return null;
    asDrawn(molecule);
    svgCount += 1;
    // Only the drawing: no "abs" / "&1" / "or1" group labels and no R/S, E/Z letters; the wedges
    // say what was read. A stereocentre without a wedge is drawn plainly, not marked as an error:
    // the card's warning says that the stereo has to be checked.
    const svg = molecule.toSVG(width - 2 * DRAWING_MARGIN, height - 2 * DRAWING_MARGIN, `structure-${svgCount}`, {
      autoCrop: true,
      autoCropMargin: DRAWING_MARGIN,
      suppressChiralText: true,
      suppressESR: true,
      suppressCIPParity: true,
      noStereoProblem: true,
    });
    return fitted(svg, width, height);
  } catch {
    return null;
  }
};

/**
 * `svg` with its stated size brought down to `width` x `height` when the cut
 * drawing is larger (labels and the margin reach past the area the atoms were
 * fitted into). The viewBox is kept, so the drawing shrinks as a whole.
 */
function fitted(svg: string, width: number, height: number): string {
  const head = /^<svg[^>]*>/.exec(svg);
  if (!head) return svg;
  const w = /\swidth="([\d.]+)px"/.exec(head[0]);
  const h = /\sheight="([\d.]+)px"/.exec(head[0]);
  if (!w || !h) return svg;
  const drawnWidth = Number(w[1]);
  const drawnHeight = Number(h[1]);
  if (!(drawnWidth > 0) || !(drawnHeight > 0)) return svg;
  const scale = Math.min(1, width / drawnWidth, height / drawnHeight);
  if (scale === 1) return svg;
  const size = (value: number): string => String(Math.round(value * scale * 100) / 100);
  const tag = head[0].replace(w[0], ` width="${size(drawnWidth)}px"`).replace(h[0], ` height="${size(drawnHeight)}px"`);
  return tag + svg.slice(head[0].length);
}
