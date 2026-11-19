import { describe, expect, it } from "vitest";
import { createRuntimeHostClient } from "../src/content/host-client";
import {
  isBase64Text,
  isCompoundFilesResult,
  isPlainFileName,
  isReadPdfResult,
  isRegisterResult,
  isResultOf,
  isSpectrumInfo,
  MAX_SPECTRA,
  MAX_SPECTRUM_PARAMETERS,
  readFilesFolder,
  readRegisterFolder,
} from "../src/content/validate";
import { batchYieldText } from "../src/content/yields";
import { presentError } from "../src/content/errors";
import { FilesController, folderFileLine, formatFrequency, humanSize, otherFiles, spectrumHeading } from "../src/compounds/files";
import { decodeBase64, loadPdf, pdfParams, progressText, type PdfOutcome, type PdfSource } from "../src/compounds/pdf";
import { safePdfFileName } from "../src/compounds/pdf-viewer";
import {
  FILE_UNAVAILABLE,
  MAX_FOLDER_FILES,
  MAX_PDF_BYTES,
  PDF_CHUNK_BYTES,
  type CompoundFilesResult,
  type FolderFile,
  type ReadPdfParams,
  type ReadPdfResult,
  type SpectrumInfo,
} from "../src/shared/protocol";
import { FakeClient, flush, pingResult, registerResult, spectrum } from "./helpers";

// ------------------------------------------------------------------ fixtures

function folderFile(over: Partial<FolderFile> = {}): FolderFile {
  return { name: "000042_01.mol", size: 1200, modified: "2026-11-17T14:00:00+00:00", is_pdf: false, recorded: false, ...over };
}

function filesResult(over: Partial<CompoundFilesResult> = {}): CompoundFilesResult {
  return {
    core_stereo_id: "000042_01",
    folder: { path: "C:\\Registry\\compounds\\000042_01", exists: true },
    spectra: [],
    files: [],
    files_truncated: false,
    warnings: [],
    ...over,
  };
}

function b64(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64");
}

function pdfBytes(length: number): Uint8Array {
  const bytes = new Uint8Array(length);
  for (let i = 0; i < length; i++) bytes[i] = (i * 37 + 11) & 0xff;
  bytes.set([0x25, 0x50, 0x44, 0x46, 0x2d], 0); // %PDF-
  return bytes;
}

function piece(over: Partial<ReadPdfResult> = {}): ReadPdfResult {
  return { name: "000042_01_001_FC2.pdf", size: 10, offset: 0, data: "", eof: false, ...over };
}

// ------------------------------------------------------------------ validators

