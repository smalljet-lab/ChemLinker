/**
 * What a capture card shows of a molecule (`renderMolfileSvg`, the real
 * OpenChemLib, and the card's stylesheet): the wedges, the size, the labels,
 * the places of the atoms, and the line with the SMILES.
 *
 * The molfiles are written by hand the way the host writes them (RDKit, V2000).
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Molecule } from "openchemlib";
import { describe, expect, it } from "vitest";
import { CARD_DRAWING, renderMolfileSvg } from "../src/compounds/ocl-editor";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

type Atom = [symbol: string, x: number, y: number];
type Bond = [begin: number, end: number, order: number, stereo?: number];

/** A V2000 molfile; `chiral` is the chiral flag of the counts line. */
function molfile(atoms: Atom[], bonds: Bond[], chiral = 0): string {
  const n = (value: number, width: number): string => String(value).padStart(width, " ");
  const coordinate = (value: number): string => value.toFixed(4).padStart(10, " ");
  return [
    "",
    "     RDKit          2D",
    "",
    `${n(atoms.length, 3)}${n(bonds.length, 3)}  0  0${n(chiral, 3)}  0  0  0  0  0999 V2000`,
    ...atoms.map(([symbol, x, y]) => `${coordinate(x)}${coordinate(y)}${coordinate(0)} ${symbol.padEnd(3, " ")} 0  0  0  0  0  0  0  0  0  0  0  0`),
    ...bonds.map(([begin, end, order, stereo = 0]) => `${n(begin, 3)}${n(end, 3)}${n(order, 3)}${n(stereo, 3)}`),
    "M  END",
    "",
  ].join("\n");
}

/** 1-phenylethanamine-like: the stereocentre (atom 2) with methyl, N and a carbon chain. */
const CENTRE_ATOMS: Atom[] = [
  ["C", -1.299, -0.75],
  ["C", 0, 0],
  ["N", 0, 1.5],
  ["C", 1.299, -0.75],
  ["O", 2.598, 0],
];
const centre = (stereo: number, chiral = 0): string =>
  molfile(
    CENTRE_ATOMS,
    [
      [1, 2, 1],
      [2, 3, 1, stereo],
      [2, 4, 1],
      [4, 5, 1],
    ],
    chiral,
  );

interface Drawing {
  width: number;
  height: number;
  viewBox: [number, number, number, number];
  lines: number;
  polygons: number;
  texts: { text: string; x: number; y: number; size: number; fill: string }[];
  atoms: Map<number, { x: number; y: number }>;
  bondLengths: number[];
}

function read(svg: string): Drawing {
  const head = /^<svg\b[^>]*>/.exec(svg)![0];
  const number = (pattern: RegExp): number => Number(pattern.exec(head)![1]);
  const viewBox = /viewBox="([^"]+)"/.exec(head)![1]!.split(/\s+/).map(Number) as [number, number, number, number];
  const visible = svg.split("\n").filter((line) => !line.includes('class="event"'));
  const atoms = new Map<number, { x: number; y: number }>();
  for (const m of svg.matchAll(/<circle id="[^"]*:Atom:(\d+)"[^>]* cx="([-\d.]+)" cy="([-\d.]+)"/g)) atoms.set(Number(m[1]), { x: Number(m[2]), y: Number(m[3]) });
  const bondLengths = [...svg.matchAll(/<line id="[^"]*:Bond:\d+"[^>]* x1="([-\d.]+)" y1="([-\d.]+)" x2="([-\d.]+)" y2="([-\d.]+)"/g)].map((m) =>
    Math.hypot(Number(m[3]) - Number(m[1]), Number(m[4]) - Number(m[2])),
  );
  return {
    width: number(/\swidth="([\d.]+)px"/),
    height: number(/\sheight="([\d.]+)px"/),
    viewBox,
    lines: visible.filter((line) => line.includes("<line")).length,
    polygons: visible.filter((line) => line.includes("<polygon")).length,
    texts: [...svg.matchAll(/<text x="([-\d.]+)" y="([-\d.]+)"[^>]*font-size="([\d.]+)"[^>]*fill="([^"]+)"[^>]*>([^<]*)<\/text>/g)].map((m) => ({
      x: Number(m[1]),
      y: Number(m[2]),
      size: Number(m[3]),
      fill: m[4]!,
      text: m[5]!,
    })),
    atoms,
    bondLengths,
  };
}

