/**
 * Loading one PDF of a stereo variant through `read_pdf`, piece by piece
 * (Firefox limits a native message to 1 MB). No DOM.
 *
 * The page never sends a path: only the CORE_STEREO plus either a file name
 * from the folder listing or the full ID and spectrum key of a recorded
 * spectrum. Every answer is checked against what was asked; anything that does
 * not fit ends the load with an error in plain words. If the file's size
 * changes between answers (the file was being written), the read starts again
 * once. A load can be cancelled: no request is sent after that and late
 * answers are ignored.
 *
 * The caller turns the bytes into `new Blob([bytes], { type: "application/pdf" })`:
 * the type is always PDF, never taken from the data.
 */

import { MAX_PDF_BYTES, PDF_CHUNK_BYTES, type HostError, type ReadPdfParams } from "../shared/protocol";
import type { HostClient } from "../content/host-client";

/** Which PDF: a file lying in the folder, or the PDF recorded for one spectrum. */
export type PdfSource =
  | { kind: "file"; coreStereoId: string; name: string }
  | { kind: "spectrum"; coreStereoId: string; fullId: string; spectrumKey: string };

export type PdfOutcome =
  | { status: "ok"; name: string; bytes: Uint8Array }
  | { status: "error"; error: HostError }
  | { status: "cancelled" };

export interface PdfLoad {
  /** Settles once: with the bytes, an error, or "cancelled". Never rejects. */
  readonly done: Promise<PdfOutcome>;
  /** Stops the load: nothing more is asked, and the outcome is "cancelled". */
  cancel(): void;
}

/** Bytes so far and the file's size, after every piece. */
export type PdfProgress = (received: number, size: number) => void;

/** Code of the load's own errors (an answer that does not fit what was asked). */
export const PDF_BAD_PIECE = "host_bad_response";

/** "Loading the PDF… 40 %" (the size may still be unknown). */
export function progressText(received: number, size: number | null): string {
  if (size === null || size <= 0) return "Loading the PDF…";
  const percent = Math.min(100, Math.floor((received / size) * 100));
  return `Loading the PDF… ${percent} %`;
}

export function pdfParams(source: PdfSource, offset: number): ReadPdfParams {
  return source.kind === "file"
    ? { core_stereo_id: source.coreStereoId, name: source.name, offset }
    : { core_stereo_id: source.coreStereoId, full_id: source.fullId, spectrum_key: source.spectrumKey, offset };
}

/** Base64 to bytes; null when it is not base64. */
export function decodeBase64(text: string): Uint8Array | null {
  let binary: string;
  try {
    binary = atob(text);
  } catch {
    return null;
  }
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

const PDF_MAGIC = [0x25, 0x50, 0x44, 0x46, 0x2d]; // "%PDF-"

function damaged(detail: string): { status: "error"; error: HostError } {
  return {
    status: "error",
    error: { code: PDF_BAD_PIECE, message: `The PDF did not arrive correctly (${detail}). Try again.` },
  };
}

/** Thrown inside the loop when the file changed size; caught to start again. */
class SizeChanged extends Error {}

export function loadPdf(client: HostClient, source: PdfSource, onProgress?: PdfProgress): PdfLoad {
  let cancelled = false;
  let settle!: (outcome: PdfOutcome) => void;
  const done = new Promise<PdfOutcome>((resolve) => {
    settle = resolve;
  });
  let settled = false;
  const finish = (outcome: PdfOutcome): void => {
    if (settled) return;
    settled = true;
    settle(outcome);
  };

  /** One full read from offset 0. */
  async function readOnce(): Promise<PdfOutcome | null> {
    let size: number | null = null;
    let name = "";
    let bytes: Uint8Array | null = null;
    let received = 0;
    for (;;) {
      if (cancelled) return null;
      const reply = await client.request("read_pdf", pdfParams(source, received));
      if (cancelled) return null; // a late answer
      if (!reply.ok) return { status: "error", error: reply.error };
      const piece = reply.result;
      if (piece.offset !== received) return damaged("a piece came for another place in the file");
      if (size === null) {
        if (piece.size > MAX_PDF_BYTES) {
          return {
            status: "error",
            error: { code: "file_unavailable", message: "The PDF is larger than 100 MB and cannot be shown here." },
          };
        }
        if (piece.size === 0) return { status: "error", error: { code: "file_unavailable", message: "The PDF file is empty." } };
        size = piece.size;
        name = piece.name;
        bytes = new Uint8Array(size);
      } else if (piece.size !== size) {
        throw new SizeChanged();
      }
      const data = decodeBase64(piece.data);
      if (data === null) return damaged("a piece was not readable");
      if (data.length > PDF_CHUNK_BYTES) return damaged("a piece was larger than allowed");
      if (received + data.length > size) return damaged("more bytes came than the file has");
      if (data.length === 0 && !piece.eof) return damaged("an empty piece came before the end");
      bytes!.set(data, received);
      received += data.length;
      onProgress?.(received, size);
      if (piece.eof) {
        if (received !== size) return damaged("the file ended early");
        break;
      }
    }
    if (!PDF_MAGIC.every((b, i) => bytes![i] === b)) return damaged("it is not a PDF file");
    return { status: "ok", name, bytes: bytes! };
  }

  void (async () => {
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          const outcome = await readOnce();
          finish(outcome ?? { status: "cancelled" });
          return;
        } catch (error) {
          if (!(error instanceof SizeChanged)) throw error;
          if (cancelled) {
            finish({ status: "cancelled" });
            return;
          }
        }
      }
      finish({
        status: "error",
        error: {
          code: "file_unavailable",
          message: "The PDF kept changing while it was read. Wait until it is saved completely, then try again.",
        },
      });
    } catch (error) {
      finish({ status: "error", error: { code: "internal_error", message: `The PDF could not be loaded: ${String(error)}` } });
    }
  })();

  return {
    done,
    cancel: () => {
      if (cancelled) return;
      cancelled = true;
      finish({ status: "cancelled" });
    },
  };
}
