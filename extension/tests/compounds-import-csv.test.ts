import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  decodeCsvBytes,
  decodeEntities,
  detectSeparator,
  NO_PK_COLUMN,
  parseElnCsv,
  parseYieldCell,
  productFormulas,
  readCsv,
  yieldPositions,
} from "../src/compounds/eln-csv";
import { compareFormulas, compareYields, parseFormula, sameYield } from "../src/compounds/import-compare";

const fixture = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "fixtures", "eln-experiments.csv"));
const utf8 = (text: string) => new TextEncoder().encode(text);

/** The yield columns of the export: "Yisol (%)" and "Yana (%)", each 7 wide. */
const YIELD_HEADER = "Yisol (%),,,,,,,Yana (%),,,,,,";

describe("the ELN's experiment list: the fixture", () => {
  const parsed = parseElnCsv(fixture);

  it("reads 9 experiments in file order, no problem rows, no error", () => {
    expect(parsed.error).toBeNull();
    expect(parsed.problems).toEqual([]);
    expect(parsed.experiments.map((e) => e.ref)).toEqual([
      "4110", "4120", "4130", "4131", "4150", "4160", "4170", "4180", "4190",
    ]);
    expect(parsed.experiments.map((e) => e.code)).toEqual([
      "ABC-001", "ABC-002", "ABC-003", "ABC-004", "ABC-005", "ABC-006", "ABC-007", "ABC-008", "ABC-009",
    ]);
    expect(parsed.experiments.reduce((n, e) => n + e.products.length, 0)).toBe(11);
  });

  it("has the products, formulas and isolated yields of each experiment; no analytical yields", () => {
    const view = parsed.experiments.map((e) => [e.code, e.products.map((p) => [p.index, p.formula, p.isolated])]);
    expect(view).toEqual([
      ["ABC-001", [[1, "C13H10O", 52.38]]],
      ["ABC-002", [[1, "C21H19N", 17.25], [2, "C13H10O", 58.62]]],
      ["ABC-003", [[1, "C22H23N", 37.84]]],
      ["ABC-004", [[1, "C21H19N", 64.55]]],
      ["ABC-005", [[1, "C22H23N", 0]]],
      ["ABC-006", [[1, "C22H23N", 76.18], [2, "C22H23N", 76.18]]],
      ["ABC-007", [[1, "C22H23N", null]]],
      ["ABC-008", [[1, "C14H15N", 69.97]]],
      ["ABC-009", [[1, "C22H23N", 55.12]]],
    ]);
    for (const e of parsed.experiments) for (const p of e.products) expect(p.analytical).toBeNull();
  });

  it("keeps status, type, the product amounts and the line where each row starts", () => {
    const [first, second, third] = parsed.experiments;
    expect(first!.status).toBe("started");
    expect(first!.type).toBe("Chemical reaction");
    expect(first!.title).toBeNull();
    expect(first!.products[0]!.amount).toBe("12.5 mmol");
    expect(second!.products.map((p) => p.amount)).toEqual(["8.23 mmol", "8.23 mmol"]);
    expect(parsed.experiments[6]!.status).toBe("planned");
    // ABC-002's Procedure holds a quoted line break: ABC-003 starts one line later.
    expect([first!.line, second!.line, third!.line]).toEqual([2, 3, 5]);
  });
});