const draw = (source: string): Drawing => read(renderMolfileSvg(source, CARD_DRAWING.width, CARD_DRAWING.height)!);

describe("the wedges on a card", () => {
  it("draws the wedge of a molfile WITHOUT the chiral flag (it used to be dropped)", () => {
    const flat = draw(centre(0));
    const wedge = draw(centre(1));
    const hash = draw(centre(6));
    expect(flat.polygons).toBe(0);
    expect(wedge.polygons).toBeGreaterThan(0); // a solid wedge is a filled shape
    expect(hash.polygons).toBe(0);
    expect(hash.lines).toBeGreaterThan(flat.lines + 3); // a hashed wedge is a row of short lines
  });

  it("draws the same with the chiral flag (what the host writes) and without", () => {
    for (const stereo of [1, 6]) {
      const without = draw(centre(stereo, 0));
      const withFlag = draw(centre(stereo, 1));
      expect([without.polygons, without.lines]).toEqual([withFlag.polygons, withFlag.lines]);
    }
  });

  it("draws the wedge in black like every other bond, with no stereo label next to it", () => {
    const svg = renderMolfileSvg(centre(1), 260, 170)!;
    const colours = new Set([...svg.matchAll(/<(?:polygon|line)[^>]*(?:fill|stroke)="(rgb\([^"]+\))"/g)].map((m) => m[1]));
    expect([...colours]).toEqual(["rgb(0,0,0)"]);
    expect(read(svg).texts.map((t) => t.text.trim()).filter((t) => /^(?:abs|R|S|\?|&amp;\d+|or\d+)$/.test(t))).toEqual([]);
  });

  it("the wedge is the one the molfile has: OpenChemLib reads the two as mirror images", () => {
    const up = Molecule.fromMolfile(centre(1, 1));
    const down = Molecule.fromMolfile(centre(6, 1));
    expect(up.getIDCode()).not.toBe(down.getIDCode());
    expect(up.getStereoCenterCount()).toBe(1);
  });

  it("a stereocentre without a wedge is drawn plainly: not as an error, not as a guess", () => {
    const svg = renderMolfileSvg(centre(0), 260, 170)!;
    const drawing = read(svg);
    expect(drawing.polygons).toBe(0);
    expect(drawing.lines).toBe(4);
    expect(svg).not.toMatch(/rgb\(255,0,255\)|rgb\(255, ?0, ?255\)|magenta/i); // OpenChemLib's "stereo problem" colour
    expect(drawing.texts.map((t) => t.text)).not.toContain("?");
  });

  it("keeps the colours of the heteroatoms", () => {
    const fills = new Map(draw(centre(1)).texts.map((t) => [t.text, t.fill]));
    expect(fills.get("N")).toBe("rgb(48,80,248)");
    expect(fills.get("O")).toBe("rgb(255,13,13)");
  });
});

