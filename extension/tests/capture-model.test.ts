import { describe, expect, it } from "vitest";
import {
  addMolecule,
  addStep,
  applyEdit,
  chemdrawRequest,
  conditionLines,
  copyAllText,
  findMolecule,
  modelFromResult,
  moleculesOf,
  place,
  placementsOf,
  reactionSmiles,
  refreshDuplicates,
  removeMolecule,
  removeStep,
  roleIn,
  unusedMolecules,
} from "../src/capture/model";
import {
  MAX_CHEMDRAW_CONDITION_LINES,
  MAX_CHEMDRAW_STEP_MOLECULES,
  MAX_CHEMDRAW_STEPS,
  MAX_REACTION_TEXT,
} from "../src/shared/protocol";
import { MOLFILE_B, molecule, recognizeResult } from "./capture-fixtures";

const model = () => modelFromResult(recognizeResult());
const numbers = (list: { n: number }[]) => list.map((m) => m.n);

describe("the model from a recognition", () => {
  it("keeps every molecule, also the one in no reaction, and the steps in order", () => {
    const m = model();
    expect(numbers(m.molecules)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(m.steps).toHaveLength(2);
    expect(numbers(moleculesOf(m, 0, "reactant"))).toEqual([1, 2]);
    expect(numbers(moleculesOf(m, 0, "agent"))).toEqual([4]);
    expect(numbers(moleculesOf(m, 0, "product"))).toEqual([3]);
    expect(numbers(moleculesOf(m, 1, "reactant"))).toEqual([3]);
    expect(numbers(unusedMolecules(m))).toEqual([6]);
    expect(m.molecules.every((x) => !x.edited)).toBe(true);
    expect(m.molecules[1]!.confidence).toBe(0.61);
  });

  it("joins the condition blocks of a step into one text, one line each", () => {
    expect(model().steps[0]!.conditions).toBe("Pd/C\nH2\nEtOH, 20 C");
    expect(model().steps[1]!.conditions).toBe("");
  });

  it("knows that a molecule can be in several steps", () => {
    expect(placementsOf(model(), "m3")).toEqual([
      { step: 0, role: "product" },
      { step: 1, role: "reactant" },
    ]);
    expect(placementsOf(model(), "m6")).toEqual([]);
  });

  it("ignores molecule numbers the result does not have, and repeats", () => {
    const m = modelFromResult(
      recognizeResult({ reactions: [{ reactants: [1, 1, 99], agents: [], products: [2], conditions: [], reaction_smiles: null }] }),
    );
    expect(m.steps[0]!.reactants).toEqual(["m1"]);
  });

  it("mode molecule: no steps, every molecule unused", () => {
    const m = modelFromResult(recognizeResult({ molecules: [molecule(1)], reactions: [], unassigned: [1], warnings: [] }));
    expect(m.steps).toEqual([]);
    expect(numbers(unusedMolecules(m))).toEqual([1]);
    expect(reactionSmiles(m, 0)).toBeNull();
  });
});

describe("changing roles and steps", () => {
  it("changes the role inside a step", () => {
    const m = model();
    place(m, "m2", { step: 0, role: "reactant" }, { step: 0, role: "agent" });
    expect(numbers(moleculesOf(m, 0, "reactant"))).toEqual([1]);
    expect(numbers(moleculesOf(m, 0, "agent"))).toEqual([4, 2]);
  });

  it("sets a molecule to not used in one step only", () => {
    const m = model();
    place(m, "m3", { step: 0, role: "product" }, null);
    expect(placementsOf(m, "m3")).toEqual([{ step: 1, role: "reactant" }]);
    place(m, "m3", { step: 1, role: "reactant" }, null);
    expect(numbers(unusedMolecules(m))).toEqual([3, 6]);
    expect(numbers(m.molecules)).toContain(3); // still shown
  });

  it("moves a molecule to another step, keeping its role", () => {
    const m = model();
    place(m, "m4", { step: 0, role: "agent" }, { step: 1, role: "agent" });
    expect(moleculesOf(m, 0, "agent")).toEqual([]);
    expect(numbers(moleculesOf(m, 1, "agent"))).toEqual([4]);
  });

  it("moves a molecule to a new step at the end", () => {
    const m = model();
    place(m, "m5", { step: 1, role: "product" }, { step: "new", role: "product" });
    expect(m.steps).toHaveLength(3);
    expect(numbers(moleculesOf(m, 2, "product"))).toEqual([5]);
    expect(moleculesOf(m, 1, "product")).toEqual([]);
  });

  it("gives an unused molecule a role in a step", () => {
    const m = model();
    place(m, "m6", null, { step: 1, role: "agent" });
    expect(numbers(moleculesOf(m, 1, "agent"))).toEqual([6]);
    expect(unusedMolecules(m)).toEqual([]);
  });

  it("never has a molecule twice in a step", () => {
    const m = model();
    // 3 is the product of step 1 and the reactant of step 2: moving the reactant use into step 1 merges them.
    place(m, "m3", { step: 1, role: "reactant" }, { step: 0, role: "reactant" });
    expect(placementsOf(m, "m3")).toEqual([{ step: 0, role: "reactant" }]);
    place(m, "m1", null, { step: 0, role: "reactant" });
    expect(m.steps[0]!.reactants.filter((k) => k === "m1")).toHaveLength(1);
  });

  it("ignores unknown molecules and steps", () => {
    const m = model();
    const before = JSON.stringify(m);
    place(m, "m99", null, { step: 0, role: "reactant" });
    place(m, "m1", null, { step: 7, role: "reactant" });
    expect(JSON.stringify(m)).toBe(before);
  });

  it("adds and removes steps; the molecules of a removed step stay", () => {
    const m = model();
    expect(addStep(m)).toBe(2);
    expect(m.steps[2]).toEqual({ reactants: [], agents: [], products: [], conditions: "" });
    removeStep(m, 0);
    expect(m.steps).toHaveLength(2);
    expect(numbers(m.molecules)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(numbers(unusedMolecules(m))).toEqual([1, 2, 4, 6]);
    removeStep(m, 9);
    expect(m.steps).toHaveLength(2);
  });
});

describe("reaction SMILES", () => {
  it("joins the host's SMILES as reactants>agents>products", () => {
    const m = model();
    expect(reactionSmiles(m, 0)).toBe("CCO.CCCO>CCCCCO>CCCCO");
    expect(reactionSmiles(m, 1)).toBe("CCCCO>>*CCO"); // a placeholder keeps its "*"
  });

  it("follows role and step changes", () => {
    const m = model();
    place(m, "m4", { step: 0, role: "agent" }, { step: 0, role: "reactant" });
    expect(reactionSmiles(m, 0)).toBe("CCO.CCCO.CCCCCO>>CCCCO");
    place(m, "m5", { step: 1, role: "product" }, { step: 0, role: "product" });
    expect(reactionSmiles(m, 0)).toBe("CCO.CCCO.CCCCCO>>CCCCO.*CCO");
    expect(reactionSmiles(m, 1)).toBe("CCCCO>>");
  });

  it("is not available when a reactant or product has no SMILES, or the step has neither", () => {
    const m = model();
    place(m, "m6", null, { step: 0, role: "reactant" });
    expect(reactionSmiles(m, 0)).toBeNull();
    const empty = model();
    addStep(empty);
    expect(reactionSmiles(empty, 2)).toBeNull();
    place(empty, "m4", { step: 0, role: "agent" }, { step: 2, role: "agent" });
    expect(reactionSmiles(empty, 2)).toBeNull(); // only something over the arrow
    expect(reactionSmiles(empty, 5)).toBeNull();
  });

  it("leaves an unreadable molecule over the arrow out", () => {
    const m = model();
    place(m, "m6", null, { step: 0, role: "agent" });
    expect(reactionSmiles(m, 0)).toBe("CCO.CCCO>CCCCCO>CCCCO");
  });

  it("is given for a step with only reactants or only products", () => {
    const m = model();
    place(m, "m3", { step: 0, role: "product" }, null);
    expect(reactionSmiles(m, 0)).toBe("CCO.CCCO>CCCCCO>");
  });
});

describe("editing", () => {
  it("an edited molecule takes the new structure, loses its confidence and is marked as edited", () => {
    const m = model();
    applyEdit(m, "m2", MOLFILE_B, { smiles: "CN", warnings: ["charged_or_salt"] });
    const edited = findMolecule(m, "m2")!;
    expect(edited).toMatchObject({ n: 2, molfile: MOLFILE_B, smiles: "CN", confidence: null, edited: true });
    // low_confidence was about the program's reading, not about the user's drawing.
    expect(edited.warnings).toEqual(["charged_or_salt"]);
    expect(placementsOf(m, "m2")).toEqual([{ step: 0, role: "reactant" }]);
    expect(reactionSmiles(m, 0)).toBe("CCO.CN>CCCCCO>CCCCO");
    expect(findMolecule(m, "m1")!.edited).toBe(false);
  });

  it("keeps only warnings a single structure can have from the description", () => {
    const m = model();
    applyEdit(m, "m1", MOLFILE_B, { smiles: "CN", warnings: ["stereo", "low_confidence" as never, "isotope"] });
    expect(findMolecule(m, "m1")!.warnings).toEqual(["isotope", "stereo"]);
  });

  it("an edit that cannot be read leaves no SMILES and no reaction SMILES", () => {
    const m = model();
    applyEdit(m, "m1", MOLFILE_B, { smiles: null, warnings: ["unparsable"] });
    expect(findMolecule(m, "m1")).toMatchObject({ smiles: null, warnings: ["unparsable"], edited: true });
    expect(reactionSmiles(m, 0)).toBeNull();
  });

  it("marks duplicates by equal SMILES after an edit, and takes the mark away again", () => {
    const m = model();
    applyEdit(m, "m2", MOLFILE_B, { smiles: "CCO", warnings: [] }); // now the same as molecule 1
    expect(findMolecule(m, "m1")!.warnings).toContain("duplicate");
    expect(findMolecule(m, "m2")!.warnings).toEqual(["duplicate"]);
    expect(findMolecule(m, "m3")!.warnings).not.toContain("duplicate");
    applyEdit(m, "m2", MOLFILE_B, { smiles: "CN", warnings: [] });
    expect(findMolecule(m, "m1")!.warnings).not.toContain("duplicate");
    expect(findMolecule(m, "m2")!.warnings).toEqual([]);
  });

  it("molecules without SMILES are never duplicates of each other", () => {
    const m = model();
    applyEdit(m, "m1", MOLFILE_B, { smiles: null, warnings: ["unparsable"] });
    refreshDuplicates(m);
    expect(findMolecule(m, "m1")!.warnings).toEqual(["unparsable"]);
    expect(findMolecule(m, "m6")!.warnings).toEqual(["unparsable"]);
  });

  it("a molecule drawn by the user gets the next number, no box, and starts unused", () => {
    const m = model();
    const key = addMolecule(m, MOLFILE_B, { smiles: "CN", warnings: [] });
    expect(findMolecule(m, key)).toMatchObject({ n: 7, box: null, smiles: "CN", confidence: null, edited: true });
    expect(numbers(unusedMolecules(m))).toEqual([6, 7]);
    removeMolecule(m, "m7");
    expect(addMolecule(m, MOLFILE_B, { smiles: "CN", warnings: [] })).toBe("m7");
  });

  it("removing a molecule takes it out of every step", () => {
    const m = model();
    removeMolecule(m, "m3");
    expect(numbers(m.molecules)).toEqual([1, 2, 4, 5, 6]);
    expect(m.steps[0]!.products).toEqual([]);
    expect(m.steps[1]!.reactants).toEqual([]);
    expect(reactionSmiles(m, 0)).toBe("CCO.CCCO>CCCCCO>");
  });
});

describe("copy all", () => {
  it("lists, per step, the reaction SMILES, every molecule with its role, and the conditions", () => {
    expect(copyAllText(model())).toBe(
      [
        "Step 1",
        "Reaction SMILES: CCO.CCCO>CCCCCO>CCCCO",
        "Reactant (1): CCO",
        "Reactant (2): CCCO",
        "Over the arrow (4): CCCCCO",
        "Product (3): CCCCO",
        "Conditions: Pd/C; H2; EtOH, 20 C",
        "",
        "Step 2",
        "Reaction SMILES: CCCCO>>*CCO",
        "Reactant (3): CCCCO",
        "Product (5): *CCO",
        "",
        "Not used in a reaction",
        "Molecule (6): no SMILES",
      ].join("\n"),
    );
  });

  it("uses the text as the user edited it, and says when a reaction SMILES is not available", () => {
    const m = model();
    m.steps[0]!.conditions = "  Pd/C, H2 \n\n EtOH\t20 °C ";
    place(m, "m6", null, { step: 1, role: "product" });
    const text = copyAllText(m);
    expect(text).toContain("Conditions: Pd/C, H2; EtOH 20 °C");
    expect(text).toContain("Reaction SMILES: not available");
    expect(text).not.toContain("Not used in a reaction");
  });

  it("one step is called Reaction; molecules alone are just listed", () => {
    const one = model();
    removeStep(one, 1);
    expect(copyAllText(one).startsWith("Reaction\n")).toBe(true);
    const none = modelFromResult(recognizeResult({ molecules: [molecule(1)], reactions: [], unassigned: [1] }));
    expect(copyAllText(none)).toBe("Molecule (1): CCO");
  });
});

describe("conditions as lines", () => {
  it("trims, drops blank lines and control characters", () => {
    expect(conditionLines(" a \r\n\r\n b\tc \n\u0007d")).toEqual(["a", "b c", "d"]);
    expect(conditionLines("")).toEqual([]);
  });
});

describe("the request for ChemDraw", () => {
  it("sends the current state: molfiles by role and the condition lines", () => {
    const m = model();
    m.steps[0]!.conditions = "Pd/C, H2\n\nEtOH";
    applyEdit(m, "m1", MOLFILE_B, { smiles: "CN", warnings: [] });
    place(m, "m4", { step: 0, role: "agent" }, null);
    const request = chemdrawRequest(m);
    expect(request).toEqual({
      ok: true,
      steps: [
        {
          reactants: [MOLFILE_B, findMolecule(m, "m2")!.molfile],
          agents: [],
          products: [findMolecule(m, "m3")!.molfile],
          conditions: ["Pd/C, H2", "EtOH"],
        },
        { reactants: [findMolecule(m, "m3")!.molfile], agents: [], products: [findMolecule(m, "m5")!.molfile], conditions: [] },
      ],
      // Molecules 4 (set to "not used") and 6 (in no step) are not in the file.
      leftOut: 2,
    });
  });

  it("leaves out steps without reactants and products, and unused molecules", () => {
    const m = model();
    addStep(m);
    place(m, "m4", { step: 0, role: "agent" }, { step: 2, role: "agent" });
    const request = chemdrawRequest(m);
    expect(request.ok && request.steps).toHaveLength(2);
  });

  it("names the molecule that has no structure instead of sending it", () => {
    const m = model();
    place(m, "m6", null, { step: 0, role: "reactant" });
    const request = chemdrawRequest(m);
    expect(request).toMatchObject({ ok: false });
    expect(!request.ok && request.problem).toContain("Molecule 6 has no structure");
  });

  it("says so when there is nothing to draw", () => {
    const m = modelFromResult(recognizeResult({ reactions: [] }));
    const request = chemdrawRequest(m);
    expect(!request.ok && request.problem).toContain("Give at least one molecule the role Reactant or Product first");
  });

  it("refuses what the host would refuse: too many steps, molecules, lines, or a line that is too long", () => {
    const many = model();
    for (let i = 0; i < MAX_CHEMDRAW_STEPS; i++) place(many, "m1", null, { step: "new", role: "reactant" });
    expect(chemdrawRequest(many)).toMatchObject({ ok: false, problem: expect.stringContaining(`more than ${MAX_CHEMDRAW_STEPS} steps`) });

    const crowded = modelFromResult(
      recognizeResult({
        molecules: Array.from({ length: MAX_CHEMDRAW_STEP_MOLECULES + 1 }, (_v, i) => molecule(i + 1)),
        reactions: [
          { reactants: Array.from({ length: MAX_CHEMDRAW_STEP_MOLECULES + 1 }, (_v, i) => i + 1), agents: [], products: [], conditions: [], reaction_smiles: null },
        ],
      }),
    );
    expect(chemdrawRequest(crowded)).toMatchObject({ ok: false, problem: expect.stringContaining("more than 10 molecules") });

    const wordy = model();
    wordy.steps[0]!.conditions = Array.from({ length: MAX_CHEMDRAW_CONDITION_LINES + 1 }, (_v, i) => `line ${i}`).join("\n");
    expect(chemdrawRequest(wordy)).toMatchObject({ ok: false, problem: expect.stringContaining("lines of conditions") });

    const long = model();
    long.steps[0]!.conditions = "x".repeat(MAX_REACTION_TEXT + 1);
    expect(chemdrawRequest(long)).toMatchObject({ ok: false, problem: expect.stringContaining(`longer than ${MAX_REACTION_TEXT}`) });
    long.steps[0]!.conditions = "x".repeat(MAX_REACTION_TEXT);
    expect(chemdrawRequest(long).ok).toBe(true);
  });
});

describe("found in review", () => {
  it("a save that gives the SMILES the molecule already has is not an edit", () => {
    const m = model();
    const before = structuredClone(findMolecule(m, "m2")!);
    expect(before.warnings).toEqual(["stereo", "low_confidence"]);
    expect(applyEdit(m, "m2", MOLFILE_B, { smiles: before.smiles, warnings: ["stereo"] })).toBe(false);
    expect(findMolecule(m, "m2")).toEqual(before); // score, warnings, molfile, "not edited": all as they were
    // A different SMILES is an edit.
    expect(applyEdit(m, "m2", MOLFILE_B, { smiles: "CN", warnings: [] })).toBe(true);
    expect(findMolecule(m, "m2")).toMatchObject({ smiles: "CN", edited: true, confidence: null, warnings: [] });
  });

  it("a drawing without SMILES always counts as an edit, also on a molecule that had none", () => {
    const m = model();
    expect(findMolecule(m, "m6")!.smiles).toBeNull();
    expect(applyEdit(m, "m6", MOLFILE_B, { smiles: null, warnings: ["unparsable"] })).toBe(true);
    expect(findMolecule(m, "m6")).toMatchObject({ edited: true, molfile: MOLFILE_B });
    expect(applyEdit(m, "nobody", MOLFILE_B, { smiles: "CN", warnings: [] })).toBe(false);
  });

  it("knows the role a molecule has in a step", () => {
    const m = model();
    expect(roleIn(m, "m3", 0)).toBe("product");
    expect(roleIn(m, "m3", 1)).toBe("reactant");
    expect(roleIn(m, "m4", 0)).toBe("agent");
    expect(roleIn(m, "m4", 1)).toBeNull();
    expect(roleIn(m, "m4", 7)).toBeNull();
  });

  it("the ChemDraw request counts the molecules it leaves out", () => {
    const m = model();
    const request = chemdrawRequest(m);
    expect(request.ok && request.leftOut).toBe(1); // molecule 6
    place(m, "m6", null, { step: 1, role: "agent" });
    findMolecule(m, "m6")!.molfile = MOLFILE_B;
    const all = chemdrawRequest(m);
    expect(all.ok && all.leftOut).toBe(0);
    // A molecule that is only in a step that is not drawn (no reactant, no product) is left out too.
    place(m, "m4", { step: 0, role: "agent" }, { step: "new", role: "agent" });
    const partly = chemdrawRequest(m);
    expect(partly.ok && partly.steps).toHaveLength(2);
    expect(partly.ok && partly.leftOut).toBe(1);
  });

  it("a single molecule cannot go to ChemDraw without a role, and the message says how", () => {
    const one = modelFromResult(recognizeResult({ molecules: [recognizeResult().molecules[0]!], reactions: [], unassigned: [1] }));
    const request = chemdrawRequest(one);
    expect(!request.ok && request.problem).toContain("To open this molecule there, give it the role Reactant or Product first");
    const none = modelFromResult(recognizeResult({ molecules: [], reactions: [], unassigned: [] }));
    const empty = chemdrawRequest(none);
    expect(!empty.ok && empty.problem).toBe("There is no molecule to draw.");
  });
});