describe("the ELN's experiment list: tolerance", () => {
  it("accepts ';' as separator (a file re-saved by Excel), with decimal commas", () => {
    const text = [
      "pk;Lab notebook entry;Reaction equation (molecular formulas);Yisol (%);;;;Product 1;Product 2",
      '4110;ABC-001;"BrC7H5O (s) + 1.1 BC6H7O2 (s) ---&gt; C13H10O";52,38%;;;1;"12,5 mmol";',
      // Excel quotes a cell that holds the separator (here the ";" of "&gt;").
      '4120;ABC-002;"C13H10O + C8H11N (l) ---&gt; C21H19N + C13H10O";17.25 %;58,62%;1 : 3,40;;8.23 mmol;8.23 mmol',
    ].join("\r\n");
    const parsed = parseElnCsv(utf8(text));
    expect(parsed.error).toBeNull();
    expect(parsed.experiments.map((e) => e.products.map((p) => [p.formula, p.isolated, p.amount]))).toEqual([
      [["C13H10O", 52.38, "12,5 mmol"]],
      [["C21H19N", 17.25, "8.23 mmol"], ["C13H10O", 58.62, "8.23 mmol"]],
    ]);
  });

  it("accepts a byte order mark and LF line ends", () => {
    const text = "\uFEFFpk,Lab notebook entry,Title\n4110,ABC-001,First\n\n4120,ABC-002,Second\n";
    const parsed = parseElnCsv(utf8(text));
    expect(parsed.error).toBeNull();
    expect(parsed.experiments.map((e) => [e.ref, e.code, e.title, e.line])).toEqual([
      ["4110", "ABC-001", "First", 2],
      ["4120", "ABC-002", "Second", 4],
    ]);
  });

  it("reads bytes that are not UTF-8 as windows-1252", () => {
    const bytes = Buffer.concat([
      Buffer.from("pk,Lab notebook entry,Title\r\n4110,ABC-001,T", "latin1"),
      Buffer.from([0xe9, 0x73, 0x74, 0x20, 0x80, 0x20, 0x96, 0x20, 0x9c]),
      Buffer.from("\r\n", "latin1"),
    ]);
    expect(parseElnCsv(bytes).experiments[0]!.title).toBe("Tést € – œ");
    expect(decodeCsvBytes(new Uint8Array([0xef, 0xbb, 0xbf, 0x41]))).toBe("A");
    expect(decodeCsvBytes(utf8("Tést").buffer as ArrayBuffer)).toBe("Tést");
  });

  it("reads quoted cells with separators, doubled quotes and line breaks; decodes entities", () => {
    const text =
      'pk,Lab notebook entry,Title,Reaction equation (molecular formulas)\r\n' +
      '4110,ABC-001,"A, &quot;b&quot; &amp; ""c""\r\nsecond &#39;line&#39; &#x3E; &#62;",C2H6O ---&gt; C2H4O\r\n';
    const parsed = parseElnCsv(utf8(text));
    expect(parsed.experiments[0]!.title).toBe('A, "b" & "c"\nsecond \'line\' > >');
    expect(parsed.experiments[0]!.products[0]!.formula).toBe("C2H4O");
  });

  it("copes with rows shorter or longer than the header and skips empty rows", () => {
    const text = [
      "pk,Lab notebook entry,Reaction equation (molecular formulas),Yisol (%),,,",
      "4110,ABC-001",
      ",,,,,,",
      "4120,ABC-002,A ---&gt; C2H6O,12%,,,extra,cells,9%",
    ].join("\r\n");
    const parsed = parseElnCsv(utf8(text));
    expect(parsed.problems).toEqual([]);
    expect(parsed.experiments.map((e) => [e.ref, e.products.map((p) => [p.formula, p.isolated])])).toEqual([
      ["4110", []],
      ["4120", [["C2H6O", 12]]],
    ]);
  });

  it("refuses a file without a pk column, and an empty file, with a plain message", () => {
    const parsed = parseElnCsv(utf8("Lab notebook entry,Title\r\nABC-001,x\r\n"));
    expect(parsed.error).toBe(NO_PK_COLUMN);
    expect(NO_PK_COLUMN).toBe('This does not look like an experiment list exported from the ELN: there is no "pk" column.');
    expect(parsed.experiments).toEqual([]);
    expect(parseElnCsv(new Uint8Array()).error).toBe("The file is empty.");
  });

  it("reports rows with an empty or non-numeric pk, and a second row with the same pk, and imports neither", () => {
    const text = [
      "pk,Lab notebook entry",
      "4110,ABC-001",
      ",ABC-002",
      "21x5,ABC-003",
      "4110,ABC-004",
      "4120,ABC-005",
    ].join("\r\n");
    const parsed = parseElnCsv(utf8(text));
    expect(parsed.experiments.map((e) => e.code)).toEqual(["ABC-001", "ABC-005"]);
    expect(parsed.problems.map((p) => p.line)).toEqual([3, 4, 5]);
    expect(parsed.problems[0]!.reason).toMatch(/pk cell is empty/);
    expect(parsed.problems[1]!.reason).toMatch(/"21x5" is not a number/);
    expect(parsed.problems[2]!.reason).toMatch(/already listed on line 2/);
  });
});

