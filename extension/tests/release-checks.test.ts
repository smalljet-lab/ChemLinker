import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { SITE_OPS } from "../src/background/message-handler";
import { MAX_SET_REACTION_BYTES } from "../src/content/reaction";
import * as protocol from "../src/shared/protocol";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const project = join(root, "..");
const read = (...parts: string[]) => readFileSync(join(...parts), "utf8");

describe("one version number", () => {
  const pkg = JSON.parse(read(root, "package.json"));
  const lock = JSON.parse(read(root, "package-lock.json"));
  const manifest = JSON.parse(read(root, "static", "manifest.json"));

  it("is the same in package.json, package-lock.json and static/manifest.json", () => {
    expect(pkg.version).toMatch(/^\d+\.\d+\.\d+$/);
    expect(lock.version).toBe(pkg.version);
    expect(lock.packages[""].version).toBe(pkg.version);
    expect(manifest.version).toBe(pkg.version);
  });

  it("is the registry program's __version__ (scripts/set-version.py sets all of them)", () => {
    const init = read(project, "src", "compound_registry", "__init__.py");
    const found = /^__version__\s*=\s*["']([^"']+)["']/m.exec(init);
    expect(found, "__version__ in src/compound_registry/__init__.py").not.toBeNull();
    expect(found![1]).toBe(pkg.version);
  });
});

describe("package.json", () => {
  const pkg = JSON.parse(read(root, "package.json"));
  const lock = JSON.parse(read(root, "package-lock.json"));

  it("names both browsers, the licence of the project and the Node.js versions the build tools need", () => {
    expect(pkg.description).toBe(
      "Browser extension (Firefox and Chrome): local compound ID registry panel for the Sciformation ELN",
    );
    expect(pkg.license).toBe("MIT");
    expect(read(project, "LICENSE")).toMatch(/^MIT License/);
    expect(pkg.engines).toEqual({ node: "^20.19.0 || >=22.12.0" });
    expect(lock.packages[""].license).toBe(pkg.license);
    expect(lock.packages[""].engines).toEqual(pkg.engines);
  });

  it("has the build and package scripts for both browsers", () => {
    expect(pkg.scripts).toMatchObject({
      build: "node build.mjs",
      "build:chrome": "node build.mjs --chrome",
      package: "node scripts/package.mjs",
      "package:chrome": "node scripts/package.mjs --chrome",
    });
  });
});

