import { describe, expect, it, vi } from "vitest";
import {
  createCaptureClient,
  isChemdrawResult,
  isDescribeResult,
  isRecognitionStatus,
  isRecognizeResult,
} from "../src/capture/client";
import { CAPTURE_FAIL_REASONS } from "../src/background/capture";
import {
  CAPTURE_FAIL_TEXT,
  INSTALL_COMMAND,
  MOLECULE_WARNING_TEXT,
  warningText,
  RESULT_WARNING_TEXT,
  captureFailText,
  confidenceText,
  errorSentence,
  statusText,
} from "../src/capture/texts";
import { MOLECULE_WARNING_CODES, RECOGNITION_STATES, RECOGNITION_WARNING_CODES } from "../src/shared/protocol";
import { recognizeResult } from "./capture-fixtures";

describe("capture client", () => {
  it("asks the background for the screenshot by id", async () => {
    const send = vi.fn(async () => ({ ok: true, result: { image: "data:image/png;base64,AAAA" } }));
    const client = createCaptureClient(send);
    expect(await client.image("abc")).toEqual({ ok: true, result: { image: "data:image/png;base64,AAAA" } });
    expect(send).toHaveBeenCalledWith({ type: "capture-image", id: "abc" });
  });

  it.each([
    ["not a PNG data URL", { ok: true, result: { image: "https://example.org/x.png" } }],
    ["no image", { ok: true, result: {} }],
    ["nothing", undefined],
    ["an error without a message", { ok: false, error: { code: "x" } }],
  ])("refuses a screenshot answer that is %s", async (_name, raw) => {
    const reply = await createCaptureClient(async () => raw).image("abc");
    expect(reply).toMatchObject({ ok: false, error: { code: "host_bad_response" } });
  });

  it("passes an error of the background on, and turns a rejection into host_unavailable", async () => {
    const expired = { ok: false, error: { code: "capture_expired", message: "gone" } };
    expect(await createCaptureClient(async () => expired).image("abc")).toEqual(expired);
    const failing = createCaptureClient(async () => {
      throw new Error("Could not establish connection");
    });
    expect(await failing.image("abc")).toMatchObject({ ok: false, error: { code: "host_unavailable" } });
    const reply = await failing.request("recognition_status", { start: true });
    expect(reply).toMatchObject({ ok: false, error: { code: "host_unavailable" } });
    expect(!reply.ok && reply.error.message).toContain("Could not establish connection");
  });

  it("sends host requests in the background's envelope and checks the answer's shape", async () => {
    const result = recognizeResult();
    const send = vi.fn(async () => ({ ok: true, result }));
    const client = createCaptureClient(send);
    expect(await client.request("recognize", { image: "AAAA", mode: "reaction" })).toEqual({ ok: true, result });
    expect(send).toHaveBeenCalledWith({ type: "host-request", op: "recognize", params: { image: "AAAA", mode: "reaction" } });
    // The same answer is not a status.
    expect(await client.request("recognition_status", {})).toMatchObject({ ok: false, error: { code: "host_bad_response" } });
  });

  it("passes host errors on unchanged", async () => {
    const error = { ok: false, error: { code: "recognition_failed", message: "The worker stopped." } };
    expect(await createCaptureClient(async () => error).request("recognize", { image: "A", mode: "molecule" })).toEqual(error);
  });
});

