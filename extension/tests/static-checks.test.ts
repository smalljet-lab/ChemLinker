import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { inflateRawSync, crc32 } from "node:zlib";
import { describe, expect, it } from "vitest";
import { COMPOUNDS_PAGE, EXTENSION_ID, SITE_ORIGIN } from "../src/shared/protocol";
import { checkEntryName, createZip } from "../scripts/zip.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
}

describe("manifest", () => {
  const manifest = JSON.parse(readFileSync(join(root, "static", "manifest.json"), "utf8"));
  const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

  it("is MV3 with the contract's add-on id", () => {
    expect(manifest.manifest_version).toBe(3);
    expect(manifest.browser_specific_settings.gecko.id).toBe(EXTENSION_ID);
    expect(manifest.browser_specific_settings.gecko.strict_min_version).toBe("140.0");
    expect(manifest.browser_specific_settings.gecko.data_collection_permissions).toEqual({ required: ["none"] });
    expect(manifest.version).toBe(pkg.version);
  });

  it("asks for exactly nativeMessaging and the Sciformation host", () => {
    expect(manifest.permissions).toEqual(["nativeMessaging"]);
    expect(manifest.host_permissions).toEqual([`${SITE_ORIGIN}/*`]);
    expect(manifest.optional_permissions).toBeUndefined();
    expect(manifest.optional_host_permissions).toBeUndefined();
  });

  it("exposes nothing to web pages and keeps the default CSP", () => {
    expect(manifest.web_accessible_resources).toBeUndefined();
    expect(manifest.content_security_policy).toBeUndefined();
    expect(manifest.externally_connectable).toBeUndefined();
  });

  it("has a toolbar button without a popup that opens the compounds page", () => {
    expect(manifest.action).toEqual({
      default_title: "All compounds (Compound Registry)",
      default_icon: "compounds-icon.svg",
      default_area: "navbar",
    });
    expect(manifest.action.default_popup).toBeUndefined();
    expect(manifest.browser_action).toBeUndefined();
    expect(readFileSync(join(root, "static", manifest.action.default_icon), "utf8")).toMatch(/^<svg /);
  });

  it("pins openchemlib to one exact version, and it is the only runtime dependency", () => {
    expect(Object.keys(pkg.dependencies)).toEqual(["openchemlib"]);
    expect(pkg.dependencies.openchemlib).toMatch(/^\d+\.\d+\.\d+$/);
    const lock = JSON.parse(readFileSync(join(root, "package-lock.json"), "utf8"));
    expect(lock.version).toBe(pkg.version);
    expect(lock.packages["node_modules/openchemlib"].version).toBe(pkg.dependencies.openchemlib);
  });

  it("uses an event page, the page bridge in the MAIN world first, then the isolated script, in all frames", () => {
    expect(manifest.background).toEqual({ scripts: ["background.js"] });
    expect(manifest.content_scripts).toEqual([
      {
        matches: [`${SITE_ORIGIN}/*`],
        js: ["page-bridge.js"],
        run_at: "document_idle",
        all_frames: true,
        world: "MAIN",
      },
      {
        matches: [`${SITE_ORIGIN}/*`],
        js: ["content.js"],
        run_at: "document_idle",
        all_frames: true,
      },
    ]);
  });
});

