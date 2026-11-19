import { describe, expect, it } from "vitest";
import { createRuntimeHostClient, requestOpenCompounds } from "../src/content/host-client";
import { matchedByText } from "../src/content/labels";
import { checkName, NameEditor } from "../src/content/name-edit";
import {
  detailsLines,
  factsFromStructure,
  factsFromVariant,
  formatComposition,
  formatExactMass,
  formatMolarMass,
  formulaText,
} from "../src/content/properties";
import {
  isAnalyzeResult,
  isCompoundsResult,
  isFindResult,
  isResultOf,
  isSetNameResult,
  isStructureInfo,
} from "../src/content/validate";
import { describeQuery } from "../src/compounds/page";
import { listParams, NO_SEARCH, pageInfo, resultLine } from "../src/compounds/state";
import { MAX_NAME_TEXT, type BackgroundReply } from "../src/shared/protocol";
import {
  FakeClient,
  analyzeResult,
  compoundItem,
  compoundsResult,
  compoundVariant,
  coreFindResult,
  flush,
  foundVariant,
  nameInfo,
  properties,
  setNameResult,
  structureInfo,
} from "./helpers";

describe("validators: additive display-only fields", () => {
  it("accepts an analysis of an older host without properties and name", () => {
    const old = analyzeResult();
    delete (old.structure as { properties?: unknown }).properties;
    expect("name" in old).toBe(false);
    expect(isAnalyzeResult(old)).toBe(true);
  });

  it("accepts properties, null properties and a stored name", () => {
    expect(isStructureInfo(structureInfo({ properties: properties() }))).toBe(true);
    expect(isStructureInfo(structureInfo({ properties: null }))).toBe(true);
    expect(isAnalyzeResult(analyzeResult({ name: nameInfo("ethanol") }))).toBe(true);
    expect(isAnalyzeResult(analyzeResult({ name: null }))).toBe(true);
  });

  it.each([
    ["mol_weight as text", { ...properties(), mol_weight: "46" }],
    ["composition not a list", { ...properties(), composition: "C 52" }],
    ["composition item without percent", { ...properties(), composition: [{ element: "C" }] }],
    ["properties as text", "C2H6O"],
  ])("rejects malformed properties: %s", (_name, value) => {
    expect(isStructureInfo({ ...structureInfo(), properties: value })).toBe(false);
  });

  it("rejects a malformed name", () => {
    expect(isAnalyzeResult({ ...analyzeResult(), name: "ethanol" })).toBe(false);
    expect(isAnalyzeResult({ ...analyzeResult(), name: { text: 1, updated_at: "x" } })).toBe(false);
  });

  it("accepts find variants with and without name / inchi / properties", () => {
    const plain = foundVariant();
    delete plain.name;
    delete plain.inchi;
    delete plain.properties;
    expect(isFindResult(coreFindResult({ variants: [plain] }))).toBe(true);
    expect(isFindResult(coreFindResult({ variants: [foundVariant({ name: nameInfo("x") })] }))).toBe(true);
    expect(isFindResult(coreFindResult({ variants: [{ ...foundVariant(), inchi: 5 } as never] }))).toBe(false);
    expect(isFindResult(coreFindResult({ variants: [{ ...foundVariant(), properties: {} } as never] }))).toBe(false);
  });
});

describe("validators: compounds and set_name", () => {
  it("accepts a compounds answer", () => {
    expect(isCompoundsResult(compoundsResult())).toBe(true);
    expect(isCompoundsResult(compoundsResult({ items: [] }))).toBe(true);
    expect(isCompoundsResult(compoundsResult({ items: [compoundItem({ properties: null, svg: null, matched_by: ["id", "future_reason" as never] })] }))).toBe(true);
    expect(isResultOf("compounds", compoundsResult(), 1)).toBe(true);
  });

  it.each([
    ["items missing", { ...compoundsResult(), items: undefined }],
    ["total as text", { ...compoundsResult(), total: "1" }],
    ["notes missing", { ...compoundsResult(), notes: undefined }],
    ["item without core_id", compoundsResult({ items: [{ ...compoundItem(), core_id: "" }] })],
    ["variant without matched", compoundsResult({ items: [compoundItem({ variants: [{ ...compoundVariant(), matched: undefined } as never] })] })],
    ["variant name as text", compoundsResult({ items: [compoundItem({ variants: [{ ...compoundVariant(), name: "x" } as never] })] })],
    ["experiment without ref", compoundsResult({ items: [compoundItem({ experiments: [{ experiment_code: "A" } as never] })] })],
    ["matched_by not a list", compoundsResult({ items: [{ ...compoundItem(), matched_by: "id" } as never] })],
  ])("rejects a compounds answer with %s", (_name, value) => {
    expect(isCompoundsResult(value)).toBe(false);
  });

  it("accepts and rejects set_name answers", () => {
    expect(isSetNameResult(setNameResult())).toBe(true);
    expect(isSetNameResult(setNameResult({ name: null, changed: false }))).toBe(true);
    expect(isResultOf("set_name", setNameResult(), 1)).toBe(true);
    expect(isSetNameResult({ ...setNameResult(), changed: "yes" })).toBe(false);
    expect(isSetNameResult({ ...setNameResult(), name: "ethanol" })).toBe(false);
    expect(isSetNameResult({ ...setNameResult(), core_stereo_id: "" })).toBe(false);
  });

  it("the host client turns a malformed compounds answer into host_bad_response", async () => {
    const client = createRuntimeHostClient(async () => ({ ok: true, result: { items: "nope" } }));
    const reply = await client.request("compounds", {});
    expect(reply.ok).toBe(false);
    expect((reply as Extract<BackgroundReply, { ok: false }>).error.code).toBe("host_bad_response");
  });
});

