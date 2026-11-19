// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { extractExperiment, readExperimentCode, readRowYields, rowHasStructure } from "../src/content/extract";
import { parseYieldText } from "../src/content/yields";
import { showExperiment, testIsVisible, type FixtureRow } from "./page-fixture";

const read = () => extractExperiment(document, testIsVisible);
/** View mode, both yield fields empty. */
const NONE = { isolated: null, analytical: null };

afterEach(() => {
  document.body.replaceChildren();
});

describe("extractExperiment", () => {
  it("reads key, code and two product rows in page order", () => {
    showExperiment(document);
    expect(read()).toEqual({
      kind: "experiment",
      key: "4180",
      code: "ABC-008",
      rows: [
        { index: 1, handle: "cNfVkc3", smiles: "CCO", yields: NONE },
        { index: 2, handle: "cNfVkd7", smiles: "CC(=O)O", yields: NONE },
      ],
    });
  });

  it("ignores the hidden template link", () => {
    showExperiment(document, { templateKey: "999" });
    const ex = read();
    expect(ex.kind === "experiment" && ex.key).toBe("4180");
  });

  it("uses the default visibility check (layout boxes) when none is given", () => {
    showExperiment(document);
    // jsdom has no layout: nothing is visible, so no experiment is found.
    expect(extractExperiment(document).kind).toBe("none");
  });

  it("reads one product", () => {
    showExperiment(document, { rows: [{ id: "x1", smiles: "CCO" }] });
    const ex = read();
    expect(ex.kind === "experiment" && ex.rows).toEqual([{ index: 1, handle: "x1", smiles: "CCO", yields: NONE }]);
  });

  it("reads zero rows", () => {
    showExperiment(document, { rows: [] });
    const ex = read();
    expect(ex.kind === "experiment" && ex.rows).toEqual([]);
  });

  it("reports rows without a structure", () => {
    showExperiment(document, { rows: [{ id: "a", smiles: "" }, {}, { id: "b", smiles: "C" }] });
    const ex = read();
    if (ex.kind !== "experiment") throw new Error(ex.kind);
    expect(ex.rows.map(rowHasStructure)).toEqual([false, false, true]);
    expect(ex.rows[1]).toEqual({ index: 2, handle: null, smiles: null, yields: NONE });
  });

  it("refuses a key mismatch between the two links", () => {
    showExperiment(document, { incidentKey: "4181" });
    expect(read()).toMatchObject({ kind: "error" });
  });

  it("accepts a page without the incident link", () => {
    showExperiment(document, { incidentKey: null });
    expect(read().kind).toBe("experiment");
  });

  it("finds nothing without an anchor", () => {
    document.body.textContent = "Login";
    expect(read()).toEqual({ kind: "none" });
  });

  it.each(["21a825", "", "-1", "1e5"])("refuses the non-digit key %j", (key) => {
    showExperiment(document, { key, incidentKey: null });
    expect(read()).toMatchObject({ kind: "error" });
  });

  it("reports a grid without rows container", () => {
    showExperiment(document, { noRowsContainer: true });
    expect(read()).toMatchObject({ kind: "error", what: expect.stringMatching(/rows container/) });
  });

  it("reads the code from the h2 title, else from its visible span", () => {
    showExperiment(document, { code: "ABC-010" });
    expect(readExperimentCode(document, testIsVisible)).toBe("ABC-010");
    const h2 = document.querySelector("h2")!;
    h2.removeAttribute("title");
    expect(readExperimentCode(document, testIsVisible)).toBe("ABC-010");
    h2.querySelector("span")!.textContent = "  ";
    expect(readExperimentCode(document, testIsVisible)).toBeNull();
  });

  it("has no code when there is no h2 text", () => {
    showExperiment(document, { code: null });
    const ex = read();
    expect(ex.kind === "experiment" && ex.code).toBeNull();
  });
});

describe("parseYieldText", () => {
  it.each([
    ["", null],
    ["   ", null],
    ["76.2", 76.2],
    [" 76.2 ", 76.2],
    ["76,2", 76.2],
    ["90", 90],
    ["0", 0],
    ["73.65", 73.65],
    ["76.2%", 76.2],
    ["76.2 %", 76.2],
    ["1000", 1000],
    ["105.2", 105.2],
  ])("reads %j as %j", (text, value) => {
    expect(parseYieldText(text)).toBe(value);
  });

  it.each(["abc", "76.2.3", "-3", "+3", "1001", "1e2", ".5", "5.", "76,2,1", "76.2 %%", "% 81", "81 mg", "1 000", "NaN", "Infinity", "0x10"])(
    "refuses %j (never guesses)",
    (text) => {
      expect(parseYieldText(text)).toBeUndefined();
    },
  );
});

