import { describe, expect, it, vi } from "vitest";
import { EXPERIMENT_KEY_NEEDED, EXPERIMENT_KEY_NOT_A_NUMBER, ManualSource, parseProductIndex } from "../src/content/manual-source";
import { detectStructure } from "../src/content/structure-kind";
import { MAX_PRODUCT_INDEX, MAX_REF_TEXT, MAX_STRUCTURE_CHARS } from "../src/shared/protocol";
import type { SourceState } from "../src/shared/adapter";
import { MOLFILE } from "./helpers";

type Ready = Extract<SourceState, { kind: "ready" }>;
function ready(source: ManualSource): Ready {
  const state = source.getState();
  if (state.kind !== "ready") throw new Error(`not ready: ${JSON.stringify(state)}`);
  return state;
}

function file(name: string, text: string, size = text.length) {
  return { name, size, text: async () => text };
}

describe("structure kind", () => {
  it("treats text with a line break as a molfile, verbatim", () => {
    const text = "\n  header line with trailing spaces   \n\n  0  0  0     0  0            999 V2000\nM  END\n\n";
    expect(detectStructure(text)).toMatchObject({ kind: "molfile", structure: { molfile: text } });
  });
  it("keeps CRLF and leading/trailing whitespace of a molfile", () => {
    const text = "  \r\nname\r\n\r\nM  END  ";
    expect(detectStructure(text)).toMatchObject({ kind: "molfile", structure: { molfile: text } });
  });
  it("treats single-line text containing 'M  END' as a molfile", () => {
    expect(detectStructure("xx M  END")).toMatchObject({ kind: "molfile" });
  });
  it("trims a SMILES", () => {
    expect(detectStructure("  \tC[C@H](N)O  ")).toEqual({ kind: "smiles", structure: { smiles: "C[C@H](N)O" } });
  });
  it("reports empty text", () => {
    expect(detectStructure("   \t ")).toEqual({ kind: "empty" });
    expect(detectStructure("\n\n")).toEqual({ kind: "empty" });
  });
  it("enforces the size limit", () => {
    expect(detectStructure("C".repeat(MAX_STRUCTURE_CHARS))).toMatchObject({ kind: "smiles" });
    expect(detectStructure("C".repeat(MAX_STRUCTURE_CHARS + 1))).toMatchObject({ kind: "too_large" });
    expect(detectStructure(`C\n${"C".repeat(MAX_STRUCTURE_CHARS)}`)).toMatchObject({ kind: "too_large" });
  });
  it("reads a SMILES copied with its line end as a SMILES", () => {
    expect(detectStructure("CCO\n")).toEqual({ kind: "smiles", structure: { smiles: "CCO" } });
    expect(detectStructure("\r\n  CCO \r\n")).toEqual({ kind: "smiles", structure: { smiles: "CCO" } });
    expect(detectStructure("CCO\nCCN")).toMatchObject({ kind: "molfile" });
  });
});

describe("product no.", () => {
  it.each([
    ["1", 1],
    [" 12 ", 12],
    [String(MAX_PRODUCT_INDEX), MAX_PRODUCT_INDEX],
    ["0", null],
    [String(MAX_PRODUCT_INDEX + 1), null],
    ["1.5", null],
    ["-1", null],
    ["1e2", null],
    ["", null],
    ["abc", null],
  ])("parses %j as %j", (text, expected) => {
    expect(parseProductIndex(text)).toBe(expected);
  });
});