describe("open-compounds request", () => {
  it("sends the message and reads ok", async () => {
    const sent: unknown[] = [];
    const reply = await requestOpenCompounds(async (m) => {
      sent.push(m);
      return { ok: true };
    });
    expect(sent).toEqual([{ type: "open-compounds" }]);
    expect(reply).toEqual({ ok: true });
  });

  it("passes a refusal on and explains a missing background", async () => {
    expect(await requestOpenCompounds(async () => ({ ok: false, error: { code: "not_allowed", message: "no" } }))).toEqual({
      ok: false,
      error: { code: "not_allowed", message: "no" },
    });
    const dead = await requestOpenCompounds(async () => {
      throw new Error("Could not establish connection");
    });
    expect(dead.ok).toBe(false);
    expect((dead as { error: { code: string } }).error.code).toBe("host_unavailable");
    const odd = await requestOpenCompounds(async () => "yes");
    expect((odd as { error: { code: string } }).error.code).toBe("host_bad_response");
  });
});

describe("formatting of the details", () => {
  it("formats masses and the elemental analysis", () => {
    expect(formatMolarMass(345.574)).toBe("345.57 g/mol");
    expect(formatMolarMass(46.069)).toBe("46.07 g/mol");
    expect(formatExactMass(345.29259)).toBe("345.2926");
    expect(
      formatComposition([
        { element: "C", percent: 77.38 },
        { element: "H", percent: 9.87 },
        { element: "N", percent: 3.61 },
      ]),
    ).toBe("C 77.38, H 9.87, N 3.61 %");
    expect(formatComposition([{ element: "C", percent: 100 }])).toBe("C 100.00 %");
    expect(formatComposition([])).toBeNull();
  });

  it("adds the per-fragment formula only when it differs", () => {
    expect(formulaText("C14H16ClN", "C14H15N.ClH")).toBe("C14H16ClN (per fragment: C14H15N.ClH)");
    expect(formulaText("C2H6O", "C2H6O")).toBe("C2H6O");
    expect(formulaText(null, "C2H6O")).toBe("C2H6O");
    expect(formulaText("C2H6O", null)).toBe("C2H6O");
    expect(formulaText(null, null)).toBeNull();
  });

  it("lists every fact of an analysis, in order", () => {
    const lines = detailsLines(factsFromStructure(structureInfo({ properties: properties() })));
    expect(lines.map((l) => [l.label, l.value])).toEqual([
      ["Formula", "C2H6O"],
      ["Molar mass", "46.07 g/mol"],
      ["Exact mass", "46.0419"],
      ["Elemental analysis", "C 52.14, H 13.13, O 34.73 %"],
      ["SMILES", "CCO"],
      ["InChI", "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"],
      ["InChIKey", "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"],
      ["Stereo", "Absolute (as drawn)"],
    ]);
    expect(lines.filter((l) => l.copy).map((l) => l.label)).toEqual(["SMILES", "InChI", "InChIKey"]);
  });

  it("shows less, without breaking, for an older host", () => {
    const s = structureInfo();
    delete (s as { properties?: unknown }).properties;
    const lines = detailsLines(factsFromStructure(s));
    expect(lines.map((l) => l.label)).toEqual(["Formula", "SMILES", "InChI", "InChIKey", "Stereo"]);
    const v = foundVariant();
    delete v.properties;
    delete v.inchi;
    expect(detailsLines(factsFromVariant(v, "C2H7NO")).map((l) => l.label)).toEqual(["Formula", "SMILES", "InChIKey", "Stereo"]);
  });

  it("names the detected mode when it was overridden", () => {
    const facts = factsFromStructure(structureInfo({ stereo_mode: "rac", detected_mode: "abs" }));
    expect(facts.stereo).toBe("Racemic (drawing: Absolute (as drawn))");
  });
});