describe("yields of a product row", () => {
  function rowYields(row: FixtureRow) {
    showExperiment(document, { rows: [{ id: "a", smiles: "C", ...row }] });
    const ex = read();
    if (ex.kind !== "experiment") throw new Error(ex.kind);
    return ex.rows[0]!.yields;
  }

  it("reads empty fields in view mode as no yields", () => {
    expect(rowYields({ yields: {} })).toEqual({ isolated: null, analytical: null });
  });

  it("reads filled fields from the input's value, never its title", () => {
    expect(rowYields({ yields: { isolated: "73.7", analytical: "78", title: "73.651758265378" } })).toEqual({
      isolated: 73.7,
      analytical: 78,
    });
  });

  it("reads only the isolated yield (cell 10) and only the analytical one (cell 9)", () => {
    expect(rowYields({ yields: { isolated: "76,2" } })).toEqual({ isolated: 76.2, analytical: null });
    expect(rowYields({ yields: { analytical: "12.5 %" } })).toEqual({ isolated: null, analytical: 12.5 });
  });

  it("uses the value property (what the page shows now), not the attribute", () => {
    showExperiment(document, { rows: [{ id: "a", smiles: "C", yields: { isolated: "10" } }] });
    const input = document.querySelector<HTMLInputElement>('input[placeholder="isolated"]')!;
    input.value = "55.5";
    expect(readRowYields(document.querySelector("tr.z-row")!)).toEqual({ isolated: 55.5, analytical: null });
  });

  it("does not use the fields while the experiment is edited (inputs not read-only)", () => {
    expect(rowYields({ yields: { isolated: "73.7", mode: "edit" } })).toBeNull();
  });

  it("does not use the fields when only one of them is read-only", () => {
    showExperiment(document, { rows: [{ id: "a", smiles: "C", yields: { isolated: "73.7" } }] });
    document.querySelector('input[placeholder="analytical"]')!.removeAttribute("readonly");
    expect(readRowYields(document.querySelector("tr.z-row")!)).toBeNull();
  });

  it("refuses the whole row when one value is not a plain number", () => {
    expect(rowYields({ yields: { isolated: "73.7", analytical: "about 80" } })).toBeNull();
    expect(rowYields({ yields: { isolated: "2000" } })).toBeNull();
  });

  it("checks the shape: yield cells without inputs, too few cells", () => {
    expect(rowYields({ yields: "bare" })).toBeNull();
    expect(rowYields({ cells: 11 })).toBeNull();
    expect(rowYields({ cells: 12 })).toEqual({ isolated: null, analytical: null });
  });

  it("checks the shape: an extra input in a yield cell or a missing one in the comment cell", () => {
    showExperiment(document, { rows: [{ id: "a", smiles: "C", yields: { isolated: "5" } }] });
    const row = document.querySelector("tr.z-row")!;
    const cells = Array.from(row.children);
    const extra = document.createElement("input");
    extra.readOnly = true;
    cells[10]!.append(extra);
    expect(readRowYields(row)).toBeNull();
    extra.remove();
    expect(readRowYields(row)).toEqual({ isolated: 5, analytical: null });
    cells[11]!.querySelector("input")!.remove();
    expect(readRowYields(row)).toBeNull();
  });

  it("counts only the row's direct cells (the nested tables have cells of their own)", () => {
    showExperiment(document, { rows: [{ id: "a", smiles: "C", yields: { isolated: "42" } }] });
    const row = document.querySelector("tr.z-row")!;
    expect(row.querySelectorAll("td").length).toBeGreaterThan(15);
    expect(readRowYields(row)).toEqual({ isolated: 42, analytical: null });
    // With one cell less the positions shift: the comment cell becomes cell 10 and the shape check fails.
    row.children[0]!.remove();
    expect(readRowYields(row)).toBeNull();
  });

  it("reads each row separately", () => {
    showExperiment(document, {
      rows: [
        { id: "a", smiles: "C", yields: { isolated: "76.2" } },
        { id: "b", smiles: "N", yields: { isolated: "1", mode: "edit" } },
        { id: "c", smiles: "O", yields: { analytical: "90" } },
      ],
    });
    const ex = read();
    expect(ex.kind === "experiment" && ex.rows.map((r) => r.yields)).toEqual([
      { isolated: 76.2, analytical: null },
      null,
      { isolated: null, analytical: 90 },
    ]);
  });
});
