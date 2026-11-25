import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { inflateRawSync, crc32 } from "node:zlib";
import { describe, expect, it } from "vitest";
import { CAPTURE_PAGE, COMPOUNDS_PAGE, EXTENSION_ID, POPUP_PAGE, SITE_ORIGIN } from "../src/shared/protocol";
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

  it("asks for exactly nativeMessaging, activeTab and the Sciformation host", () => {
    // activeTab: the screenshot of the tab the user starts "Capture reaction" on, and nothing else.
    // No "tabs", no "scripting", no "<all_urls>", no "storage", no "clipboardWrite".
    expect(manifest.permissions).toEqual(["nativeMessaging", "activeTab"]);
    expect(manifest.host_permissions).toEqual([`${SITE_ORIGIN}/*`]);
    expect(manifest.optional_permissions).toBeUndefined();
    expect(manifest.optional_host_permissions).toBeUndefined();
  });

  it("exposes nothing to web pages and keeps the default CSP", () => {
    expect(manifest.web_accessible_resources).toBeUndefined();
    expect(manifest.content_security_policy).toBeUndefined();
    expect(manifest.externally_connectable).toBeUndefined();
  });

  it("has a toolbar button that opens the popup, and one keyboard shortcut for the capture", () => {
    expect(manifest.action).toEqual({
      default_title: "ChemLinker",
      default_icon: "compounds-icon.svg",
      default_area: "navbar",
      default_popup: POPUP_PAGE,
    });
    expect(POPUP_PAGE).toBe("popup.html");
    expect(manifest.browser_action).toBeUndefined();
    expect(manifest.commands).toEqual({
      "capture-reaction": {
        suggested_key: { default: "Alt+Shift+R" },
        description: "Capture reaction: read a drawn reaction from the current tab",
      },
    });
    expect(readFileSync(join(root, "src", "background", "capture.ts"), "utf8")).toContain('CAPTURE_COMMAND = "capture-reaction"');
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

  it("the capture page and the popup log nothing (a screenshot, a structure or read text must never reach a log)", () => {
    const hits = files
      .filter((f) => relative(root, f).startsWith(join("src", "capture")) || relative(root, f).startsWith(join("src", "popup")))
      .filter((f) => /\bconsole\s*\./.test(readFileSync(f, "utf8")))
      .map((f) => relative(root, f));
    expect(hits).toEqual([]);
    expect(readFileSync(join(root, "src", "background", "capture.ts"), "utf8")).not.toMatch(/\bconsole\s*\.|\blog\(/);
  });

  it("only the background takes screenshots, and no script is ever injected into a page", () => {
    const using = (word: string) => files.filter((f) => readFileSync(f, "utf8").includes(word)).map((f) => relative(root, f));
    expect(using("captureVisibleTab(")).toEqual([join("src", "background", "capture.ts")]);
    expect(using("executeScript")).toEqual([]);
    expect(using("browser.scripting")).toEqual([]);
    expect(using("insertCSS")).toEqual([]);
  });

  /**
   * "Who may send what" (message-handler.ts) only holds while every page goes
   * through the background. The extension's pages have the nativeMessaging
   * permission themselves: a page that opened its own port to the host, or
   * used the tabs API, would skip every sender check. So the only way out of
   * the capture page and the popup is `browser.runtime.sendMessage`, and the
   * host is connected in one place.
   */
  it("only the background's entry connects to the native host", () => {
    const using = (pattern: RegExp) => files.filter((f) => pattern.test(readFileSync(f, "utf8"))).map((f) => relative(root, f));
    // The call; shared/protocol.ts names the function in a comment on HOST_NAME.
    expect(using(/connectNative\s*\(/)).toEqual([join("src", "background", "index.ts")]);
    expect(using(/connectNative/).sort()).toEqual([join("src", "background", "index.ts"), join("src", "shared", "protocol.ts")].sort());
    expect(using(/sendNativeMessage/)).toEqual([]);
    expect(using(/captureVisibleTab/)).toEqual([join("src", "background", "capture.ts")]);
  });

  it("the capture page and the popup use no extension API but runtime.sendMessage", () => {
    const pageFiles = files.filter((f) => {
      const rel = relative(root, f);
      return rel.startsWith(join("src", "capture")) || rel.startsWith(join("src", "popup"));
    });
    expect(pageFiles.length).toBeGreaterThan(5);
    const uses: string[] = [];
    for (const f of pageFiles) {
      const text = readFileSync(f, "utf8");
      // Every mention of the API object, in code and in comments, with what follows it.
      for (const match of text.matchAll(/\b(?:browser|chrome)\b\s*(?:\??\.\s*[A-Za-z_$][\w$]*|\[[^\]]*\])(?:\s*\??\.\s*[A-Za-z_$][\w$]*)*/g)) {
        uses.push(match[0].replace(/\s+/g, ""));
      }
      // No other way to the same objects.
      expect(text, f).not.toMatch(/\bglobalThis\b|\bwindow\s*\[|\bself\s*\[/);
      expect(text, f).not.toMatch(/\b(?:tabs|windows|scripting|nativeMessaging|permissions|downloads|webRequest|cookies|history)\s*\??\.\s*[a-z]/);
    }
    expect([...new Set(uses)]).toEqual(["browser.runtime.sendMessage"]);
    // Both entries do call it, each exactly once.
    for (const entry of [join("src", "capture", "index.ts"), join("src", "popup", "index.ts")]) {
      const text = readFileSync(join(root, entry), "utf8");
      expect(text.split("browser.runtime.sendMessage(").length - 1, entry).toBe(1);
    }
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

describe("the extension's own pages", () => {
  /** [page, its script, its stylesheet, the folder of its sources] */
  const PAGES: [string, string, string, string][] = [
    [COMPOUNDS_PAGE, "compounds.js", "compounds.css", "compounds"],
    [CAPTURE_PAGE, "capture.js", "capture.css", "capture"],
    [POPUP_PAGE, "popup.js", "popup.css", "popup"],
  ];

  it("are exactly compounds.html, capture.html and popup.html", () => {
    expect(PAGES.map((p) => p[0])).toEqual(["compounds.html", "capture.html", "popup.html"]);
    const htmlFiles = readdirSync(join(root, "static")).filter((name) => name.endsWith(".html")).sort();
    expect(htmlFiles).toEqual(["capture.html", "compounds.html", "popup.html"]);
    const cssFiles = readdirSync(join(root, "static")).filter((name) => name.endsWith(".css")).sort();
    expect(cssFiles).toEqual(["capture.css", "compounds.css", "popup.css"]);
  });

  it.each(PAGES)("%s loads exactly its own script and stylesheet, nothing inline, nothing remote", (page, script, css) => {
    const html = readFileSync(join(root, "static", page), "utf8");
    const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
    expect(scripts.map((m) => m[1]!.trim())).toEqual([`src="${script}"`]);
    expect(scripts.every((m) => m[2]!.trim() === "")).toBe(true);
    expect([...html.matchAll(/<link\b[^>]*>/gi)].map((m) => m[0])).toEqual([`<link rel="stylesheet" href="${css}">`]);
    expect(html).not.toMatch(/<style\b|\sstyle\s*=/i); // no inline styles
    expect(html).not.toMatch(/\son[a-z]+\s*=/i); // no inline event handlers
    expect(html).not.toMatch(/javascript:/i);
    expect(html).not.toMatch(/https?:/i);
    expect(html).not.toMatch(/<(iframe|object|embed|base|meta\s+http-equiv)\b/i);
    expect(html).toContain('<div id="app"');
  });

  it.each(PAGES)("the stylesheet of %s loads nothing remote", (_page, _script, css) => {
    expect(readFileSync(join(root, "static", css), "utf8")).not.toMatch(/@import|url\(|https?:/i);
  });

  it("build.mjs bundles every page and copies every static file they and the manifest use", () => {
    const build = readFileSync(join(root, "build.mjs"), "utf8");
    for (const [page, script, css, folder] of PAGES) {
      expect(build).toContain(`["src", "${folder}", "index.ts"]`);
      for (const name of [page, script, css]) expect(build).toContain(`"${name}"`);
      expect(statSync(join(root, "static", page)).isFile()).toBe(true);
      expect(statSync(join(root, "static", css)).isFile()).toBe(true);
    }
    expect(build).toContain('"compounds-icon.svg"');
    expect(build).toContain('const BUNDLES = ["background.js", "content.js", "page-bridge.js", "compounds.js", "capture.js", "popup.js"];');
    // Every bundle call: only the compounds page and the capture page may hold openchemlib (last argument true).
    const calls = [...build.matchAll(/await bundle\(\s*\["src", "(\w+)", "index\.ts"\],\s*"([\w-]+\.js)",[\s\S]*?\);\n/g)];
    expect(calls.map((m) => [m[1], m[2], /,\s*true,?\s*\);\n$/.test(m[0])])).toEqual([
      ["background", "background.js", false],
      ["content", "content.js", false],
      ["page", "page-bridge.js", false],
      ["compounds", "compounds.js", true],
      ["capture", "capture.js", true],
      ["popup", "popup.js", false],
    ]);
  });
});

describe("openchemlib stays in the compounds page and the capture page", () => {
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
    ["src/popup/index.ts"],
  ])("%s does not reach openchemlib", (entry) => {
    const { packages, files } = packagesReachable(entry);
    expect([...packages]).toEqual([]);
    expect([...files].some((f) => f.includes(join("src", "compounds")))).toBe(false);
    expect([...files].some((f) => f.includes(join("src", "capture")))).toBe(false);
  });

  it.each([["src/compounds/index.ts"], ["src/capture/index.ts"]])("%s reaches it, through ocl-editor.ts only", (entry) => {
    const { packages, files } = packagesReachable(entry);
    expect([...packages]).toEqual(["openchemlib"]);
    const importers = [...files].filter((f) => /from\s+["']openchemlib["']/.test(readFileSync(f, "utf8")));
    expect(importers.map((f) => relative(root, f))).toEqual([join("src", "compounds", "ocl-editor.ts")]);
  });

  it("only ocl-editor.ts imports openchemlib, and only the two page entries import ocl-editor.ts", () => {
    const sources = walk(join(root, "src"));
    const all = sources.filter((f) => /from\s+["']openchemlib["']/.test(readFileSync(f, "utf8")));
    expect(all.map((f) => relative(root, f))).toEqual([join("src", "compounds", "ocl-editor.ts")]);
    const users = sources.filter((f) => /from\s+["'][^"']*ocl-editor["']/.test(readFileSync(f, "utf8")));
    expect(users.map((f) => relative(root, f)).sort()).toEqual(
      [join("src", "capture", "index.ts"), join("src", "compounds", "index.ts")].sort(),
    );
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