describe("words", () => {
  it("says why a compound matched", () => {
    expect(matchedByText([])).toBeNull();
    expect(matchedByText(["id"])).toBe("Matched by ID");
    expect(matchedByText(["name", "drawn"])).toBe("Matched by name and drawn structure");
    expect(matchedByText(["inchikey", "formula", "experiment"])).toBe("Matched by InChIKey, formula and ELN experiment");
    expect(matchedByText(["structure", "something_new"])).toBe("Matched by structure (same constitution) and something_new");
  });

  it("describes the result and the search", () => {
    const twelve = Array.from({ length: 12 }, (_, i) => compoundItem({ core_id: String(i) }));
    expect(resultLine(compoundsResult({ total: 12, registry_total: 340, items: twelve }), true)).toBe(
      "12 of 340 compounds match the search.",
    );
    expect(resultLine(compoundsResult({ total: 30, registry_total: 30, offset: 48, items: [] }), false)).toBe("30 compounds.");
    expect(resultLine(compoundsResult({ total: 1, registry_total: 340 }), true)).toBe("1 of 340 compounds matches the search.");
    expect(resultLine(compoundsResult(), false)).toBe("1 compound.");
    const page2 = compoundsResult({ total: 30, registry_total: 30, offset: 24, items: Array.from({ length: 6 }, (_, i) => compoundItem({ core_id: String(i) })) });
    expect(resultLine(page2, false)).toBe("30 compounds. Showing 25–30.");
    expect(pageInfo(page2)).toEqual({ page: 2, pages: 2 });
    expect(resultLine(compoundsResult({ total: 0, registry_total: 0, items: [] }), false)).toBe("The registry has no compounds yet.");
    expect(resultLine(compoundsResult({ total: 0, registry_total: 5, items: [] }), true)).toBe("No compound matches this search.");
    expect(describeQuery(NO_SEARCH)).toBeNull();
    expect(describeQuery({ text: "CCO", molfile: "M", match: "exact" })).toBe(
      "Search: text “CCO” and a drawn structure (exact: same constitution)",
    );
  });

  it("builds the compounds parameters", () => {
    expect(listParams({ query: NO_SEARCH, order: "asc", offset: 0, limit: 24 })).toEqual({ order: "asc", offset: 0, limit: 24 });
    expect(listParams({ query: { text: "x", molfile: "M", match: "exact" }, order: "desc", offset: 24, limit: 24 })).toEqual({
      text: "x",
      molfile: "M",
      match: "exact",
      order: "desc",
      offset: 24,
      limit: 24,
    });
  });
});

describe("name check and name editor", () => {
  it.each([
    ["", false],
    ["   ", false],
    ["a\tb", false],
    ["a\nb", false],
    ["x\u007f", false],
    ["x".repeat(MAX_NAME_TEXT + 1), false],
    ["x".repeat(MAX_NAME_TEXT), true],
    ["  (2S)-2-aminopropan-1-ol  ", true],
  ])("checkName(%j) ok=%s", (text, ok) => {
    const result = checkName(text);
    expect(result.ok).toBe(ok);
    if (result.ok) expect(result.name).toBe(text.trim());
  });

  function editor() {
    const client = new FakeClient();
    let changes = 0;
    const announced: string[] = [];
    const names = new NameEditor(client, { onChange: () => (changes += 1), announce: (m) => announced.push(m) });
    return { client, names, announced, changes: () => changes };
  }

  it("saves the trimmed draft for the edited CORE_STEREO only", async () => {
    const { client, names, announced } = editor();
    names.begin("000042_01", "old");
    expect(names.state.draft).toBe("old");
    expect(names.takeFocusKey()).toBe("name-input:000042_01");
    names.setDraft("  new name ");
    expect(await names.save("000042_02")).toBeNull(); // not the row being edited
    const done = names.save("000042_01");
    expect(client.last("set_name").params).toEqual({ core_stereo_id: "000042_01", name: "new name" });
    void names.save("000042_01"); // second click while pending: nothing
    expect(client.callsOf("set_name")).toHaveLength(1);
    client.last("set_name").resolve({ ok: true, result: setNameResult({ name: nameInfo("new name") }) });
    expect((await done)!.name!.text).toBe("new name");
    expect(names.state.editing).toBeNull();
    expect(names.takeFocusKey()).toBe("name-edit:000042_01");
    expect(announced).toContain("Name of 000042_01 saved.");
  });

  it("sends nothing for an invalid draft and says why", async () => {
    const { client, names } = editor();
    names.begin("000042_01", null);
    names.setDraft("   ");
    expect(await names.save("000042_01")).toBeNull();
    expect(client.calls).toHaveLength(0);
    expect(names.state.problem?.message).toBe("Enter a name first.");
  });

  it("removes with null and keeps the editor open on an error", async () => {
    const { client, names } = editor();
    names.begin("000042_01", "x");
    const p = names.remove("000042_01");
    expect(client.last("set_name").params).toEqual({ core_stereo_id: "000042_01", name: null });
    client.last("set_name").resolve({ ok: false, error: { code: "not_found", message: "No such stereo variant." } });
    expect(await p).toBeNull();
    await flush();
    expect(names.state.editing).toBe("000042_01");
    expect(names.state.problem?.error?.code).toBe("not_found");
    names.cancel();
    expect(names.state.editing).toBeNull();
  });
});