describe("validators: compound_files", () => {
  it("accepts a full answer, an empty one and a null folder", () => {
    const full = filesResult({
      spectra: [
        spectrum({
          report: "δ 7.26 (s, 1H)",
          parameters: [{ name: "Pulse", value: "zg30" }],
          mnova_file: "C:\\x\\a.mnova",
          pdf_file: "C:\\x\\a.pdf",
          pdf_available: true,
          frequency_mhz: null,
          dimensions: null,
        }),
      ],
      files: [folderFile(), folderFile({ name: "a.pdf", is_pdf: true, recorded: true, size: 0 })],
      files_truncated: true,
      warnings: ["note"],
    });
    expect(isCompoundFilesResult(full)).toBe(true);
    expect(isCompoundFilesResult(filesResult())).toBe(true);
    expect(isCompoundFilesResult(filesResult({ folder: null }))).toBe(true);
    expect(isResultOf("compound_files", full, 1)).toBe(true);
  });

  const bad: [string, unknown][] = [
    ["no core_stereo_id", { ...filesResult(), core_stereo_id: "" }],
    ["folder as text", { ...filesResult(), folder: "C:\\x" }],
    ["folder without exists", { ...filesResult(), folder: { path: "C:\\x" } }],
    ["blank folder path", { ...filesResult(), folder: { path: " ", exists: true } }],
    ["folder path with a line break", { ...filesResult(), folder: { path: "C:\\x\nC:\\y", exists: true } }],
    ["spectra not a list", { ...filesResult(), spectra: {} }],
    ["files missing", { ...filesResult(), files: undefined }],
    ["files_truncated as text", { ...filesResult(), files_truncated: "no" }],
    ["warnings not texts", { ...filesResult(), warnings: [1] }],
    ["too many files", { ...filesResult(), files: Array.from({ length: MAX_FOLDER_FILES + 1 }, (_, i) => folderFile({ name: `f${i}` })) }],
    ["too many spectra", { ...filesResult(), spectra: Array.from({ length: MAX_SPECTRA + 1 }, () => spectrum()) }],
    ["file size negative", { ...filesResult(), files: [folderFile({ size: -1 })] }],
    ["file size fractional", { ...filesResult(), files: [folderFile({ size: 1.5 })] }],
    ["file name a path", { ...filesResult(), files: [folderFile({ name: "..\\x.pdf" })] }],
    ["file name with slash", { ...filesResult(), files: [folderFile({ name: "a/b.pdf" })] }],
    ["file name too long", { ...filesResult(), files: [folderFile({ name: `${"x".repeat(256)}.pdf` })] }],
    ["file without is_pdf", { ...filesResult(), files: [{ ...folderFile(), is_pdf: undefined }] }],
  ];
  it.each(bad)("rejects %s", (_name, value) => {
    expect(isCompoundFilesResult(value)).toBe(false);
  });

  const badSpectra: [string, Partial<Record<keyof SpectrumInfo, unknown>>][] = [
    ["empty full_id", { full_id: "" }],
    ["empty spectrum_key", { spectrum_key: "" }],
    ["title a number", { title: 5 }],
    ["frequency as text", { frequency_mhz: "500" }],
    ["frequency negative", { frequency_mhz: -1 }],
    ["frequency not finite", { frequency_mhz: Infinity }],
    ["dimensions fractional", { dimensions: 1.5 }],
    ["dimensions negative", { dimensions: -1 }],
    ["report not text", { report: ["x"] }],
    ["parameters not a list", { parameters: "a=b" }],
    ["parameter value a number", { parameters: [{ name: "a", value: 1 }] }],
    ["too many parameters", { parameters: Array.from({ length: MAX_SPECTRUM_PARAMETERS + 1 }, () => ({ name: "a", value: "b" })) }],
    ["pdf_available missing", { pdf_available: undefined }],
    ["pdf_file blank", { pdf_file: "" }],
    ["mnova_file a number", { mnova_file: 3 }],
    ["updated_at missing", { updated_at: undefined }],
  ];
  it.each(badSpectra)("rejects a spectrum with %s", (_name, over) => {
    expect(isSpectrumInfo({ ...spectrum(), ...over })).toBe(false);
    expect(isCompoundFilesResult(filesResult({ spectra: [{ ...spectrum(), ...over } as SpectrumInfo] }))).toBe(false);
  });

  it("the runtime client turns a malformed answer into host_bad_response", async () => {
    const client = createRuntimeHostClient(async () => ({ ok: true, result: { ...filesResult(), files: "x" } }));
    const reply = await client.request("compound_files", { core_stereo_id: "000042_01" });
    expect(reply.ok).toBe(false);
    if (!reply.ok) expect(reply.error.code).toBe("host_bad_response");
  });
});

describe("validators: read_pdf", () => {
  it("accepts a piece and an empty last piece", () => {
    expect(isReadPdfResult(piece({ data: b64(pdfBytes(10)), eof: true }))).toBe(true);
    expect(isReadPdfResult(piece({ offset: 10, data: "", eof: true }))).toBe(true);
    expect(isResultOf("read_pdf", piece({ data: "QUJD" }), 1)).toBe(true);
  });

  const bad: [string, unknown][] = [
    ["name a path", piece({ name: "C:\\x\\a.pdf" })],
    ["name empty", piece({ name: "" })],
    ["size negative", piece({ size: -1 })],
    ["size fractional", piece({ size: 1.5 })],
    ["size too large", piece({ size: MAX_PDF_BYTES + 1 })],
    ["offset negative", piece({ offset: -3 })],
    ["offset as text", { ...piece(), offset: "0" }],
    ["data not base64", piece({ data: "<html>" })],
    ["data with a bad length", piece({ data: "QUJ" })],
    ["data with padding inside", piece({ data: "QU=D" })],
    ["data too long", piece({ data: "A".repeat(Math.ceil(PDF_CHUNK_BYTES / 3) * 4 + 4) })],
    ["eof missing", { ...piece(), eof: undefined }],
    ["not an object", "x"],
  ];
  it.each(bad)("rejects %s", (_name, value) => {
    expect(isReadPdfResult(value)).toBe(false);
  });

  it("base64 and file name checks", () => {
    expect(isBase64Text("")).toBe(true);
    expect(isBase64Text("QUJDRA==")).toBe(true);
    expect(isBase64Text("QUJDRA=")).toBe(false);
    expect(isBase64Text("QUJD\n")).toBe(false);
    expect(isPlainFileName("a.pdf")).toBe(true);
    expect(isPlainFileName("..")).toBe(false);
    expect(isPlainFileName("a\u0000.pdf")).toBe(false);
  });
});