describe("the ELN's experiment list: yields and products", () => {
  it("reads yields only in the first (width - 2) columns; ratio and 'de' columns never count", () => {
    const text = [
      `pk,${YIELD_HEADER},Product 1`,
      "1,52.38%,n/a,,x %,,12%,(0 % de),,,,,,,,",
      "2,,,,,,1 : 3.40,(5 % de),3%,,,,,,7%,",
      "3,0%,,,,,,,,,,,,,,",
    ].join("\r\n");
    const parsed = parseElnCsv(utf8(text));
    expect(parsed.experiments.map((e) => e.products.map((p) => [p.index, p.isolated, p.analytical]))).toEqual([
      [[1, 52.38, null]],
      [[1, null, 3]],
      [[1, 0, null]],
    ]);
    expect(yieldPositions(7)).toBe(5);
    expect(yieldPositions(1)).toBe(1);
  });

  it("counts products from the formulas, the Product N columns and the yields, whichever is largest", () => {
    const text = [
      `pk,Reaction equation (molecular formulas),${YIELD_HEADER},Product 1,Product 2,Product 3`,
      "1,A ---&gt; C2H6O,,,,,,,,,,,,,,,6 mmol,,2 mmol",
      "2,A ---&gt; C2H6O,,,,51%,,,,,,,,,,,,,",
      "3,A ---&gt; C2H6O + 2 C3H8O (l) + CH4O (aq),,,,,,,,,,,,,,,,,",
    ].join("\r\n");
    const parsed = parseElnCsv(utf8(text));
    expect(parsed.experiments.map((e) => e.products.length)).toEqual([3, 4, 3]);
    expect(parsed.experiments[0]!.products.map((p) => [p.formula, p.amount])).toEqual([
      ["C2H6O", "6 mmol"],
      [null, null],
      [null, "2 mmol"],
    ]);
    expect(parsed.experiments[1]!.products[3]!.isolated).toBe(51);
    expect(parsed.experiments[2]!.products.map((p) => p.formula)).toEqual(["C2H6O", "C3H8O", "CH4O"]);
  });

  it("splits the reaction equation at the arrow, drops coefficients and states", () => {
    expect(productFormulas("2.1 LiCH3 (l) + C21H19N ---&gt; C22H23N + C22H23N")).toEqual(["C22H23N", "C22H23N"]);
    expect(productFormulas("4 MgCH3Br + C21H19N ---> 1.1 C22H23N (s) + 2 H2O (g)")).toEqual(["C22H23N", "H2O"]);
    expect(productFormulas("C2H6O + O2")).toEqual([]);
    expect(productFormulas(null)).toEqual([]);
    expect(productFormulas("C2H6O ---> ")).toEqual([]);
  });

  it("reads a yield cell only as number + %", () => {
    expect(parseYieldCell("52.38%")).toBe(52.38);
    expect(parseYieldCell(" 76,2 % ")).toBe(76.2);
    expect(parseYieldCell("0%")).toBe(0);
    for (const text of ["", "52.38", "1 : 3.40", "(0 % de)", "x%", "-5%", null]) expect(parseYieldCell(text)).toBeNull();
  });
});

