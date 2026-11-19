/**
 * Comparisons used by the import: sum formulas by composition (the ELN's list
 * writes "BrC7H5O", the registry "C7H5BrO") and yields "equal within the
 * ELN's rounding". No DOM.
 */

const ELEMENTS = new Set(
  (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr " +
    "Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu " +
    "Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr " +
    "Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og"
  ).split(" "),
);

/**
 * Element counts of a sum formula such as "C21H19N", "MgCH3Br" or
 * "C2H6O.2HCl" / "CuSO4·5H2O" (parts joined by "." or "·", each with an
 * optional leading integer multiplier). Null when the text cannot be read
 * completely: charges, isotopes, brackets, unknown symbols, other text.
 */
export function parseFormula(text: string | null | undefined): Map<string, number> | null {
  if (text === null || text === undefined) return null;
  const compact = text.replace(/\s+/g, "");
  if (compact === "") return null;
  const counts = new Map<string, number>();
  for (const part of compact.split(/[.·]/)) {
    const m = /^(\d*)(.*)$/.exec(part)!;
    const multiplier = m[1] === "" ? 1 : Number(m[1]);
    const body = m[2]!;
    if (body === "" || multiplier < 1) return null;
    const token = /([A-Z][a-z]?)(\d*)/y;
    let pos = 0;
    while (pos < body.length) {
      token.lastIndex = pos;
      const t = token.exec(body);
      if (!t || !ELEMENTS.has(t[1]!)) return null;
      const n = t[2] === "" ? 1 : Number(t[2]);
      if (n < 1) return null;
      counts.set(t[1]!, (counts.get(t[1]!) ?? 0) + n * multiplier);
      pos = token.lastIndex;
    }
  }
  return counts;
}

export type FormulaComparison = "same" | "different" | "cannot_compare";

/** Same composition / different / one of them cannot be read (never "different" then). */
export function compareFormulas(a: string | null | undefined, b: string | null | undefined): FormulaComparison {
  const x = parseFormula(a);
  const y = parseFormula(b);
  if (x === null || y === null) return "cannot_compare";
  if (x.size !== y.size) return "different";
  for (const [element, n] of x) if (y.get(element) !== n) return "different";
  return "same";
}

/**
 * Two yields in percent are the same for the import when they differ by no
 * more than the ELN's rounding: the page shows about three significant digits
 * (52.4), the list two decimals (52.38).
 */
export function sameYield(a: number, b: number): boolean {
  return Math.abs(a - b) <= Math.max(0.051, 0.005 * Math.max(a, b) + 0.001);
}

export type YieldComparison = "equal" | "different" | "only_first" | "only_second" | "none";

/** `first` / `second` are e.g. the list's and the registry's isolated yield. */
export function compareYields(first: number | null | undefined, second: number | null | undefined): YieldComparison {
  const a = first ?? null;
  const b = second ?? null;
  if (a === null && b === null) return "none";
  if (a === null) return "only_second";
  if (b === null) return "only_first";
  return sameYield(a, b) ? "equal" : "different";
}