describe("answer checks", () => {
  it("accept the contract's shapes", () => {
    expect(isRecognizeResult(recognizeResult())).toBe(true);
    expect(isRecognizeResult(recognizeResult({ molecules: [], reactions: [], unassigned: [], warnings: ["no_molecules"] }))).toBe(true);
    for (const state of RECOGNITION_STATES) expect(isRecognitionStatus({ installed: state !== "not_installed", state })).toBe(true);
    expect(isRecognitionStatus({ installed: true, state: "loading", detail: "x", load_seconds: 3.5 })).toBe(true);
    expect(isDescribeResult({ structures: [{ smiles: "CCO", warnings: ["stereo"] }, { smiles: null, warnings: ["unparsable"] }] })).toBe(true);
    expect(isChemdrawResult({ path: "C:\\Temp\\r.cdxml", opened: true })).toBe(true);
    expect(isChemdrawResult({ path: "/tmp/r.cdxml", opened: false, message: "ChemDraw is not installed." })).toBe(true);
  });

  const good = recognizeResult();
  it.each([
    ["no width", { ...good, width: undefined }],
    ["width zero", { ...good, width: 0 }],
    ["molecules not a list", { ...good, molecules: {} }],
    ["a molecule without a box", { ...good, molecules: [{ ...good.molecules[0], box: [1, 2, 3] }] }],
    ["a molecule number that is text", { ...good, molecules: [{ ...good.molecules[0], n: "1" }] }],
    ["a molecule number zero", { ...good, molecules: [{ ...good.molecules[0], n: 0 }] }],
    ["SMILES that is a number", { ...good, molecules: [{ ...good.molecules[0], smiles: 5 }] }],
    ["an unknown molecule warning", { ...good, molecules: [{ ...good.molecules[0], warnings: ["made_up"] }] }],
    ["confidence that is text", { ...good, molecules: [{ ...good.molecules[0], confidence: "high" }] }],
    ["a reaction without products", { ...good, reactions: [{ ...good.reactions[0], products: undefined }] }],
    ["condition lines that are not text", { ...good, reactions: [{ ...good.reactions[0], conditions: [{ box: [0, 0, 1, 1], lines: [1] }] }] }],
    ["an unknown result warning", { ...good, warnings: ["made_up"] }],
    ["unassigned not numbers", { ...good, unassigned: ["6"] }],
    ["null", null],
  ])("refuse a recognition result with %s", (_name, value) => {
    expect(isRecognizeResult(value)).toBe(false);
  });

  it("refuse other malformed answers", () => {
    expect(isRecognitionStatus({ installed: "yes", state: "ready" })).toBe(false);
    expect(isRecognitionStatus({ installed: true, state: "warming" })).toBe(false);
    expect(isRecognitionStatus({ installed: true, state: "ready", detail: 5 })).toBe(false);
    expect(isDescribeResult({ structures: [{ smiles: "C", warnings: ["low_confidence"] }] })).toBe(false); // not a structure warning
    expect(isDescribeResult({ structures: "x" })).toBe(false);
    expect(isChemdrawResult({ path: 5, opened: true })).toBe(false);
    expect(isChemdrawResult({ path: "x", opened: "yes" })).toBe(false);
  });
});

