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

/** A bond at most this far from vertical (degrees) may be turned upright by `hydrogensToTheRight`. */
const UPRIGHT_LIMIT = 35;

/**
 * OpenChemLib writes the hydrogens of a label ("NH2", "OH") on the LEFT as
 * soon as the atom's only bond leans to the right at all ("H2N"), where
 * ChemDraw keeps them on the right for a bond that is nearly vertical. When
 * that left side is taken by another atom (the ortho F next to the NH2 of
 * sitagliptin: the card read "FH2N"), the end atom is turned about its
 * neighbour until the bond is vertical, so the hydrogens go to the right as
 * in the picture. Only for a bond within `UPRIGHT_LIMIT` degrees of vertical
 * and only when no other bond of the neighbour lies in the way, so the
 * wedges keep their meaning. For the card's drawing only.
 */
function hydrogensToTheRight(molecule: Molecule): void {
  molecule.ensureHelperArrays(Molecule.cHelperNeighbours);
  const bond = molecule.getAverageBondLength(true);
  if (!(bond > 0)) return;
  const all = molecule.getAllAtoms();
  for (let atom = 0; atom < all; atom += 1) {
    if (molecule.getAtomicNo(atom) === 6 || molecule.getConnAtoms(atom) !== 1 || molecule.getAllHydrogens(atom) === 0) continue;
    const next = molecule.getConnAtom(atom, 0);
    const x = molecule.getAtomX(atom);
    const y = molecule.getAtomY(atom);
    const dx = molecule.getAtomX(next) - x;
    const dy = molecule.getAtomY(next) - y;
    const length = Math.hypot(dx, dy);
    if (!(dx > 1e-6) || !(length > 0) || (Math.atan2(dx, Math.abs(dy)) * 180) / Math.PI > UPRIGHT_LIMIT) continue;
    const crowded = (from: number, to: number, besideY: number): boolean => {
      for (let other = 0; other < all; other += 1) {
        if (other === atom || other === next) continue;
        const ox = molecule.getAtomX(other);
        const oy = molecule.getAtomY(other);
        if (ox > from && ox < to && Math.abs(oy - besideY) < 0.5 * bond) return true;
      }
      return false;
    };
    if (!crowded(x - 1.3 * bond, x, y)) continue;
    const newX = molecule.getAtomX(next);
    const newY = molecule.getAtomY(next) + (dy > 0 ? -length : length);
    if (crowded(newX, newX + 1.3 * bond, newY)) continue;
    // The turn must not sweep over another bond of the neighbour.
    const from = Math.atan2(y - molecule.getAtomY(next), x - newX);
    const to = Math.atan2(newY - molecule.getAtomY(next), 0);
    let blocked = false;
    for (let k = 0; k < molecule.getAllConnAtoms(next); k += 1) {
      const other = molecule.getConnAtom(next, k);
      if (other === atom) continue;
      const angle = Math.atan2(molecule.getAtomY(other) - molecule.getAtomY(next), molecule.getAtomX(other) - newX);
      const sweep = Math.atan2(Math.sin(to - from), Math.cos(to - from));
      const part = Math.atan2(Math.sin(angle - from), Math.cos(angle - from));
      if (Math.sign(part) === Math.sign(sweep) && Math.abs(part) <= Math.abs(sweep)) blocked = true;
    }
    if (blocked) continue;
    molecule.setAtomX(atom, newX);
    molecule.setAtomY(atom, newY);
  }
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
    hydrogensToTheRight(molecule);
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