describe("validators: folder fields of register and ping", () => {
  it("reads register.folder; absent, null or malformed is null and the answer stays readable", () => {
    expect(readRegisterFolder(registerResult({ folder: { path: "C:\\R\\000042_01", created: true } }))).toEqual({
      path: "C:\\R\\000042_01",
      created: true,
    });
    expect(readRegisterFolder(registerResult())).toBeNull();
    expect(readRegisterFolder(registerResult({ folder: null }))).toBeNull();
    for (const folder of ["C:\\x", { path: "C:\\x" }, { path: 3, created: true }, { path: "", created: false }, { path: "a\nb", created: true }]) {
      const result = { ...registerResult(), folder };
      expect(readRegisterFolder(result)).toBeNull();
      expect(isRegisterResult(result)).toBe(true); // an issued ID is never dropped
    }
  });

  it("reads ping.files_folder; absent, null or malformed is null", () => {
    expect(readFilesFolder(pingResult({ files_folder: "C:\\R\\compounds" }))).toBe("C:\\R\\compounds");
    expect(readFilesFolder(pingResult())).toBeNull();
    expect(readFilesFolder(pingResult({ files_folder: null }))).toBeNull();
    expect(readFilesFolder({ ...pingResult(), files_folder: 5 })).toBeNull();
    expect(readFilesFolder({ ...pingResult(), files_folder: "  " })).toBeNull();
    expect(readFilesFolder({ ...pingResult(), files_folder: "x".repeat(5000) })).toBeNull();
    expect(readFilesFolder(null)).toBeNull();
  });
});

describe("errors", () => {
  it("names file_unavailable and keeps the host's message", () => {
    const view = presentError({ code: FILE_UNAVAILABLE, message: "No such PDF." }, "read_pdf");
    expect(view.heading).toBe("The file cannot be shown");
    expect(view.message).toBe("No such PDF.");
  });
});

// ------------------------------------------------------------------ texts

describe("spectra texts", () => {
  it("builds the heading from nucleus, frequency and solvent, else the title, else the key", () => {
    expect(spectrumHeading(spectrum())).toBe("1H NMR · 500 MHz · CDCl3");
    expect(spectrumHeading(spectrum({ nucleus: "13C", frequency_mhz: 125.76, solvent: null }))).toBe("13C NMR · 126 MHz");
    expect(spectrumHeading(spectrum({ dimensions: 2, solvent: "DMSO-d6", frequency_mhz: null }))).toBe("2D 1H NMR · DMSO-d6");
    expect(spectrumHeading(spectrum({ nucleus: null, frequency_mhz: null, solvent: null }))).toBe("ABC-001 FC2");
    expect(spectrumHeading(spectrum({ nucleus: " ", frequency_mhz: null, solvent: "", title: null }))).toBe("ABC-001 1H");
  });

  it("rounds frequencies sensibly", () => {
    expect(formatFrequency(500.13)).toBe("500 MHz");
    expect(formatFrequency(399.8)).toBe("400 MHz");
    expect(formatFrequency(60)).toBe("60 MHz");
    expect(formatFrequency(43.519)).toBe("43.52 MHz");
  });

  it("gives human-readable sizes and file lines", () => {
    expect(humanSize(0)).toBe("0 bytes");
    expect(humanSize(1)).toBe("1 byte");
    expect(humanSize(1023)).toBe("1023 bytes");
    expect(humanSize(1536)).toBe("1.5 KB");
    expect(humanSize(1024 * 1024 * 4.5)).toBe("4.5 MB");
    expect(humanSize(1024 * 300)).toBe("300 KB");
    expect(folderFileLine(folderFile({ name: "a.pdf", size: 2048 }))).toBe("a.pdf · 2 KB · 2026-11-17 14:00");
  });

  it("lists only the files no spectrum records as other files", () => {
    const r = filesResult({ files: [folderFile({ name: "a" }), folderFile({ name: "b", recorded: true })] });
    expect(otherFiles(r).map((f) => f.name)).toEqual(["a"]);
  });

  it("makes safe file names for saving, always ending in .pdf", () => {
    expect(safePdfFileName("000042_01_001_FC2.pdf")).toBe("000042_01_001_FC2.pdf");
    expect(safePdfFileName("a.PDF")).toBe("a.PDF");
    expect(safePdfFileName("spec")).toBe("spec.pdf");
    expect(safePdfFileName('a:b*c?"d<e>|f.pdf')).toBe("a_b_c__d_e__f.pdf");
    expect(safePdfFileName("..\\..\\evil.pdf")).toBe("evil.pdf");
    expect(safePdfFileName("...")).toBe("spectrum.pdf");
    expect(safePdfFileName("a\u0001b.pdf ")).toBe("a_b.pdf");
  });

  it("shows a batch's yield in the words of the panel", () => {
    expect(batchYieldText({ isolated: 76.2, analytical: null, updated_at: "x" })).toBe("76.2 % isolated");
    expect(batchYieldText({ isolated: 76.2, analytical: 78, updated_at: "x" })).toBe("76.2 % isolated · 78 % analytical");
    expect(batchYieldText({ isolated: null, analytical: 78.25, updated_at: "x" })).toBe("78.25 % analytical");
    expect(batchYieldText({ isolated: null, analytical: null, updated_at: "x" })).toBeNull();
    expect(batchYieldText(null)).toBeNull();
    expect(batchYieldText(undefined)).toBeNull();
  });
});