describe("texts", () => {
  it("there is a plain sentence for every warning code", () => {
    expect(Object.keys(MOLECULE_WARNING_TEXT).sort()).toEqual([...MOLECULE_WARNING_CODES].sort());
    expect(Object.keys(RESULT_WARNING_TEXT).sort()).toEqual([...RECOGNITION_WARNING_CODES].sort());
    expect(MOLECULE_WARNING_TEXT.isotope).toBe("Contains an isotope label - probably a misread");
    expect(MOLECULE_WARNING_TEXT.stereo).toBe("Has stereochemistry - please check the wedges and the double-bond geometry (E/Z)");
    expect(MOLECULE_WARNING_TEXT.charged_or_salt).toBe("Salt or charged - check the counter-ion");
  });

  it("a molecule the user drew is not told that its isotope or metal is probably a misread", () => {
    expect(warningText("isotope", false)).toBe("Contains an isotope label - probably a misread");
    expect(warningText("isotope", true)).toBe("Contains an isotope label - check that you meant it");
    expect(warningText("unusual_element", false)).toContain("probably a misread");
    expect(warningText("unusual_element", true)).toBe("Contains an unusual element - check that you meant it");
    expect(warningText("stereo", true)).toBe(MOLECULE_WARNING_TEXT.stereo);
  });

  it("writes the program's score as a number from 0 to 1, not as a percentage that reads like a probability", () => {
    expect(confidenceText(0.896)).toBe("Program's own score: 0.90");
    expect(confidenceText(1.2)).toBe("Program's own score: 1.00");
    expect(confidenceText(-3)).toBe("Program's own score: 0.00");
    expect(confidenceText(0.61)).not.toContain("%");
    expect(confidenceText(null)).toBeNull();
    expect(confidenceText(Number.NaN)).toBeNull();
  });

  it("explains every state of the recognition part", () => {
    const missing = statusText({ installed: false, state: "not_installed" });
    expect(missing.kind).toBe("warn");
    expect(missing.command).toBe("compound-registry install-recognition");
    expect(INSTALL_COMMAND).toBe("compound-registry install-recognition");
    // The host's own text names the folder and the exact command of this PC: shown as it stands.
    const where = "It is not installed. Open a terminal in the folder C:\Tools\ChemLinker and type: .venv\Scripts\compound-registry.exe install-recognition";
    const told = statusText({ installed: false, state: "not_installed", detail: where });
    expect(told.kind).toBe("warn");
    expect(told.text).toBe(`${where} Then press Check again.`);
    expect(told.command).toBeUndefined();
    expect(statusText({ installed: true, state: "loading", load_seconds: 12.4 }).text).toContain("(12 s so far)");
    expect(statusText({ installed: true, state: "stopped" }).kind).toBe("info");
    expect(statusText({ installed: true, state: "ready" }).kind).toBe("ok");
    // The user knows it by the name the installer asked with.
    expect(missing.text).toMatch(/^ChemResolver is not installed on this PC\./);
    expect(statusText({ installed: true, state: "ready" }).text).toBe("ChemResolver is ready.");
    expect(statusText({ installed: true, state: "loading" }).text).toMatch(/^ChemResolver is loading its models\./);
    expect(statusText({ installed: true, state: "failed" }).text).toBe("ChemResolver could not be started.");
    const failed = statusText({ installed: true, state: "failed", detail: "The worker ended with exit code 1." });
    expect(failed.kind).toBe("error");
    expect(failed.text).toContain("exit code 1");
  });

  it("puts errors into words for this page", () => {
    expect(errorSentence({ code: "unknown_op", message: "Unknown operation 'recognize'." })).toContain("Update the program");
    expect(errorSentence({ code: "recognition_not_installed", message: "x" })).toContain(INSTALL_COMMAND);
    expect(errorSentence({ code: "recognition_not_installed", message: "x" })).toMatch(/^ChemResolver is not installed/);
    expect(errorSentence({ code: "recognition_not_installed", message: "Type: .venv\Scripts\compound-registry.exe install-recognition" })).toBe(
      "Type: .venv\Scripts\compound-registry.exe install-recognition",
    );
    const timeout = { code: "host_timeout", message: "No answer within 300 seconds." };
    expect(errorSentence(timeout, "recognize")).toBe("No answer within 300 seconds. ChemResolver took too long. Try a smaller selection.");
    // Only a recognition has a selection to make smaller.
    for (const op of ["recognition_status", "describe_structures", "reaction_to_chemdraw", undefined] as const) {
      expect(errorSentence(timeout, op)).not.toContain("selection");
      expect(errorSentence(timeout, op)).not.toContain("took too long");
      expect(errorSentence(timeout, op)).toContain("No answer within 300 seconds. The program may be busy");
    }
    expect(errorSentence({ code: "image_invalid", message: "The picture is not a PNG." })).toBe("The picture is not a PNG.");
  });
});

describe("why a capture did not start", () => {
  it("has paragraphs for exactly the background's reasons, and for nothing else", () => {
    expect(Object.keys(CAPTURE_FAIL_TEXT).sort()).toEqual([...CAPTURE_FAIL_REASONS].sort());
    for (const reason of CAPTURE_FAIL_REASONS) {
      expect(captureFailText(reason)!.length).toBeGreaterThan(0);
      // The Chrome bundles must not contain this word (see chrome-build.test.ts).
      expect(captureFailText(reason)!.join(" ")).not.toMatch(/browser/i);
    }
    for (const other of [null, undefined, "", "constructor", "toString", "__proto__", "Not_Capturable", "<b>"]) {
      expect(captureFailText(other)).toBeNull();
    }
  });
});