describe("constants mirrored from src/compound_registry/protocol.py", () => {
  const python = read(project, "src", "compound_registry", "protocol.py");

  /** A Python literal of the kinds the limits use: a string, a number, or a product of whole numbers. */
  function literal(text: string): string | number | undefined {
    const t = text.trim();
    const quoted = /^(["'])([^"'\\]*)\1$/.exec(t);
    if (quoted) return quoted[2];
    if (/^\d[\d_]*(\s*\*\s*\d[\d_]*)*$/.test(t)) {
      return t.split("*").reduce((product, factor) => product * Number(factor.trim().replaceAll("_", "")), 1);
    }
    if (/^\d+(\.\d+)?e\d+$/i.test(t)) return Number(t);
    return undefined;
  }

  /** Module-level `NAME = <literal>` assignments (a trailing comment allowed). */
  const assigned = new Map<string, string | number>();
  for (const line of python.split(/\r?\n/)) {
    const found = /^([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*(?:#.*)?$/.exec(line);
    if (!found) continue;
    const value = literal(found[2]!);
    if (value !== undefined) assigned.set(found[1]!, value);
  }

  /** A module-level tuple of strings, e.g. OPS. */
  function tuple(name: string): string[] {
    const found = new RegExp(`^${name}\\s*=\\s*\\(([^)]*)\\)`, "m").exec(python);
    expect(found, `${name} in protocol.py`).not.toBeNull();
    return [...found![1]!.matchAll(/["']([^"']+)["']/g)].map((m) => m[1]!);
  }

  const mirrored = Object.entries(protocol).filter(
    ([name, value]) => /^[A-Z][A-Z0-9_]*$/.test(name) && (typeof value === "number" || typeof value === "string") && assigned.has(name),
  );

  it("reads protocol.py", () => {
    expect(assigned.get("PROTOCOL_VERSION")).toBe(1);
    expect(assigned.get("MAX_PDF_BYTES")).toBe(100 * 1024 * 1024);
    expect(assigned.get("MAX_REACTION_NUMBER")).toBe(1e12);
    expect(assigned.get("HOST_NAME")).toBe("compound_registry");
  });

  it("covers every constant protocol.ts says it keeps in step", () => {
    expect(mirrored.map(([name]) => name).sort()).toEqual(
      [
        "CHROME_EXTENSION_ID",
        "DEFAULT_PAGE_SIZE",
        "DEFAULT_YIELD_PAGE_SIZE",
        "EXTENSION_ID",
        "HOST_NAME",
        "MAX_CHEMDRAW_CONDITION_LINES",
        "MAX_CHEMDRAW_STEPS",
        "MAX_CHEMDRAW_STEP_MOLECULES",
        "MAX_DESCRIBE_STRUCTURES",
        "MAX_FILE_NAME",
        "MAX_FOLDER_FILES",
        "MAX_IMAGE_BASE64_CHARS",
        "MAX_IMAGE_SIDE",
        "MAX_NAME_TEXT",
        "MAX_PAGE_SIZE",
        "MAX_PDF_BYTES",
        "MAX_PRODUCT_INDEX",
        "MAX_REACTION_NUMBER",
        "MAX_REACTION_ROWS",
        "MAX_REACTION_TEXT",
        "MAX_REASON_TEXT",
        "MAX_RECOGNITION_MOLECULES",
        "MAX_REF_TEXT",
        "MAX_ROUTE_CDXML_CHARS",
        "MAX_SEARCH_TEXT",
        "MAX_STRUCTURE_CHARS",
        "MAX_YIELD_PAGE_SIZE",
        "MAX_YIELD_PERCENT",
        "MIN_IMAGE_SIDE",
        "PDF_CHUNK_BYTES",
        "PROTOCOL_VERSION",
      ].sort(),
    );
  });

  it.each(mirrored)("%s has the same value on both sides", (name, value) => {
    expect(value).toBe(assigned.get(name));
  });

  it("the extension sends only operations the host knows, and never retire", () => {
    const ops = tuple("OPS");
    expect(ops).toContain("ping");
    for (const op of [...SITE_OPS, ...protocol.PAGE_OPS, ...protocol.CAPTURE_OPS]) expect(ops, op).toContain(op);
    expect(ops).toContain("retire");
    expect([...SITE_OPS, ...protocol.PAGE_OPS, ...protocol.CAPTURE_OPS]).not.toContain("retire");
  });

  it("the capture page's operations are exactly the ones the host serves without a registry", () => {
    expect([...protocol.CAPTURE_OPS]).toEqual(["recognition_status", "recognize", "describe_structures", "reaction_to_chemdraw"]);
    expect(tuple("CAPTURE_OPS")).toEqual([...protocol.CAPTURE_OPS]);
    // No other part of the extension may send them.
    for (const op of protocol.CAPTURE_OPS) {
      expect(SITE_OPS, op).not.toContain(op);
      expect(protocol.PAGE_OPS, op).not.toContain(op);
    }
  });

  it("the lists of recognition modes, states and warning codes are the host's", () => {
    expect(tuple("RECOGNITION_MODES")).toEqual([...protocol.RECOGNITION_MODES]);
    expect(tuple("RECOGNITION_STATES")).toEqual([...protocol.RECOGNITION_STATES]);
    expect(tuple("MOLECULE_WARNING_CODES")).toEqual([...protocol.MOLECULE_WARNING_CODES]);
    expect(tuple("STRUCTURE_WARNING_CODES")).toEqual([...protocol.STRUCTURE_WARNING_CODES]);
    expect(tuple("RECOGNITION_WARNING_CODES")).toEqual([...protocol.RECOGNITION_WARNING_CODES]);
    expect([...protocol.MOLECULE_WARNING_CODES]).toEqual([
      "unparsable", "placeholder", "isotope", "unusual_element", "stereo", "charged_or_salt", "low_confidence", "duplicate",
      "smiles_molfile_differ",
    ]);
    for (const code of protocol.STRUCTURE_WARNING_CODES) expect(protocol.MOLECULE_WARNING_CODES, code).toContain(code);
  });

  it("the largest recognize request stays under the host's frame limit", () => {
    const request: protocol.HostRequest<"recognize"> = {
      id: "x".repeat(64),
      v: protocol.PROTOCOL_VERSION,
      op: "recognize",
      params: { image: "A".repeat(protocol.MAX_IMAGE_BASE64_CHARS), mode: "molecule" },
    };
    expect(JSON.stringify(request).length).toBeLessThan(assigned.get("MAX_REQUEST_BYTES") as number);
  });

  it("the time the extension waits for a recognition is longer than the host takes at most", () => {
    expect(assigned.get("RECOGNIZE_TIMEOUT_S")).toBe(270);
    expect(protocol.RECOGNIZE_TIMEOUT_MS).toBeGreaterThan((assigned.get("RECOGNIZE_TIMEOUT_S") as number) * 1000);
  });

  it("the error codes the extension reacts to are the host's", () => {
    const hostCodes = tuple("HOST_ERROR_CODES");
    expect(hostCodes).toContain(protocol.REGISTRY_CHANGED);
    expect(hostCodes).toContain(protocol.REGISTRY_MISSING);
    for (const code of [protocol.RECOGNITION_NOT_INSTALLED, protocol.RECOGNITION_FAILED, protocol.IMAGE_INVALID]) {
      expect(hostCodes, code).toContain(code);
    }
    for (const code of [protocol.YIELD_MISMATCH, protocol.FILE_UNAVAILABLE]) expect(python).toContain(`\`\`${code}\`\``);
  });

  it("the largest set_reaction request stays under the host's frame limit", () => {
    expect(assigned.get("MAX_REQUEST_BYTES")).toBe(1024 * 1024);
    expect(MAX_SET_REACTION_BYTES).toBeLessThan(assigned.get("MAX_REQUEST_BYTES") as number);
  });

  it("the time the extension waits for a name is longer than the host gives the naming program", () => {
    expect(protocol.GENERATE_NAME_TIMEOUT_MS).toBeGreaterThan((assigned.get("NAME_GENERATION_TIMEOUT_S") as number) * 1000);
  });
});