describe("the size of the drawing on a card", () => {
  const chain = (count: number): string => {
    const atoms: Atom[] = Array.from({ length: count }, (_unused, i) => ["C", i * 1.299, i % 2 === 0 ? 0 : 0.75]);
    const bonds: Bond[] = Array.from({ length: count - 1 }, (_unused, i) => [i + 1, i + 2, 1]);
    return molfile(atoms, bonds);
  };
  /** A grid of fused-ring-like squares: wide AND tall. */
  const sheet = (columns: number, rows: number): string => {
    const atoms: Atom[] = [];
    const bonds: Bond[] = [];
    for (let r = 0; r < rows; r += 1) for (let c = 0; c < columns; c += 1) atoms.push(["C", c * 1.5, r * 1.5]);
    for (let r = 0; r < rows; r += 1)
      for (let c = 0; c < columns; c += 1) {
        const i = r * columns + c + 1;
        if (c + 1 < columns) bonds.push([i, i + 1, 1]);
        if (r + 1 < rows) bonds.push([i, i + columns, 1]);
      }
    return molfile(atoms, bonds);
  };

  it("a small molecule is drawn small: standard bonds next to standard labels, not blown up to the box", () => {
    const ethanol = draw(molfile([["C", 0, 0], ["C", 1.299, 0.75], ["O", 2.598, 0]], [[1, 2, 1], [2, 3, 1]]));
    expect(ethanol.width).toBeLessThan(110);
    expect(ethanol.height).toBeLessThan(70);
    expect(Math.max(...ethanol.bondLengths)).toBeLessThanOrEqual(24.5);
    expect(Math.min(...ethanol.bondLengths)).toBeGreaterThan(20);
    expect(ethanol.texts[0]!.size).toBe(14);

    const methane = draw(molfile([["C", 0, 0]], []));
    expect(methane.width).toBeLessThan(80);
    expect(methane.height).toBeLessThan(60);
  });

  it("the same molecule has the same size whatever the bond length of its molfile", () => {
    const scaled = (factor: number): Drawing => draw(molfile(CENTRE_ATOMS.map(([s, x, y]) => [s, x * factor, y * factor]), [[1, 2, 1], [2, 3, 1, 1], [2, 4, 1], [4, 5, 1]]));
    const small = scaled(0.55);
    const large = scaled(7);
    expect(Math.abs(small.width - large.width)).toBeLessThanOrEqual(1);
    expect(Math.abs(small.height - large.height)).toBeLessThanOrEqual(1);
  });

  it.each([
    ["a long chain", chain(60)],
    ["a tall chain", molfile(Array.from({ length: 40 }, (_unused, i): Atom => ["C", i % 2 === 0 ? 0 : 0.75, i * 1.299]), Array.from({ length: 39 }, (_unused, i): Bond => [i + 1, i + 2, 1]))],
    ["a wide and tall sheet", sheet(24, 16)],
  ])("%s shrinks until it fits; it never overflows the box", (_name, source) => {
    const drawing = draw(source);
    expect(drawing.width).toBeLessThanOrEqual(CARD_DRAWING.width);
    expect(drawing.height).toBeLessThanOrEqual(CARD_DRAWING.height);
    // The whole drawing is inside what is shown.
    const [x, y, w, h] = drawing.viewBox;
    for (const atom of drawing.atoms.values()) {
      expect(atom.x).toBeGreaterThanOrEqual(x);
      expect(atom.x).toBeLessThanOrEqual(x + w);
      expect(atom.y).toBeGreaterThanOrEqual(y);
      expect(atom.y).toBeLessThanOrEqual(y + h);
    }
    expect(Math.max(...drawing.bondLengths)).toBeLessThan(24);
  });

  it("labels at the edge of the drawing are not cut off", () => {
    // H2N on the far left, OH on the far right, an NH2 at the top and a Br at the bottom.
    const source = molfile(
      [
        ["N", -2.598, 0],
        ["C", -1.299, 0.75],
        ["C", 0, 0],
        ["O", 1.299, 0.75],
        ["N", -1.299, 2.25],
        ["Br", 0, -1.5],
      ],
      [
        [1, 2, 1],
        [2, 3, 1],
        [3, 4, 1],
        [2, 5, 1],
        [3, 6, 1],
      ],
    );
    const drawing = draw(source);
    const [x, y, w, h] = drawing.viewBox;
    expect(drawing.texts.map((t) => t.text).join("")).toMatch(/Br/);
    for (const label of drawing.texts) {
      const width = label.text.length * label.size * 0.75; // generous
      expect(label.x).toBeGreaterThanOrEqual(x + 1);
      expect(label.x + width).toBeLessThanOrEqual(x + w - 1);
      expect(label.y - label.size).toBeGreaterThanOrEqual(y + 1);
      expect(label.y).toBeLessThanOrEqual(y + h - 1);
    }
  });
});

