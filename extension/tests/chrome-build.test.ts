import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { CHROME_EXTENSION_ID, COMPOUNDS_PAGE, EXTENSION_ID, PAGE_OPS, SITE_ORIGIN } from "../src/shared/protocol";
import { chromeExtensionIdFromKey, chromeManifest, iconMap, MINIMUM_CHROME_VERSION } from "../scripts/chrome-manifest.mjs";
import { DEFAULT_ELN_ORIGIN } from "../scripts/eln-origin.mjs";
import { ICON_SIZES, iconPath, iconPng } from "../scripts/make-icons.mjs";
import { packageExtension } from "../scripts/package.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const firefoxManifestText = readFileSync(join(root, "static", "manifest.json"), "utf8");
const firefoxManifest = JSON.parse(firefoxManifestText);
const chromeKey = JSON.parse(readFileSync(join(root, "static", "chrome-key.json"), "utf8"));

/** A `browser` identifier (not part of a longer word). */
const BROWSER_WORD = /\bbrowser\b/;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
}

function filesUnder(dir: string): string[] {
  return walk(dir).map((f) => relative(dir, f).split(sep).join("/")).sort();
}

/** The build for the default ELN address, whatever this computer's environment or local.config.json say. */
function runBuild(...args: string[]) {
  return spawnSync(process.execPath, [join(root, "build.mjs"), "--eln-origin", DEFAULT_ELN_ORIGIN, ...args], {
    cwd: root,
    encoding: "utf8",
  });
}

describe("Chrome manifest transform", () => {
  const chrome = chromeManifest(firefoxManifest, chromeKey);

  it("has exactly the Chrome keys, nothing added beyond key, icons and minimum_chrome_version", () => {
    expect(Object.keys(chrome)).toEqual([
      "manifest_version",
      "name",
      "version",
      "description",
      "minimum_chrome_version",
      "key",
      "icons",
      "permissions",
      "host_permissions",
      "action",
      "background",
      "content_scripts",
    ]);
    expect(chrome.manifest_version).toBe(3);
    expect(chrome.minimum_chrome_version).toBe("120");
    expect(MINIMUM_CHROME_VERSION).toBe("120");
    expect(JSON.stringify(chrome)).not.toMatch(/gecko|browser_specific_settings|default_area|\.svg/);
  });

  it("keeps name, version, description, permissions and content scripts unchanged", () => {
    for (const key of ["name", "version", "description", "permissions", "host_permissions", "content_scripts"]) {
      expect(chrome[key], key).toEqual(firefoxManifest[key]);
    }
    expect(chrome.permissions).toEqual(["nativeMessaging"]);
    expect(chrome.host_permissions).toEqual([`${SITE_ORIGIN}/*`]);
    // The page bridge stays in the page's own world.
    expect(chrome.content_scripts[0]).toMatchObject({ js: ["page-bridge.js"], world: "MAIN" });
    expect(chrome.content_scripts[1].world).toBeUndefined();
  });

  it("runs the background as a service worker and the toolbar button with PNG icons", () => {
    expect(chrome.background).toEqual({ service_worker: "background.js" });
    expect(chrome.action).toEqual({ default_title: firefoxManifest.action.default_title, default_icon: iconMap() });
    expect(chrome.icons).toEqual({
      16: "icons/icon-16.png",
      32: "icons/icon-32.png",
      48: "icons/icon-48.png",
      128: "icons/icon-128.png",
    });
  });

  it("carries the fixed key, which gives the contract's Chrome extension ID", () => {
    expect(chrome.key).toBe(chromeKey.key);
    expect(chromeExtensionIdFromKey(chromeKey.key)).toBe(CHROME_EXTENSION_ID);
    expect(chromeKey.id).toBe(CHROME_EXTENSION_ID);
    expect(CHROME_EXTENSION_ID).not.toBe(EXTENSION_ID);
  });

  it("does not change its input", () => {
    const copy = structuredClone(firefoxManifest);
    chromeManifest(copy, chromeKey);
    expect(copy).toEqual(firefoxManifest);
  });

  it("refuses manifest keys it does not know, and a key that does not give the stated ID", () => {
    expect(() => chromeManifest({ ...firefoxManifest, web_accessible_resources: [] }, chromeKey)).toThrow(
      /web_accessible_resources/,
    );
    expect(() =>
      chromeManifest({ ...firefoxManifest, action: { ...firefoxManifest.action, default_popup: "p.html" } }, chromeKey),
    ).toThrow(/default_popup/);
    expect(() => chromeManifest(firefoxManifest, { key: chromeKey.key, id: "a".repeat(32) })).toThrow(/gives the ID/);
    expect(() => chromeManifest(firefoxManifest, { id: CHROME_EXTENSION_ID } as never)).toThrow(/chrome-key/);
    expect(() => chromeManifest({ ...firefoxManifest, background: { page: "b.html" } }, chromeKey)).toThrow(/background/);
  });
});