// ------------------------------------------------------------------ files controller

describe("FilesController", () => {
  it("loads each variant once per request and ignores stale answers", async () => {
    const client = new FakeClient();
    let changes = 0;
    const files = new FilesController(client, { onChange: () => (changes += 1) });
    files.load(["000042_01", "000042_02"]);
    expect(client.callsOf("compound_files").map((c) => c.params)).toEqual([
      { core_stereo_id: "000042_01" },
      { core_stereo_id: "000042_02" },
    ]);
    expect(files.get("000042_01")).toEqual({ loading: true, result: null, error: null });
    files.load(["000042_01"]); // refresh: the first answer is stale
    const [first, , second] = client.callsOf("compound_files");
    first!.resolve({ ok: true, result: filesResult({ warnings: ["old"] }) });
    await flush();
    expect(files.get("000042_01")!.loading).toBe(true);
    second!.resolve({ ok: true, result: filesResult({ warnings: ["new"] }) });
    await flush();
    expect(files.get("000042_01")).toMatchObject({ loading: false, error: null, result: { warnings: ["new"] } });
    expect(changes).toBeGreaterThan(2);
  });

  it("keeps the previous answer on a failed refresh, refuses an answer about another variant, and forgets closed ones", async () => {
    const client = new FakeClient();
    const files = new FilesController(client, { onChange: () => {} });
    files.load(["000042_01"]);
    client.last("compound_files").resolve({ ok: true, result: filesResult() });
    await flush();
    files.load(["000042_01"]);
    client.last("compound_files").resolve({ ok: false, error: { code: "storage_error", message: "Disk gone." } });
    await flush();
    expect(files.get("000042_01")).toMatchObject({ loading: false, error: { message: "Disk gone." }, result: { core_stereo_id: "000042_01" } });
    files.load(["000042_02"]);
    client.last("compound_files").resolve({ ok: true, result: filesResult() });
    await flush();
    expect(files.get("000042_02")!.error!.code).toBe("host_bad_response");
    files.load(["000042_03"]);
    files.retain(new Set(["000042_01"]));
    client.last("compound_files").resolve({ ok: true, result: filesResult({ core_stereo_id: "000042_03" }) });
    await flush();
    expect(files.has("000042_02")).toBe(false);
    expect(files.has("000042_03")).toBe(false);
    expect(files.has("000042_01")).toBe(true);
  });
});

// ------------------------------------------------------------------ PDF loader

const FILE: PdfSource = { kind: "file", coreStereoId: "000042_01", name: "a.pdf" };
const SPECTRUM: PdfSource = { kind: "spectrum", coreStereoId: "000042_01", fullId: "000042_01_001", spectrumKey: "ABC-001 1H" };