describe("CSV basics", () => {
  it("decides the separator from the header line, outside quotes", () => {
    expect(detectSeparator("a,b;c,d\r\n;;;;;")).toBe(",");
    expect(detectSeparator("a;b;c,d\r\n")).toBe(";");
    expect(detectSeparator('"a;b;c";d,e,f')).toBe(",");
  });

  it("reads quoted cells across CR, LF and CRLF, with line numbers of the row starts", () => {
    expect(readCsv('a,"b\r\nc",d\re,f\n"g""h",', ",")).toEqual([
      { line: 1, cells: ["a", "b\nc", "d"] },
      { line: 3, cells: ["e", "f"] },
      { line: 4, cells: ['g"h', ""] },
    ]);
    expect(readCsv("a,b\r\n", ",")).toEqual([{ line: 1, cells: ["a", "b"] }]);
    expect(readCsv('x"y,z', ",")).toEqual([{ line: 1, cells: ['x"y', "z"] }]);
  });

  it("decodes named and numeric entities and leaves unknown ones", () => {
    expect(decodeEntities("a &lt;b&gt; &amp;amp; &#39;&#x27; &unknown; &#0;")).toBe("a <b> &amp; '' &unknown; &#0;");
  });
});

describe("formula comparison by composition", () => {
  it("reads formulas in any element order", () => {
    expect(Object.fromEntries(parseFormula("BrC7H5O")!)).toEqual({ Br: 1, C: 7, H: 5, O: 1 });
    expect(Object.fromEntries(parseFormula("MgCH3Br")!)).toEqual({ Mg: 1, C: 1, H: 3, Br: 1 });
    expect(Object.fromEntries(parseFormula("C2H6O.2HCl")!)).toEqual({ C: 2, H: 8, O: 1, Cl: 2 });
    expect(Object.fromEntries(parseFormula("CuSO4·5H2O")!)).toEqual({ Cu: 1, S: 1, O: 9, H: 10 });
  });

  it("cannot read charges, isotopes, brackets or other text", () => {
    for (const text of ["C2H3O2-", "C2H3O2+", "[13C]H4", "D2O", "Ca(OH)2", "C2H6O ethanol", "", "c2h6o", "C0H4", null]) {
      expect(parseFormula(text), String(text)).toBeNull();
    }
  });

  it("says same, different or cannot be compared (never different for what it cannot read)", () => {
    expect(compareFormulas("BrC7H5O", "C7H5BrO")).toBe("same");
    expect(compareFormulas("C22H23N", "C22H23N")).toBe("same");
    expect(compareFormulas("C13H10O", "C13H12O")).toBe("different");
    expect(compareFormulas("C2H6O", "C2H6OS")).toBe("different");
    expect(compareFormulas("C2H3O2-", "C2H3O2")).toBe("cannot_compare");
    expect(compareFormulas(null, "C2H6O")).toBe("cannot_compare");
  });
});

describe("yield comparison within the ELN's rounding", () => {
  it.each([
    [52.4, 52.38],
    [64.5, 64.55],
    [70, 69.97],
    [76.2, 76.18],
    [0, 0.05],
    [99.5, 100],
  ])("%d and %d are equal", (a, b) => {
    expect(sameYield(a, b)).toBe(true);
    expect(sameYield(b, a)).toBe(true);
  });

  it.each([
    [52.4, 52.7],
    [0, 0.1],
    [10, 10.2],
  ])("%d and %d differ", (a, b) => {
    expect(sameYield(a, b)).toBe(false);
  });

  it("names a missing side", () => {
    expect(compareYields(52.38, 52.4)).toBe("equal");
    expect(compareYields(52.38, 44)).toBe("different");
    expect(compareYields(52.38, null)).toBe("only_first");
    expect(compareYields(null, 52.4)).toBe("only_second");
    expect(compareYields(null, undefined)).toBe("none");
  });
});