describe("PNG icons", () => {
  it.each(ICON_SIZES)("static/icons icon %i px is a PNG of that size, exactly what make-icons.mjs draws", (size) => {
    const data = readFileSync(join(root, "static", ...iconPath(size).split("/")));
    expect(data.subarray(0, 8)).toEqual(PNG_SIGNATURE);
    expect(data.toString("latin1", 12, 16)).toBe("IHDR");
    expect(data.readUInt32BE(16)).toBe(size);
    expect(data.readUInt32BE(20)).toBe(size);
    expect(data.equals(iconPng(size))).toBe(true);
  }, 60_000); // drawing the 128 px icon takes seconds when every test file runs at once

  it("covers every size the manifest names", () => {
    expect(ICON_SIZES).toEqual([16, 32, 48, 128]);
    expect(Object.values(iconMap())).toEqual(ICON_SIZES.map((s) => iconPath(s)));
  });
});

describe("the builds", () => {
  let tmp = "";
  let chromeDir = "";
  let firefoxDir = "";

  beforeAll(() => {
    tmp = mkdtempSync(join(tmpdir(), "chrome-build-"));
    chromeDir = join(tmp, "chrome-extension");
    firefoxDir = join(tmp, "firefox");
    const chrome = runBuild("--chrome", "--out", chromeDir);
    expect(chrome.status, chrome.stderr).toBe(0);
    const firefox = runBuild("--out", firefoxDir);
    expect(firefox.status, firefox.stderr).toBe(0);
  }, 180_000);

  afterAll(() => {
    if (tmp) rmSync(tmp, { recursive: true, force: true });
  });

  it("package.json has build:chrome and leaves build as the Firefox build", () => {
    const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
    expect(pkg.scripts.build).toBe("node build.mjs");
    expect(pkg.scripts["build:chrome"]).toBe("node build.mjs --chrome");
    const build = readFileSync(join(root, "build.mjs"), "utf8");
    expect(build).toContain('resolve(root, "..", "chrome-extension")');
    expect(build).toContain('join(root, "dist")');
    expect(build).toContain('"chrome120"');
    expect(build).toContain('"firefox140"');
  });

  it("writes the Chrome folder with only generated files and every file the manifest uses", () => {
    const manifest = JSON.parse(readFileSync(join(chromeDir, "manifest.json"), "utf8"));
    expect(manifest).toEqual(chromeManifest(firefoxManifest, chromeKey));
    expect(filesUnder(chromeDir)).toEqual(
      [
        "background.js",
        "compounds-icon.svg",
        "compounds.css",
        "compounds.html",
        "compounds.js",
        "content.js",
        "manifest.json",
        "page-bridge.js",
        ...ICON_SIZES.map((s) => iconPath(s)),
      ].sort(),
    );
    const referenced = [
      manifest.background.service_worker,
      ...manifest.content_scripts.flatMap((c: { js: string[] }) => c.js),
      ...Object.values(manifest.icons),
      ...Object.values(manifest.action.default_icon),
      COMPOUNDS_PAGE,
      "compounds.css",
      "compounds.js",
    ] as string[];
    for (const name of referenced) expect(existsSync(join(chromeDir, ...name.split("/"))), name).toBe(true);
    for (const size of ICON_SIZES) {
      expect(readFileSync(join(chromeDir, ...iconPath(size).split("/"))).equals(iconPng(size))).toBe(true);
    }
  });

  it("replaces browser with chrome at build time in the Chrome bundles that use extension APIs", () => {
    for (const name of ["background.js", "content.js", "compounds.js"]) {
      const text = readFileSync(join(chromeDir, name), "utf8");
      expect(text.startsWith('"use strict";\n(() => {'), name).toBe(true);
      expect(text, name).toContain("chrome.runtime.");
      // No runtime `browser` global is read: no shim, no reference left.
      expect(text, name).not.toMatch(BROWSER_WORD);
    }
  });

  it("the Chrome content script reaches chrome.runtime even when the page has an element with id or name browser", async () => {
    // Chrome has no `browser` global in the isolated world, so such an element
    // shows through as window.browser (DOM clobbering); the bundle must not read it.
    const jsdomName = "jsdom"; // jsdom ships no types: imported by a name TypeScript does not resolve
    type ScriptWindow = Window & typeof globalThis & { eval(code: string): unknown; chrome?: unknown; close(): void };
    const { JSDOM } = (await import(jsdomName)) as {
      JSDOM: new (html: string, options: object) => { window: ScriptWindow };
    };
    const dom = new JSDOM(
      '<!doctype html><html><body><div id="browser"></div><iframe name="browser"></iframe></body></html>',
      { url: `${SITE_ORIGIN}/sciformation/experiment`, runScripts: "outside-only" },
    );
    const w = dom.window;
    try {
      expect(typeof (w as unknown as { browser: unknown }).browser).toBe("object"); // clobbered
      w.name = "contentFrame"; // the ELN frame where the launcher is always shown
      const sent: Array<{ type: string; op?: string }> = [];
      const messageListeners: unknown[] = [];
      w.chrome = {
        runtime: {
          id: CHROME_EXTENSION_ID,
          getURL: (path: string) => `chrome-extension://${CHROME_EXTENSION_ID}/${path}`,
          onMessage: { addListener: (l: unknown) => messageListeners.push(l) },
          sendMessage: (m: { type: string; op?: string }) => {
            sent.push(m);
            return Promise.resolve({ ok: true });
          },
        },
      };
      // The panel's shadow root is closed: keep a handle on it.
      const roots: ShadowRoot[] = [];
      const attach = w.Element.prototype.attachShadow;
      w.Element.prototype.attachShadow = function (this: Element, init: ShadowRootInit) {
        const shadow = attach.call(this, init);
        roots.push(shadow);
        return shadow;
      };
      w.eval(readFileSync(join(chromeDir, "content.js"), "utf8"));
      expect(roots).toHaveLength(1);
      // The import's listener (answers the compounds page only).
      expect(messageListeners).toHaveLength(1);
      const button = (selector: string) => roots[0]!.querySelector<HTMLButtonElement>(selector)!;
      button("button.launcher").click(); // opens the panel, which pings the host
      button("button.all-compounds").click();
      await vi.waitFor(() =>
        expect(sent.map((m) => m.type)).toEqual(expect.arrayContaining(["host-request", "open-compounds"])),
      );
      expect(sent.find((m) => m.type === "host-request")!.op).toBe("ping");
    } finally {
      w.close();
    }
  }, 60_000);

  it("never defines browser in page-bridge.js, which runs in the page's own world", () => {
    const text = readFileSync(join(chromeDir, "page-bridge.js"), "utf8");
    expect(text).not.toMatch(/\bchrome\b|\bbrowser\./);
  });

  it("leaves the Firefox build without a shim, and its manifest for the default ELN address is static/manifest.json", () => {
    for (const name of ["background.js", "content.js", "compounds.js", "page-bridge.js"]) {
      const text = readFileSync(join(firefoxDir, name), "utf8");
      expect(text, name).not.toContain("globalThis.browser");
      expect(text.startsWith('"use strict";\n(() => {'), name).toBe(true);
    }
    expect(readFileSync(join(firefoxDir, "manifest.json"), "utf8")).toBe(firefoxManifestText);
    expect(filesUnder(firefoxDir)).toEqual(
      ["background.js", "compounds-icon.svg", "compounds.css", "compounds.html", "compounds.js", "content.js", "manifest.json", "page-bridge.js"],
    );
  });

  it("the Chrome service worker answers through sendResponse with chrome.* only", async () => {
    const listeners: Array<(message: unknown, sender: unknown, sendResponse: (r: unknown) => void) => unknown> = [];
    const posted: Array<{ id: string; op: string }> = [];
    const portListeners: Array<(m: unknown) => void> = [];
    const created: string[] = [];
    const chrome = {
      runtime: {
        id: CHROME_EXTENSION_ID,
        lastError: undefined,
        getURL: (path: string) => `chrome-extension://${CHROME_EXTENSION_ID}/${path}`,
        connectNative: (name: string) => {
          expect(name).toBe("compound_registry");
          return {
            postMessage: (m: { id: string; op: string }) => {
              posted.push(m);
              queueMicrotask(() => portListeners.forEach((l) => l({ id: m.id, ok: true, result: {} })));
            },
            disconnect: () => {},
            onMessage: { addListener: (l: (m: unknown) => void) => portListeners.push(l) },
            onDisconnect: { addListener: () => {} },
          };
        },
        onMessage: { addListener: (l: (typeof listeners)[number]) => listeners.push(l) },
        // No onSuspend: a service worker may not have it fire; the script must not depend on it.
      },
      tabs: {
        create: async ({ url }: { url: string }) => {
          created.push(url);
          return {};
        },
      },
      action: { onClicked: { addListener: () => {} } },
    };
    // A decoy `browser` global: the bundle must use `chrome` and never read it.
    const browser = { runtime: {} };
    const context = vm.createContext({ chrome, browser, console, crypto: globalThis.crypto, setTimeout, clearTimeout, URL, queueMicrotask });
    new vm.Script(readFileSync(join(chromeDir, "background.js"), "utf8")).runInContext(context);
    expect(listeners).toHaveLength(1);
    const listener = listeners[0]!;

    const ask = async (message: unknown, sender: unknown) => {
      const sendResponse = vi.fn();
      expect(listener(message, sender, sendResponse)).toBe(true);
      await vi.waitFor(() => expect(sendResponse).toHaveBeenCalledTimes(1));
      return sendResponse.mock.calls[0]![0];
    };

    const site = { id: CHROME_EXTENSION_ID, url: `${SITE_ORIGIN}/sciformation/experiment` };
    const page = { id: CHROME_EXTENSION_ID, url: `chrome-extension://${CHROME_EXTENSION_ID}/${COMPOUNDS_PAGE}?q=1` };

    expect(await ask({ type: "open-compounds" }, site)).toEqual({ ok: true });
    expect(created).toEqual([`chrome-extension://${CHROME_EXTENSION_ID}/${COMPOUNDS_PAGE}`]);

    // The compounds page is recognised by its chrome-extension:// URL.
    expect(await ask({ type: "open-compounds" }, page)).toMatchObject({ ok: false, error: { code: "not_allowed" } });
    await ask({ type: "host-request", op: PAGE_OPS[0], params: {} }, page);
    expect(posted.map((m) => m.op)).toEqual([PAGE_OPS[0]]);

    expect(
      await ask({ type: "open-compounds" }, { id: "another-extension", url: site.url }),
    ).toMatchObject({ ok: false, error: { code: "not_allowed" } });
  });

  it("the Chrome service worker names Chrome, not Firefox, when the host cannot be reached", async () => {
    const listeners: Array<(message: unknown, sender: unknown, sendResponse: (r: unknown) => void) => unknown> = [];
    const chrome = {
      runtime: {
        id: CHROME_EXTENSION_ID,
        lastError: undefined,
        getURL: (path: string) => `chrome-extension://${CHROME_EXTENSION_ID}/${path}`,
        connectNative: () => {
          throw new Error("Specified native messaging host not found.");
        },
        onMessage: { addListener: (l: (typeof listeners)[number]) => listeners.push(l) },
      },
      tabs: { create: async () => ({}) },
      action: { onClicked: { addListener: () => {} } },
    };
    const context = vm.createContext({ chrome, console, crypto: globalThis.crypto, setTimeout, clearTimeout, URL, queueMicrotask });
    new vm.Script(readFileSync(join(chromeDir, "background.js"), "utf8")).runInContext(context);
    const sendResponse = vi.fn();
    const sender = { id: CHROME_EXTENSION_ID, url: `${SITE_ORIGIN}/sciformation/experiment` };
    expect(listeners[0]!({ type: "host-request", op: "ping", params: {} }, sender, sendResponse)).toBe(true);
    await vi.waitFor(() => expect(sendResponse).toHaveBeenCalledTimes(1));
    const reply = sendResponse.mock.calls[0]![0] as { ok: boolean; error: { code: string; message: string } };
    expect(reply.error.code).toBe("host_unavailable");
    expect(reply.error.message).toBe(
      "The local registry program could not be reached (Chrome reports: Specified native messaging host not found.).",
    );
    expect(readFileSync(join(firefoxDir, "background.js"), "utf8")).toContain("getBrowserInfo");
  });

  it("refuses to delete an output folder holding files it does not generate", () => {
    const foreign = join(tmp, "foreign");
    mkdirSync(foreign);
    writeFileSync(join(foreign, "notes.txt"), "keep me");
    const result = runBuild("--chrome", "--out", foreign);
    expect(result.status).not.toBe(0);
    expect(result.stderr + result.stdout).toMatch(/refusing to delete/);
    expect(readFileSync(join(foreign, "notes.txt"), "utf8")).toBe("keep me");
    // (Not run against a real ancestor of the sources: build.mjs also refuses
    // those, but a test must never risk deleting the project.)
    expect(readFileSync(join(root, "build.mjs"), "utf8")).toContain("it contains the extension sources");
  });

  it("rebuilds over its own previous output", () => {
    const again = runBuild("--chrome", "--out", chromeDir);
    expect(again.status, again.stderr).toBe(0);
    expect(existsSync(join(chromeDir, "manifest.json"))).toBe(true);
  }, 120_000);
});

