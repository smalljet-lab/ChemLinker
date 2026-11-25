/**
 * The capture page's only way out: `browser.runtime.sendMessage` to the
 * background, which serves it the screenshot (`capture-image`) and the four
 * host operations of CAPTURE_OPS. Every answer is checked before the page
 * uses it; nothing here rejects.
 *
 * No time limit of its own: the background's native client ends every request
 * (RECOGNIZE_TIMEOUT_MS for `recognize`, 30 s for the others).
 */

import {
  MOLECULE_WARNING_CODES,
  RECOGNITION_STATES,
  RECOGNITION_WARNING_CODES,
  STRUCTURE_WARNING_CODES,
  type BackgroundReply,
  type BackgroundRequest,
  type CaptureImageReply,
  type CaptureImageRequest,
  type CaptureOp,
  type DescribeStructuresResult,
  type HostError,
  type ParamsByOp,
  type PixelBox,
  type ReactionToChemdrawResult,
  type RecognitionStatusResult,
  type RecognizeResult,
  type ResultByOp,
} from "../shared/protocol";

export interface CaptureClient {
  /** The screenshot the page was opened for, as a PNG data URL. */
  image(id: string): Promise<CaptureImageReply>;
  request<K extends CaptureOp>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>>;
}

type Rec = Record<string, unknown>;

function isRecord(value: unknown): value is Rec {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const isString = (value: unknown): value is string => typeof value === "string";
const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const isStringOrNull = (value: unknown): value is string | null => value === null || isString(value);

function isListOf<T>(value: unknown, check: (item: unknown) => item is T): value is T[] {
  return Array.isArray(value) && value.every(check);
}

const isCode = (codes: readonly string[]) => (value: unknown): value is string => isString(value) && codes.includes(value);
const isIndex = (value: unknown): value is number => isNumber(value) && Number.isInteger(value) && value >= 1;

function isBox(value: unknown): value is PixelBox {
  return Array.isArray(value) && value.length === 4 && value.every(isNumber);
}

function isHostError(value: unknown): value is HostError {
  return isRecord(value) && isString(value.code) && isString(value.message);
}

export function isRecognitionStatus(value: unknown): value is RecognitionStatusResult {
  return (
    isRecord(value) &&
    typeof value.installed === "boolean" &&
    isCode(RECOGNITION_STATES)(value.state) &&
    (value.detail === undefined || isString(value.detail)) &&
    (value.load_seconds === undefined || isNumber(value.load_seconds))
  );
}

function isMolecule(value: unknown): boolean {
  return (
    isRecord(value) &&
    isIndex(value.n) &&
    isBox(value.box) &&
    isStringOrNull(value.smiles) &&
    isStringOrNull(value.molfile) &&
    (value.confidence === null || isNumber(value.confidence)) &&
    isListOf(value.warnings, isCode(MOLECULE_WARNING_CODES))
  );
}

function isReaction(value: unknown): boolean {
  return (
    isRecord(value) &&
    isListOf(value.reactants, isIndex) &&
    isListOf(value.agents, isIndex) &&
    isListOf(value.products, isIndex) &&
    Array.isArray(value.conditions) &&
    value.conditions.every((c) => isRecord(c) && isBox(c.box) && isListOf(c.lines, isString)) &&
    isStringOrNull(value.reaction_smiles)
  );
}

export function isRecognizeResult(value: unknown): value is RecognizeResult {
  return (
    isRecord(value) &&
    isNumber(value.width) &&
    value.width > 0 &&
    isNumber(value.height) &&
    value.height > 0 &&
    Array.isArray(value.molecules) &&
    value.molecules.every(isMolecule) &&
    Array.isArray(value.reactions) &&
    value.reactions.every(isReaction) &&
    isListOf(value.unassigned, isIndex) &&
    isNumber(value.seconds) &&
    isListOf(value.warnings, isCode(RECOGNITION_WARNING_CODES))
  );
}

export function isDescribeResult(value: unknown): value is DescribeStructuresResult {
  return (
    isRecord(value) &&
    Array.isArray(value.structures) &&
    value.structures.every((s) => isRecord(s) && isStringOrNull(s.smiles) && isListOf(s.warnings, isCode(STRUCTURE_WARNING_CODES)))
  );
}

export function isChemdrawResult(value: unknown): value is ReactionToChemdrawResult {
  return (
    isRecord(value) &&
    isString(value.path) &&
    typeof value.opened === "boolean" &&
    (value.message === undefined || value.message === null || isString(value.message))
  );
}

const RESULT_CHECKS: { [K in CaptureOp]: (value: unknown) => value is ResultByOp[K] } = {
  recognition_status: isRecognitionStatus,
  recognize: isRecognizeResult,
  describe_structures: isDescribeResult,
  reaction_to_chemdraw: isChemdrawResult,
};

function failure(code: string, message: string): { ok: false; error: HostError } {
  return { ok: false, error: { code, message } };
}

function noAnswer(error: unknown): { ok: false; error: HostError } {
  const reason = error instanceof Error && error.message ? error.message : String(error);
  return failure(
    "host_unavailable",
    `The extension's background script did not answer (${reason}). Reload this page; if that does not help, reload the extension.`,
  );
}

const UNREADABLE = "The extension's background script sent an unreadable answer.";

/**
 * @param send `(message) => browser.runtime.sendMessage(message)`
 */
export function createCaptureClient(send: (message: BackgroundRequest | CaptureImageRequest) => Promise<unknown>): CaptureClient {
  return {
    async image(id: string): Promise<CaptureImageReply> {
      let raw: unknown;
      try {
        raw = await send({ type: "capture-image", id });
      } catch (error) {
        return noAnswer(error);
      }
      if (isRecord(raw) && raw.ok === true && isRecord(raw.result) && isString(raw.result.image) && raw.result.image.startsWith("data:image/png;base64,")) {
        return { ok: true, result: { image: raw.result.image } };
      }
      if (isRecord(raw) && raw.ok === false && isHostError(raw.error)) return failure(raw.error.code, raw.error.message);
      return failure("host_bad_response", UNREADABLE);
    },

    async request<K extends CaptureOp>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>> {
      const message = { type: "host-request", op, params } as BackgroundRequest;
      let raw: unknown;
      try {
        raw = await send(message);
      } catch (error) {
        return noAnswer(error);
      }
      if (isRecord(raw) && raw.ok === false && isHostError(raw.error)) return failure(raw.error.code, raw.error.message);
      if (!isRecord(raw) || raw.ok !== true) return failure("host_bad_response", UNREADABLE);
      if (!RESULT_CHECKS[op](raw.result)) {
        return failure("host_bad_response", `The local registry program sent a '${op}' answer that this extension does not understand.`);
      }
      return { ok: true, result: raw.result as ResultByOp[K] };
    },
  };
}