describe("source scan", () => {
  const forbidden = [
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "fetch(",
    "XMLHttpRequest",
    "WebSocket",
    "sendBeacon",
    "importScripts",
    "localStorage",
    "sessionStorage",
    "adoptedStyleSheets",
    "DOMParser",
    "createContextualFragment",
    "browser.storage",
    "document.cookie",
  ];
  const files = walk(join(root, "src")).filter((f) => /\.(ts|js|mjs)$/.test(f));

  it("finds the sources", () => {
    expect(files.length).toBeGreaterThan(10);
  });

  /** The page bridge is the only file allowed to call fetch (same-origin molfile download). */
  const FETCH_ALLOWED = join("src", "page", "bridge.ts");

  it.each(forbidden)("no file under src/ contains %s", (word) => {
    const hits = files
      .filter((f) => readFileSync(f, "utf8").includes(word))
      .map((f) => relative(root, f))
      .filter((f) => !(word === "fetch(" && f === FETCH_ALLOWED));
    expect(hits).toEqual([]);
  });

  it("the page bridge calls fetch exactly once, same-origin only", () => {
    const text = readFileSync(join(root, FETCH_ALLOWED), "utf8");
    const calls = text.split("fetch(").length - 1;
    expect(calls).toBe(1);
    expect(text).toMatch(/win\.fetch\(downloadUrl, \{\s*credentials: "same-origin",\s*mode: "same-origin",/);
  });

  it("only the page bridge touches the ZK download command", () => {
    const hits = files.filter((f) => readFileSync(f, "utf8").includes("cmd0")).map((f) => relative(root, f));
    expect(hits).toEqual([FETCH_ALLOWED]);
  });

  it("loads nothing remote", () => {
    for (const f of files) {
      const text = readFileSync(f, "utf8");
      expect(text, f).not.toMatch(/import\s*\(/);
      expect(text, f).not.toMatch(/\bsrc\s*=\s*["']https?:/);
    }
  });
});

describe("compounds page", () => {
  const html = readFileSync(join(root, "static", COMPOUNDS_PAGE), "utf8");

  it("loads exactly its own script and stylesheet, nothing inline, nothing remote", () => {
    expect(COMPOUNDS_PAGE).toBe("compounds.html");
    const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
    expect(scripts.map((m) => m[1]!.trim())).toEqual(['src="compounds.js"']);
    expect(scripts.every((m) => m[2]!.trim() === "")).toBe(true);
    expect([...html.matchAll(/<link\b[^>]*>/gi)].map((m) => m[0])).toEqual(['<link rel="stylesheet" href="compounds.css">']);
    expect(html).not.toMatch(/\son[a-z]+\s*=/i); // no inline event handlers
    expect(html).not.toMatch(/javascript:/i);
    expect(html).not.toMatch(/https?:/i);
    expect(html).not.toMatch(/<(iframe|object|embed|base|meta\s+http-equiv)\b/i);
    expect(html).toContain('<div id="app"');
  });

  it("its stylesheet loads nothing remote", () => {
    const css = readFileSync(join(root, "static", "compounds.css"), "utf8");
    expect(css).not.toMatch(/@import|url\(|https?:/i);
  });

  it("build.mjs bundles the page and copies every static file it and the manifest use", () => {
    const build = readFileSync(join(root, "build.mjs"), "utf8");
    expect(build).toContain('["src", "compounds", "index.ts"]');
    expect(build).toContain('"compounds.js"');
    for (const name of ["compounds.html", "compounds.css", "compounds-icon.svg"]) {
      expect(build).toContain(`"${name}"`);
      expect(statSync(join(root, "static", name)).isFile()).toBe(true);
    }
  });
});

describe("openchemlib stays in the compounds page", () => {
  /** Every bare module specifier reachable from an entry through relative imports. */
  function packagesReachable(entry: string): { packages: Set<string>; files: Set<string> } {
    const packages = new Set<string>();
    const files = new Set<string>();
    const queue = [join(root, entry)];
    while (queue.length > 0) {
      const file = queue.pop()!;
      if (files.has(file)) continue;
      files.add(file);
      const text = readFileSync(file, "utf8");
      for (const m of text.matchAll(/(?:^|\n)\s*(?:import|export)\s[^;]*?from\s+["']([^"']+)["']|(?:^|\n)\s*import\s+["']([^"']+)["']/g)) {
        const spec = m[1] ?? m[2]!;
        if (spec.startsWith(".")) {
          const base = join(dirname(file), spec);
          queue.push(base.endsWith(".ts") ? base : `${base}.ts`);
        } else {
          packages.add(spec);
        }
      }
    }
    return { packages, files };
  }

  it.each([
    ["src/background/index.ts"],
    ["src/content/index.ts"],
    ["src/page/index.ts"],
  ])("%s does not reach openchemlib", (entry) => {
    const { packages, files } = packagesReachable(entry);
    expect([...packages]).toEqual([]);
    expect([...files].some((f) => f.includes(join("src", "compounds")))).toBe(false);
  });

  it("the compounds page reaches it, through ocl-editor.ts only", () => {
    const { packages, files } = packagesReachable("src/compounds/index.ts");
    expect([...packages]).toEqual(["openchemlib"]);
    const importers = [...files].filter((f) => /from\s+["']openchemlib["']/.test(readFileSync(f, "utf8")));
    expect(importers.map((f) => relative(root, f))).toEqual([join("src", "compounds", "ocl-editor.ts")]);
    const all = walk(join(root, "src")).filter((f) => /from\s+["']openchemlib["']/.test(readFileSync(f, "utf8")));
    expect(all.map((f) => relative(root, f))).toEqual([join("src", "compounds", "ocl-editor.ts")]);
  });
});

describe("zip writer", () => {
  function readZip(buf: Buffer) {
    const end = buf.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
    const count = buf.readUInt16LE(end + 10);
    let p = buf.readUInt32LE(end + 16);
    const out: { name: string; data: Buffer }[] = [];
    for (let i = 0; i < count; i++) {
      expect(buf.readUInt32LE(p)).toBe(0x02014b50);
      const method = buf.readUInt16LE(p + 10);
      const crc = buf.readUInt32LE(p + 16);
      const csize = buf.readUInt32LE(p + 20);
      const usize = buf.readUInt32LE(p + 24);
      const nlen = buf.readUInt16LE(p + 28);
      const local = buf.readUInt32LE(p + 42);
      const name = buf.subarray(p + 46, p + 46 + nlen).toString("utf8");
      expect(buf.readUInt32LE(local)).toBe(0x04034b50);
      const lnlen = buf.readUInt16LE(local + 26);
      const elen = buf.readUInt16LE(local + 28);
      const body = buf.subarray(local + 30 + lnlen + elen, local + 30 + lnlen + elen + csize);
      const data = method === 8 ? inflateRawSync(body) : Buffer.from(body);
      expect(data.length).toBe(usize);
      expect(crc32(data) >>> 0).toBe(crc);
      out.push({ name, data });
      p += 46 + nlen;
    }
    return out;
  }

  it("round-trips stored and deflated entries", () => {
    const entries = [
      { name: "manifest.json", data: Buffer.from(JSON.stringify({ a: "x".repeat(500) })) },
      { name: "a/b.js", data: Buffer.from("x") },
      { name: "empty.txt", data: Buffer.alloc(0) },
    ];
    const zip = createZip(entries);
    const back = readZip(zip);
    expect(back.map((e) => e.name)).toEqual(["manifest.json", "a/b.js", "empty.txt"]);
    for (let i = 0; i < entries.length; i++) expect(back[i]!.data.equals(entries[i]!.data)).toBe(true);
  });

  it.each(["a\\b.js", "/abs.js", "C:/x.js", "../x.js", "a//b.js", ""])("refuses entry name %j", (name) => {
    expect(() => checkEntryName(name)).toThrow();
  });
});