describe("the background script (source)", () => {
  async function loadBackground(withSuspend: boolean) {
    const listeners: Array<(message: unknown, sender: unknown, sendResponse: (r: unknown) => void) => unknown> = [];
    const suspend: Array<() => void> = [];
    vi.stubGlobal("browser", {
      runtime: {
        id: EXTENSION_ID,
        lastError: undefined,
        getURL: (path: string) => `moz-extension://uuid/${path}`,
        getBrowserInfo: async () => ({ name: "Firefox" }),
        connectNative: () => {
          throw new Error("no host in this test");
        },
        onMessage: { addListener: (l: (typeof listeners)[number]) => listeners.push(l) },
        ...(withSuspend ? { onSuspend: { addListener: (l: () => void) => suspend.push(l) } } : {}),
      },
      tabs: { create: async () => ({}) },
      action: { onClicked: { addListener: () => {} } },
    });
    vi.resetModules();
    await import("../src/background/index");
    vi.unstubAllGlobals();
    return { listeners, suspend };
  }

  it("answers with sendResponse and returns true, not a Promise", async () => {
    const { listeners, suspend } = await loadBackground(true);
    expect(listeners).toHaveLength(1);
    expect(suspend).toHaveLength(1);
    const sendResponse = vi.fn();
    const result = listeners[0]!({ type: "nonsense" }, { id: EXTENSION_ID, url: `${SITE_ORIGIN}/x` }, sendResponse);
    expect(result).toBe(true);
    await vi.waitFor(() => expect(sendResponse).toHaveBeenCalledTimes(1));
    expect(sendResponse.mock.calls[0]![0]).toMatchObject({ ok: false, error: { code: "not_allowed" } });
  });

  it("forwards host requests and survives a sender that is gone", async () => {
    const { listeners } = await loadBackground(false);
    const sendResponse = vi.fn(() => {
      throw new Error("Attempting to use a disconnected port object");
    });
    expect(listeners[0]!({ type: "host-request", op: "ping", params: {} }, { id: EXTENSION_ID, url: `${SITE_ORIGIN}/x` }, sendResponse)).toBe(true);
    await vi.waitFor(() => expect(sendResponse).toHaveBeenCalledTimes(1));
    expect(sendResponse.mock.calls[0]).toEqual([expect.objectContaining({ ok: false, error: expect.objectContaining({ code: "host_unavailable" }) })]);
    // Firefox (it has runtime.getBrowserInfo) keeps its wording.
    // (The reason is whatever connect threw; the stubbed globals are gone by now.)
    expect(((sendResponse.mock.calls[0] as unknown[])[0] as { error: { message: string } }).error.message).toMatch(
      /^The local registry program could not be reached \(Firefox reports: .+\)\.$/,
    );
  });

  it("the source registers onMessage with a listener that does not return a Promise", () => {
    const text = readFileSync(join(root, "src", "background", "index.ts"), "utf8");
    expect(text).toContain("browser.runtime.onMessage.addListener(onRuntimeMessage)");
    expect(text).toMatch(/sendResponse: \(reply: unknown\) => void\): true \{/);
    expect(text).toContain("browser.runtime.onSuspend?.addListener");
  });
});

