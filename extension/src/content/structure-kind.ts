/**
 * Decides whether pasted text is a molfile or a SMILES.
 *
 * Text with a line break INSIDE it (not only at its start or end) or with
 * `M  END` is a molfile and is used VERBATIM (leading header lines matter).
 * Anything else is a SMILES with surrounding whitespace removed, so a SMILES
 * copied together with its line end is still a SMILES. Same rule as the CLI.
 */

import { MAX_STRUCTURE_CHARS, type StructureInput } from "../shared/protocol";

export type DetectedStructure =
  | { kind: "empty" }
  | { kind: "too_large"; length: number }
  | { kind: "molfile"; structure: { molfile: string }; lines: number }
  | { kind: "smiles"; structure: { smiles: string } };

export function isMolfileText(text: string): boolean {
  return /[\r\n]/.test(text.trim()) || text.includes("M  END");
}

export function detectStructure(text: string, maxChars: number = MAX_STRUCTURE_CHARS): DetectedStructure {
  if (text.trim() === "") return { kind: "empty" };
  if (isMolfileText(text)) {
    if (text.length > maxChars) return { kind: "too_large", length: text.length };
    return { kind: "molfile", structure: { molfile: text }, lines: text.split(/\r\n|\r|\n/).length };
  }
  const smiles = text.trim();
  if (smiles.length > maxChars) return { kind: "too_large", length: smiles.length };
  return { kind: "smiles", structure: { smiles } };
}

export function sameStructure(a: StructureInput, b: StructureInput): boolean {
  return a.molfile === b.molfile && a.smiles === b.smiles;
}
