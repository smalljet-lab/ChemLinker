/**
 * The drawing on a capture card (`renderMolfileSvg`, the real OpenChemLib):
 * only the structure with its wedges. A molfile from RDKit has no chiral flag;
 * OpenChemLib then labels every stereocentre "&1" (a racemic group) and adds
 * R/S and E/Z letters, although the SMILES on the same card is one enantiomer.
 */

import { Molecule } from "openchemlib";
import { describe, expect, it } from "vitest";
import { renderMolfileSvg } from "../src/compounds/ocl-editor";

/** The text labels of an SVG, in order. */
const labels = (svg: string): string[] => [...svg.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map((m) => m[1]!);

/** A V2000 molfile with 2D coordinates and chiral flag 0, as the host sends it. */
function molfile(smiles: string): string {
  const molecule = Molecule.fromSmiles(smiles);
  molecule.inventCoordinates();
  const lines = molecule.toMolfile().split("\n");
  // Counts line: aaabbblllfffcccsss...; ccc (columns 13 to 15) is the chiral flag.
  lines[3] = `${lines[3]!.slice(0, 12)}  0${lines[3]!.slice(15)}`;
  return lines.join("\n");
}

const STEREO_LABEL = /^(?:R|S|E|Z|r|s|\?|&amp;\d+|&\d+|or\d+|abs|rac)$/;

describe("the structure drawn on a card", () => {
  it.each([
    ["an amino acid with one stereocentre", "C[C@H](N)C(=O)O"],
    ["a double bond with Z geometry", "C/C=C\\C(=O)O"],
    ["three stereocentres on a ring", "CC(=O)N[C@@H]1[C@@H](N)CC(C(=O)O)=C[C@H]1OC(C)C"],
  ])("%s gets no stereo group, R/S or E/Z label", (_name, smiles) => {
    const source = molfile(smiles);
    const svg = renderMolfileSvg(source, 260, 170);
    expect(svg).toMatch(/^<svg /);
    expect(labels(svg!).filter((label) => STEREO_LABEL.test(label.trim()))).toEqual([]);

    // The check is not empty: without the two options OpenChemLib does print such labels.
    const plain = Molecule.fromMolfile(source).toSVG(260, 170, "plain", { autoCrop: true, autoCropMargin: 8, suppressChiralText: true });
    expect(labels(plain).filter((label) => STEREO_LABEL.test(label.trim())).length).toBeGreaterThan(0);
  });

  it("still draws the atoms", () => {
    const svg = renderMolfileSvg(molfile("C[C@H](N)C(=O)O"), 260, 170)!;
    expect(labels(svg).join(" ")).toMatch(/N/);
    expect(labels(svg).join(" ")).toMatch(/O/);
  });

  it("gives nothing for text that is no molfile or an empty one", () => {
    expect(renderMolfileSvg("not a molfile", 260, 170)).toBeNull();
  });
});