describe("npm run package", () => {
  let tmp = "";

  beforeAll(() => {
    tmp = mkdtempSync(join(tmpdir(), "package-"));
  });

  afterAll(() => {
    if (tmp) rmSync(tmp, { recursive: true, force: true });
  });

  function project(name: string, version = "9.9.9") {
    const dir = join(tmp, name);
    mkdirSync(dir);
    writeFileSync(join(dir, "package.json"), JSON.stringify({ version }));
    return {
      root: dir,
      dist: join(dir, "dist"),
      artifacts: join(dir, "artifacts"),
      xpi: join(dir, "artifacts", `chemlinker-${version}-firefox.xpi`),
      zip: join(dir, "artifacts", `chemlinker-${version}-chrome.zip`),
    };
  }

  function writeDist(dist: string, version: string, extra: Record<string, unknown> = {}, marker = "fresh") {
    rmSync(dist, { recursive: true, force: true });
    mkdirSync(dist, { recursive: true });
    const manifest = {
      manifest_version: 3,
      version,
      browser_specific_settings: { gecko: { id: EXTENSION_ID } },
      background: { scripts: ["background.js"] },
      content_scripts: [{ js: ["content.js"] }],
      ...extra,
    };
    writeFileSync(join(dist, "manifest.json"), JSON.stringify(manifest));
    for (const name of ["background.js", "content.js", "compounds.html", "compounds.css", "compounds.js"]) {
      writeFileSync(join(dist, name), `// ${marker} ${name}`);
    }
  }

  const quiet = () => {};

  it("rebuilds dist/ before zipping", async () => {
    const p = project("rebuild");
    writeDist(p.dist, "9.9.8", {}, "stale");
    const order: string[] = [];
    const out = await packageExtension({
      ...p,
      log: quiet,
      build: () => {
        order.push("build");
        writeDist(p.dist, "9.9.9");
      },
    });
    expect(order).toEqual(["build"]);
    expect(out).toBe(p.xpi);
    const zip = readFileSync(p.xpi);
    expect(zip.includes(Buffer.from("// fresh background.js"))).toBe(true);
    expect(zip.includes(Buffer.from("stale"))).toBe(false);
  });

  it("refuses a dist/ whose version is not package.json's, and writes nothing", async () => {
    const p = project("stale");
    await expect(
      packageExtension({ ...p, log: quiet, build: () => writeDist(p.dist, "9.9.8") }),
    ).rejects.toThrow(/version 9\.9\.8/);
    expect(existsSync(p.xpi)).toBe(false);
  });

  it("refuses a dist/ that is not a Firefox build", async () => {
    const p = project("chrome");
    await expect(
      packageExtension({ ...p, log: quiet, build: () => writeDist(p.dist, "9.9.9", { browser_specific_settings: undefined }) }),
    ).rejects.toThrow(/not a Firefox build/);
    expect(existsSync(p.xpi)).toBe(false);
  });

  it("never replaces an existing .xpi of the same version without force, and does not even build", async () => {
    const p = project("exists");
    mkdirSync(p.artifacts);
    writeFileSync(p.xpi, "older package");
    const build = vi.fn(() => writeDist(p.dist, "9.9.9"));
    await expect(packageExtension({ ...p, log: quiet, build })).rejects.toThrow(/already exists.*--force/);
    expect(build).not.toHaveBeenCalled();
    expect(readFileSync(p.xpi, "utf8")).toBe("older package");

    await packageExtension({ ...p, log: quiet, build, force: true });
    expect(build).toHaveBeenCalledTimes(1);
    expect(readFileSync(p.xpi).subarray(0, 4)).toEqual(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
  });

  /** The names of a zip's entries, in archive order (from its central directory). */
  function zipNames(buf: Buffer): string[] {
    const end = buf.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
    const count = buf.readUInt16LE(end + 10);
    let p = buf.readUInt32LE(end + 16);
    const names: string[] = [];
    for (let i = 0; i < count; i++) {
      const nameLength = buf.readUInt16LE(p + 28);
      names.push(buf.subarray(p + 46, p + 46 + nameLength).toString("utf8"));
      p += 46 + nameLength + buf.readUInt16LE(p + 30) + buf.readUInt16LE(p + 32);
    }
    return names;
  }

  function writeChromeDist(dist: string, version: string, extra: Record<string, unknown> = {}) {
    rmSync(dist, { recursive: true, force: true });
    mkdirSync(join(dist, "icons"), { recursive: true });
    const manifest = {
      manifest_version: 3,
      version,
      key: chromeKey.key,
      icons: { 16: "icons/icon-16.png" },
      action: { default_icon: { 16: "icons/icon-16.png" } },
      background: { service_worker: "background.js" },
      content_scripts: [{ js: ["page-bridge.js"] }, { js: ["content.js"] }],
      ...extra,
    };
    writeFileSync(join(dist, "manifest.json"), JSON.stringify(manifest));
    for (const name of ["background.js", "content.js", "page-bridge.js", "compounds.html", "compounds.css", "compounds.js", "icons/icon-16.png"]) {
      writeFileSync(join(dist, ...name.split("/")), `// chrome ${name}`);
    }
  }

  it("packages the Chrome build as a .zip with manifest.json at the archive root", async () => {
    const p = project("chrome-zip");
    const built: string[] = [];
    const out = await packageExtension({
      ...p,
      target: "chrome",
      log: quiet,
      build: () => {
        built.push("build");
        writeChromeDist(p.dist, "9.9.9");
      },
    });
    expect(built).toEqual(["build"]);
    expect(out).toBe(p.zip);
    expect(existsSync(p.xpi)).toBe(false);
    expect(zipNames(readFileSync(p.zip))).toEqual([
      "manifest.json",
      "background.js",
      "compounds.css",
      "compounds.html",
      "compounds.js",
      "content.js",
      "icons/icon-16.png",
      "page-bridge.js",
    ]);
  });

  it("refuses to package a Firefox build, a keyless build or one lacking an icon as the Chrome package", async () => {
    const firefox = project("chrome-from-firefox");
    await expect(
      packageExtension({ ...firefox, target: "chrome", log: quiet, build: () => writeDist(firefox.dist, "9.9.9") }),
    ).rejects.toThrow(/not a Chrome build/);
    const keyless = project("chrome-keyless");
    await expect(
      packageExtension({ ...keyless, target: "chrome", log: quiet, build: () => writeChromeDist(keyless.dist, "9.9.9", { key: undefined }) }),
    ).rejects.toThrow(/not a Chrome build/);
    const noIcon = project("chrome-no-icon");
    await expect(
      packageExtension({
        ...noIcon,
        target: "chrome",
        log: quiet,
        build: () => {
          writeChromeDist(noIcon.dist, "9.9.9");
          rmSync(join(noIcon.dist, "icons", "icon-16.png"));
        },
      }),
    ).rejects.toThrow(/lacks icons\/icon-16\.png/);
    for (const p of [firefox, keyless, noIcon]) expect(existsSync(p.zip)).toBe(false);
    await expect(packageExtension({ ...firefox, target: "safari" as never, log: quiet, build: () => {} })).rejects.toThrow(
      /unknown package target/,
    );
  });

  it("never replaces an existing .zip of the same version without force", async () => {
    const p = project("chrome-exists");
    mkdirSync(p.artifacts);
    writeFileSync(p.zip, "older package");
    const build = vi.fn(() => writeChromeDist(p.dist, "9.9.9"));
    await expect(packageExtension({ ...p, target: "chrome", log: quiet, build })).rejects.toThrow(/already exists.*--force/);
    expect(build).not.toHaveBeenCalled();
    expect(readFileSync(p.zip, "utf8")).toBe("older package");
    await packageExtension({ ...p, target: "chrome", log: quiet, build, force: true });
    expect(readFileSync(p.zip).subarray(0, 4)).toEqual(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
  });

  it("the Chrome package of the real build holds exactly the built folder and keeps the extension ID", async () => {
    const p = project("chrome-real", firefoxManifest.version);
    const dist = join(tmp, "chrome-real-dist");
    const out = await packageExtension({
      root,
      target: "chrome",
      dist,
      artifacts: p.artifacts,
      log: quiet,
      build: () => {
        const result = runBuild("--chrome", "--out", dist);
        expect(result.status, result.stderr).toBe(0);
      },
    });
    expect(out).toBe(join(p.artifacts, `chemlinker-${firefoxManifest.version}-chrome.zip`));
    const names = zipNames(readFileSync(out));
    expect(names[0]).toBe("manifest.json");
    expect([...names].sort()).toEqual(filesUnder(dist));
    const manifest = JSON.parse(readFileSync(join(dist, "manifest.json"), "utf8"));
    expect(chromeExtensionIdFromKey(manifest.key)).toBe(CHROME_EXTENSION_ID);
  }, 120_000);

  it("the npm scripts run the packager, which builds with build.mjs and passes the ELN address on", () => {
    const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
    expect(pkg.scripts.package).toBe("node scripts/package.mjs");
    expect(pkg.scripts["package:chrome"]).toBe("node scripts/package.mjs --chrome");
    const text = readFileSync(join(root, "scripts", "package.mjs"), "utf8");
    expect(text).toContain('[join(root, "build.mjs"), ...TARGETS[target].buildArgs]');
    expect(text).toContain('buildArgs: ["--chrome"]');
    expect(text).toContain('args.push("--eln-origin", elnOrigin)');
    expect(text).toContain("force: { type: \"boolean\", default: false }");
    expect(text).toContain('"eln-origin": { type: "string" }');
    // The Chrome package is made from the folder `npm run build:chrome` writes.
    expect(text).toContain('resolve(root, "..", "chrome-extension")');
  });
});
