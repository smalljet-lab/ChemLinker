/** Invented data for the capture tests: a two-step scheme with one molecule in no step. */

import type { RecognizeResult, RecognizedMolecule } from "../src/shared/protocol";

export const MOLFILE_A = "\n  invented\n\n  2  1  0  0  0  0            999 V2000\n    0.0000    0.0000    0.0000 C   0  0\n    0.8250    0.0000    0.0000 O   0  0\n  1  2  1  0\nM  END\n";
export const MOLFILE_B = MOLFILE_A.replace(" O ", " N ");

export function molecule(n: number, over: Partial<RecognizedMolecule> = {}): RecognizedMolecule {
  return {
    n,
    box: [n * 100, 10, n * 100 + 80, 90],
    smiles: `C${"C".repeat(n)}O`,
    molfile: MOLFILE_A.replace("invented", `invented ${n}`),
    confidence: 0.9,
    warnings: [],
    ...over,
  };
}

/**
 * 1 + 2 -> 3 over 4 ("Pd/C", "H2"), then 3 -> 5; molecule 6 is in no step.
 * 2 has stereo and low confidence, 5 has a placeholder, 6 could not be read.
 */
export function recognizeResult(over: Partial<RecognizeResult> = {}): RecognizeResult {
  return {
    width: 800,
    height: 400,
    molecules: [
      molecule(1),
      molecule(2, { confidence: 0.61, warnings: ["stereo", "low_confidence"] }),
      molecule(3),
      molecule(4),
      molecule(5, { smiles: "*CCO", warnings: ["placeholder"] }),
      molecule(6, { smiles: null, molfile: null, confidence: null, warnings: ["unparsable"] }),
    ],
    reactions: [
      {
        reactants: [1, 2],
        agents: [4],
        products: [3],
        conditions: [
          { box: [300, 100, 380, 120], lines: ["Pd/C", "H2"] },
          { box: [300, 130, 380, 150], lines: ["EtOH, 20 C"] },
        ],
        reaction_smiles: "CCO.CCCO>CCCCCO>CCCCO",
      },
      { reactants: [3], agents: [], products: [5], conditions: [], reaction_smiles: "CCCCO>>*CCO" },
    ],
    unassigned: [6],
    seconds: 12.4,
    warnings: ["unassigned_molecules"],
    ...over,
  };
}