describe("the places of the atoms on a card", () => {
  it("are the places of the molfile: left stays left, up stays up (the host puts them where the picture has them)", () => {
    // An L: atom 1 at the top, atom 2 the corner, atom 3 to the RIGHT of the corner.
    const drawing = draw(molfile([["C", 0, 3], ["C", 0, 0], ["N", 3, 0]], [[1, 2, 1], [2, 3, 1]]));
    const [top, corner, right] = [drawing.atoms.get(0)!, drawing.atoms.get(1)!, drawing.atoms.get(2)!];
    expect(top.y).toBeLessThan(corner.y - 10); // SVG y grows downwards
    expect(Math.abs(top.x - corner.x)).toBeLessThan(0.5);
    expect(right.x).toBeGreaterThan(corner.x + 10);
    expect(Math.abs(right.y - corner.y)).toBeLessThan(0.5);
  });

  it("are not laid out again: the mirror image of a molfile is drawn mirrored", () => {
    const atoms: Atom[] = [["C", 0, 0], ["C", 1.3, 0.75], ["O", 2.6, 0], ["N", 1.3, 2.25]];
    const bonds: Bond[] = [[1, 2, 1], [2, 3, 1], [2, 4, 1]];
    const one = draw(molfile(atoms, bonds));
    const mirrored = draw(molfile(atoms.map(([s, x, y]) => [s, -x, y]), bonds));
    const side = (d: Drawing): number => Math.sign(d.atoms.get(2)!.x - d.atoms.get(0)!.x);
    expect(side(one)).toBe(1);
    expect(side(mirrored)).toBe(-1);
  });
});

describe("the line with the SMILES on a card (capture.css)", () => {
  const css = readFileSync(join(root, "static", "capture.css"), "utf8");
  /** The declarations of the rule with exactly this selector. */
  const rule = (selector: string): string => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const found = new RegExp(`(?:^|})\\s*${escaped}\\s*\\{([^}]*)\\}`, "m").exec(css.replace(/\/\*[\s\S]*?\*\//g, ""));
    expect(found, selector).not.toBeNull();
    return found![1]!.replace(/\s+/g, " ");
  };

  it("breaks a long SMILES anywhere inside its own box and never lets it run out of the card", () => {
    const mono = css.split("\n").find((line) => line.startsWith(".mono "))!;
    expect(mono).toContain("overflow-wrap: anywhere");
    expect(mono).toContain("word-break: break-all");
    expect(mono).toMatch(/font-family: ui-monospace/);
    const smiles = rule(".smiles");
    expect(smiles).toContain("min-width: 0"); // a flex item may get narrower than its text
    expect(smiles).toContain("flex: 1 1 0");
    expect(smiles).toContain("overflow-x: hidden");
    expect(smiles).toContain("overflow-y: auto"); // a very long one scrolls inside its box
    expect(smiles).toMatch(/max-height: [\d.]+em/);
    expect(smiles).not.toMatch(/white-space: nowrap|text-overflow/);
    expect(smiles).toContain("user-select: text");
  });

  it("keeps the Copy button next to it at its full size", () => {
    expect(rule(".smiles-row")).toContain("display: flex");
    expect(rule(".smiles-row > .btn")).toContain("flex: none");
    expect(rule(".rxn-line > .btn")).toContain("flex: none");
  });

  it("shows the drawing at its own size or smaller, never enlarged", () => {
    const structure = rule(".structure");
    expect(structure).toContain("object-fit: scale-down");
    expect(structure).toContain(`height: ${CARD_DRAWING.height}px`);
    expect(structure).toContain("width: 100%");
  });
});