describe("ManualSource", () => {
  it("starts idle", () => {
    const source = new ManualSource();
    expect(source.getState().kind).toBe("idle");
    expect(source.name).toBe("Manual input");
  });

  it("is ready without experiment when no key is given, with a notice", () => {
    const source = new ManualSource();
    source.setStructureText(MOLFILE);
    const state = ready(source);
    expect(state.experiment).toBeNull();
    expect(state.notice).toBe(EXPERIMENT_KEY_NEEDED);
    expect(state.products).toHaveLength(1);
    expect(state.products[0]!.structure).toEqual({ molfile: MOLFILE });
    expect(state.products[0]!.productIndex).toBe(1);
  });

  it("sends a molfile verbatim and a SMILES trimmed", () => {
    const source = new ManualSource();
    source.setStructureText(MOLFILE);
    expect(ready(source).products[0]!.structure.molfile).toBe(MOLFILE);
    source.setStructureText("  CCO \n".replace("\n", ""));
    expect(ready(source).products[0]!.structure).toEqual({ smiles: "CCO" });
  });

  it("builds the experiment from trimmed fields; url stays null", () => {
    const source = new ManualSource();
    source.setStructureText("CCO");
    source.setExperimentKey("  4180 ");
    source.setExperimentCode("  ");
    expect(ready(source).experiment).toEqual({ ref: "4180", code: null, url: null });
    expect(ready(source).notice).toBeUndefined();
    source.setExperimentCode(" ABC-008 ");
    expect(ready(source).experiment).toEqual({ ref: "4180", code: "ABC-008", url: null });
  });

  it("takes only the ELN's internal number as experiment key, never the ELN number", () => {
    const source = new ManualSource();
    source.setStructureText("CCO");
    for (const key of ["ABC-008", "4180a", "21 7825", "-5", "1.5", "1".repeat(19), "k".repeat(MAX_REF_TEXT + 1)]) {
      source.setExperimentKey(key);
      // No experiment: lookups still work, Create ID does not.
      expect(ready(source).experiment, key).toBeNull();
      expect(ready(source).notice, key).toBe(EXPERIMENT_KEY_NOT_A_NUMBER);
      expect(ready(source).products, key).toHaveLength(1);
    }
    expect(EXPERIMENT_KEY_NOT_A_NUMBER).toContain("not the ELN number");
    source.setExperimentKey("1".repeat(18));
    expect(ready(source).experiment?.ref).toBe("1".repeat(18));
    expect(ready(source).notice).toBeUndefined();
  });

  it("refuses an over-long experiment code", () => {
    const source = new ManualSource();
    source.setStructureText("CCO");
    source.setExperimentKey("4180");
    source.setExperimentCode("c".repeat(MAX_REF_TEXT));
    expect(ready(source).experiment?.code).toHaveLength(MAX_REF_TEXT);
    source.setExperimentCode("c".repeat(MAX_REF_TEXT + 1));
    expect(ready(source).experiment).toBeNull();
    expect(ready(source).notice).toContain("experiment code is longer");
  });

  it("reports an invalid product no. and a too large structure as errors", () => {
    const source = new ManualSource();
    source.setStructureText("CCO");
    source.setProductIndex("0");
    expect(source.getState().kind).toBe("error");
    source.setProductIndex("7");
    expect(ready(source).products[0]!.productIndex).toBe(7);
    source.setStructureText("C".repeat(MAX_STRUCTURE_CHARS + 1));
    expect(source.getState().kind).toBe("error");
  });

  it("changes the token on structure changes only", () => {
    const source = new ManualSource();
    source.setStructureText("CCO");
    const t1 = ready(source).products[0]!.changeToken;
    source.setExperimentKey("1");
    source.setProductIndex("2");
    expect(ready(source).products[0]!.changeToken).toBe(t1);
    source.setStructureText("CCN");
    expect(ready(source).products[0]!.changeToken).not.toBe(t1);
  });

  it("notifies subscribers and stops after unsubscribe", () => {
    const source = new ManualSource();
    const listener = vi.fn();
    const off = source.subscribe(listener);
    source.setStructureText("CCO");
    source.setStructureText("CCO"); // unchanged: no notification
    expect(listener).toHaveBeenCalledTimes(1);
    off();
    source.setStructureText("CCN");
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("loads one .mol file verbatim and labels the card with its name", async () => {
    const source = new ManualSource();
    const text = "\r\n  ChemDraw\r\n\r\n  3  2  0  0  0  0  0  0  0  0999 V2000\r\nM  END\r\n";
    expect(await source.loadFile(file("Product.MOL", text))).toEqual({ ok: true });
    const p = ready(source).products[0]!;
    expect(p.structure).toEqual({ molfile: text });
    expect(p.label).toBe("Product.MOL");
    expect(source.structureText).toBe(text);
  });

  it("refuses other files, oversized files and empty files", async () => {
    const source = new ManualSource();
    expect(await source.loadFile(file("a.sdf", "x\nM  END"))).toMatchObject({ ok: false });
    expect(await source.loadFile(file("a.mol", "x", MAX_STRUCTURE_CHARS + 1))).toMatchObject({ ok: false });
    expect(await source.loadFile(file("a.mol", "x".repeat(MAX_STRUCTURE_CHARS + 1), 10))).toMatchObject({ ok: false });
    expect(await source.loadFile(file("a.mol", "  \n "))).toMatchObject({ ok: false });
    expect(source.getState().kind).toBe("idle");
  });
});