/** Answers every read_pdf call from `file` in pieces of `chunk` bytes. */
async function serve(client: FakeClient, file: Uint8Array, chunk: number, from = 0): Promise<void> {
  for (let i = from; ; i++) {
    await flush();
    const call = client.callsOf("read_pdf")[i];
    if (!call) return;
    const offset = (call.params as ReadPdfParams).offset ?? 0;
    const data = file.subarray(offset, Math.min(file.length, offset + chunk));
    call.resolve({
      ok: true,
      result: piece({ size: file.length, offset, data: b64(data), eof: offset + data.length >= file.length }),
    });
  }
}

describe("PDF loader", () => {
  it("asks piece by piece with the right parameters and reassembles the exact bytes, with progress", async () => {
    const client = new FakeClient();
    const file = pdfBytes(PDF_CHUNK_BYTES * 2 + 1234);
    // every byte value, to be sure the binary round trip is exact
    for (let i = 0; i < 256; i++) file[100 + i] = i;
    const progress: [number, number][] = [];
    const load = loadPdf(client, SPECTRUM, (r, s) => progress.push([r, s]));
    await serve(client, file, PDF_CHUNK_BYTES);
    const outcome = await load.done;
    expect(outcome.status).toBe("ok");
    const ok = outcome as Extract<PdfOutcome, { status: "ok" }>;
    expect(ok.name).toBe("000042_01_001_FC2.pdf");
    expect(Buffer.from(ok.bytes).equals(Buffer.from(file))).toBe(true);
    expect(client.callsOf("read_pdf").map((c) => c.params)).toEqual([
      { core_stereo_id: "000042_01", full_id: "000042_01_001", spectrum_key: "ABC-001 1H", offset: 0 },
      { core_stereo_id: "000042_01", full_id: "000042_01_001", spectrum_key: "ABC-001 1H", offset: PDF_CHUNK_BYTES },
      { core_stereo_id: "000042_01", full_id: "000042_01_001", spectrum_key: "ABC-001 1H", offset: PDF_CHUNK_BYTES * 2 },
    ]);
    expect(progress).toEqual([
      [PDF_CHUNK_BYTES, file.length],
      [PDF_CHUNK_BYTES * 2, file.length],
      [file.length, file.length],
    ]);
  });

  it("sends a file by name, never a path", async () => {
    expect(pdfParams(FILE, 7)).toEqual({ core_stereo_id: "000042_01", name: "a.pdf", offset: 7 });
    const client = new FakeClient();
    const load = loadPdf(client, FILE);
    await serve(client, pdfBytes(20), 8);
    expect((await load.done).status).toBe("ok");
    expect(client.callsOf("read_pdf").map((c) => (c.params as ReadPdfParams).offset)).toEqual([0, 8, 16]);
  });

  it("accepts an empty last piece after all bytes", async () => {
    const client = new FakeClient();
    const load = loadPdf(client, FILE);
    const file = pdfBytes(10);
    client.last("read_pdf").resolve({ ok: true, result: piece({ data: b64(file), eof: false }) });
    await flush();
    expect(client.last("read_pdf").params).toMatchObject({ offset: 10 });
    client.last("read_pdf").resolve({ ok: true, result: piece({ offset: 10, data: "", eof: true }) });
    expect((await load.done).status).toBe("ok");
  });

  async function refusal(answers: ReadPdfResult[]): Promise<string> {
    const client = new FakeClient();
    const load = loadPdf(client, FILE);
    for (const answer of answers) {
      await flush();
      client.last("read_pdf").resolve({ ok: true, result: answer });
    }
    const outcome = await load.done;
    expect(outcome.status).toBe("error");
    return (outcome as Extract<PdfOutcome, { status: "error" }>).error.message;
  }
  const ten = pdfBytes(10);

  it.each<[string, ReadPdfResult[], RegExp]>([
    ["a piece for another offset", [piece({ offset: 4, data: b64(ten.subarray(0, 4)) })], /another place/],
    ["more bytes than the file has", [piece({ size: 4, data: b64(ten), eof: true })], /more bytes/],
    ["an empty piece before the end", [piece({ data: "" })], /empty piece/],
    ["an end before all bytes", [piece({ data: b64(ten.subarray(0, 5)), eof: true })], /ended early/],
    ["a piece larger than allowed", [piece({ size: PDF_CHUNK_BYTES + 10, data: b64(new Uint8Array(PDF_CHUNK_BYTES + 3)) })], /larger than allowed/],
    ["a file that is not a PDF", [piece({ data: b64(new Uint8Array(10)), eof: true })], /not a PDF/],
    ["a file larger than allowed", [piece({ size: MAX_PDF_BYTES + 1 })], /larger than 100 MB/],
    ["an empty file", [piece({ size: 0, eof: true })], /empty/],
    ["unreadable base64", [piece({ data: "@@@@" })], /not readable/],
  ])("refuses %s", async (_name, answers, message) => {
    expect(await refusal(answers)).toMatch(message);
  });

  it("passes the host's error on unchanged and asks nothing more", async () => {
    const client = new FakeClient();
    const load = loadPdf(client, FILE);
    client.last("read_pdf").resolve({ ok: false, error: { code: FILE_UNAVAILABLE, message: "The PDF a.pdf is not in the folder." } });
    expect(await load.done).toEqual({ status: "error", error: { code: FILE_UNAVAILABLE, message: "The PDF a.pdf is not in the folder." } });
    await flush();
    expect(client.callsOf("read_pdf")).toHaveLength(1);
  });

  it("starts again once when the size changes, then gives up", async () => {
    const client = new FakeClient();
    const load = loadPdf(client, FILE);
    const old = pdfBytes(20);
    const grown = pdfBytes(30);
    client.last("read_pdf").resolve({ ok: true, result: piece({ size: 20, data: b64(old.subarray(0, 8)) }) });
    await flush();
    client.last("read_pdf").resolve({ ok: true, result: piece({ size: 30, offset: 8, data: b64(grown.subarray(8, 16)) }) });
    await flush();
    expect(client.last("read_pdf").params).toMatchObject({ offset: 0 }); // started again
    await serve(client, grown, 16, 2);
    const outcome = await load.done;
    expect(outcome.status).toBe("ok");
    expect(Buffer.from((outcome as Extract<PdfOutcome, { status: "ok" }>).bytes).equals(Buffer.from(grown))).toBe(true);

    const client2 = new FakeClient();
    const load2 = loadPdf(client2, FILE);
    let size = 20;
    for (let i = 0; i < 4; i++) {
      await flush();
      const offset = (client2.last("read_pdf").params as ReadPdfParams).offset ?? 0;
      const file = pdfBytes(size);
      client2.last("read_pdf").resolve({ ok: true, result: piece({ size, offset, data: b64(file.subarray(offset, offset + 8)) }) });
      size += 1; // the file grows between every two answers
    }
    const outcome2 = await load2.done;
    expect(outcome2.status).toBe("error");
    expect((outcome2 as Extract<PdfOutcome, { status: "error" }>).error.message).toMatch(/kept changing/);
    expect(client2.callsOf("read_pdf")).toHaveLength(4);
  });

  it("cancel: no further request, late answers ignored, outcome cancelled", async () => {
    const client = new FakeClient();
    const progress: number[] = [];
    const load = loadPdf(client, FILE, (r) => progress.push(r));
    const file = pdfBytes(20);
    client.last("read_pdf").resolve({ ok: true, result: piece({ size: 20, data: b64(file.subarray(0, 8)) }) });
    await flush();
    expect(client.callsOf("read_pdf")).toHaveLength(2);
    load.cancel();
    expect(await load.done).toEqual({ status: "cancelled" });
    client.last("read_pdf").resolve({ ok: true, result: piece({ size: 20, offset: 8, data: b64(file.subarray(8, 16)) }) });
    await flush();
    expect(client.callsOf("read_pdf")).toHaveLength(2);
    expect(progress).toEqual([8]);
  });

  it("progress text", () => {
    expect(progressText(0, null)).toBe("Loading the PDF…");
    expect(progressText(40, 100)).toBe("Loading the PDF… 40 %");
    expect(progressText(999, 1000)).toBe("Loading the PDF… 99 %");
    expect(progressText(10, 10)).toBe("Loading the PDF… 100 %");
  });

  it("decodes base64 to bytes", () => {
    expect(Array.from(decodeBase64("AAH/")!)).toEqual([0, 1, 255]);
    expect(decodeBase64("***")).toBeNull();
  });
});
