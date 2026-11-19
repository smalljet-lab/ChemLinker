import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { chromeManifest } from "../scripts/chrome-manifest.mjs";
import {
  DEFAULT_ELN_ORIGIN,
  ELN_ORIGIN_ENV,
  LOCAL_CONFIG_FILE,
  manifestForEln,
  parseElnOrigin,
  readLocalConfig,
  resolveElnOrigin,
} from "../scripts/eln-origin.mjs";
import { experimentLink } from "../src/content/safe-values";
import { experimentPageUrl } from "../src/shared/import";
import { DEFAULT_SITE_ORIGIN, EXTENSION_VERSION, SITE_ORIGIN } from "../src/shared/site";
import { SITE_ORIGIN as PROTOCOL_SITE_ORIGIN } from "../src/shared/protocol";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const template = JSON.parse(readFileSync(join(root, "static", "manifest.json"), "utf8"));
const chromeKey = JSON.parse(readFileSync(join(root, "static", "chrome-key.json"), "utf8"));
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

const OTHER = "https://eln.example.org:8443";
const OTHER_PATTERN = "https://eln.example.org/*";

let tmp = "";
beforeAll(() => {
  tmp = mkdtempSync(join(tmpdir(), "eln-origin-"));
});
afterAll(() => {
  if (tmp) rmSync(tmp, { recursive: true, force: true });
});

describe("the default ELN address", () => {
  it("is the same in the build script, the source and static/manifest.json", () => {
    expect(DEFAULT_SITE_ORIGIN).toBe(DEFAULT_ELN_ORIGIN);
    // Outside a build nothing replaces the setting: the default applies.
    expect(SITE_ORIGIN).toBe(DEFAULT_SITE_ORIGIN);
    expect(PROTOCOL_SITE_ORIGIN).toBe(SITE_ORIGIN);
    expect(EXTENSION_VERSION).toBeNull();
    const pattern = parseElnOrigin(DEFAULT_ELN_ORIGIN).matchPattern;
    expect(template.host_permissions).toEqual([pattern]);
    for (const script of template.content_scripts) expect(script.matches).toEqual([pattern]);
    expect(manifestForEln(template, pattern)).toEqual(template);
  });

  it("is where experiment links point, at the root of the host", () => {
    expect(experimentPageUrl("4180")).toBe(
      `${SITE_ORIGIN}/startUseCase?useCase=performSearch&table=ElnReaction&pk=4180&viewMode=1`,
    );
    expect(experimentLink(experimentPageUrl("4180"))).not.toBeNull();
  });
});

describe("parseElnOrigin", () => {
  it.each([
    ["https://eln.example.org", "https://eln.example.org", "https://eln.example.org/*"],
    ["https://eln.example.org/", "https://eln.example.org", "https://eln.example.org/*"],
    ["  https://ELN.Example.org  ", "https://eln.example.org", "https://eln.example.org/*"],
    [OTHER, OTHER, OTHER_PATTERN],
    ["https://eln.example.org:443", "https://eln.example.org", "https://eln.example.org/*"],
    ["https://10.0.0.5:8443", "https://10.0.0.5:8443", "https://10.0.0.5/*"],
    ["https://localhost", "https://localhost", "https://localhost/*"],
    ["https://my-eln.sub-1.example.org:1", "https://my-eln.sub-1.example.org:1", "https://my-eln.sub-1.example.org/*"],
    ["https://[::1]:8443", "https://[::1]:8443", "https://[::1]/*"],
  ])("accepts %j", (value, origin, matchPattern) => {
    expect(parseElnOrigin(value)).toEqual({ origin, matchPattern });
  });

  it.each([
    ["", /empty/],
    ["   ", /empty/],
    [undefined, /empty/],
    [42, /empty/],
    ["eln.example.org", /not a URL/],
    ["http://eln.example.org", /must start with https:\/\//],
    ["ftp://eln.example.org", /must start with https:\/\//],
    ["https://eln.example.org/sciformation", /must not contain a path/],
    ["https://eln.example.org/sciformation/", /must not contain a path/],
    ["https://eln.example.org/?a=1", /query or a fragment/],
    ["https://eln.example.org?", /query or a fragment/],
    ["https://eln.example.org/#x", /query or a fragment/],
    ["https://user:secret@eln.example.org", /user name or password/],
    ["https://user@eln.example.org", /user name or password/],
    ["https://*.example.org", /host name is not valid/],
    ["https://*", /host name is not valid|not a URL/],
    ["https://eln..example.org", /host name is not valid/],
    ["https://eln.-example.org", /host name is not valid/],
    ["https://eln-.example.org", /host name is not valid/],
    ["https://eln.example.org.", /host name is not valid/],
    ["https://eln_x.example.org", /host name is not valid/],
    ["https://eln.example.org:0", /port is not valid/],
    ["https://eln.example.org:00", /port is not valid/],
    ["https://[::1]:0", /port is not valid/],
    ["https://eln.example.org:99999", /not a URL/],
    ["https://eln.example.org https://b.example", /white space/],
  ])("refuses %j", (value, why) => {
    expect(() => parseElnOrigin(value, "--eln-origin")).toThrow(why);
    expect(() => parseElnOrigin(value, "--eln-origin")).toThrow(/^--eln-origin: .* is not usable as the ELN address .*for example https:\/\/eln\.example\.org/);
  });
});

describe("resolveElnOrigin", () => {
  const config = (name: string, content: string) => {
    const path = join(tmp, name);
    writeFileSync(path, content);
    return path;
  };

  it("takes the command line first, then the environment, then local.config.json, then the default", async () => {
    const configPath = config("order.json", JSON.stringify({ elnOrigin: "https://from-file.example.org" }));
    const env = { [ELN_ORIGIN_ENV]: "https://from-env.example.org" };
    expect(await resolveElnOrigin({ arg: OTHER, env, configPath })).toEqual({
      origin: OTHER,
      matchPattern: OTHER_PATTERN,
      source: "--eln-origin",
    });
    expect(await resolveElnOrigin({ env, configPath })).toMatchObject({ origin: "https://from-env.example.org", source: ELN_ORIGIN_ENV });
    expect(await resolveElnOrigin({ env: {}, configPath })).toMatchObject({ origin: "https://from-file.example.org", source: LOCAL_CONFIG_FILE });
    expect(await resolveElnOrigin({ env: { [ELN_ORIGIN_ENV]: "" }, configPath })).toMatchObject({ source: LOCAL_CONFIG_FILE });
    expect(await resolveElnOrigin({ env: {}, configPath: join(tmp, "missing.json") })).toEqual({
      ...parseElnOrigin(DEFAULT_ELN_ORIGIN),
      source: "default",
    });
    expect(await resolveElnOrigin()).toMatchObject({ origin: DEFAULT_ELN_ORIGIN, source: "default" });
    expect(ELN_ORIGIN_ENV).toBe("COMPOUND_REGISTRY_ELN_ORIGIN");
    expect(LOCAL_CONFIG_FILE).toBe("local.config.json");
  });

  it("never falls back silently when a given address is not usable", async () => {
    const configPath = config("fallback.json", JSON.stringify({ elnOrigin: "https://from-file.example.org" }));
    await expect(resolveElnOrigin({ arg: "http://x.example", env: {}, configPath })).rejects.toThrow(/^--eln-origin: /);
    await expect(resolveElnOrigin({ arg: "", env: {}, configPath })).rejects.toThrow(/empty/);
    await expect(resolveElnOrigin({ env: { [ELN_ORIGIN_ENV]: "x.example" }, configPath })).rejects.toThrow(/^COMPOUND_REGISTRY_ELN_ORIGIN: /);
  });

  it("reads local.config.json, also one saved with a byte order mark, and says what it must hold", async () => {
    expect(await readLocalConfig(join(tmp, "none.json"))).toBeUndefined();
    expect(await readLocalConfig(config("bom.json", `\uFEFF{"elnOrigin": "${OTHER}"}`))).toBe(OTHER);
    for (const [name, content] of [
      ["broken.json", "{elnOrigin"],
      ["list.json", "[]"],
      ["other.json", '{"origin": "https://eln.example.org"}'],
      ["number.json", '{"elnOrigin": 5}'],
    ] as const) {
      await expect(readLocalConfig(config(name, content))).rejects.toThrow(/must hold \{"elnOrigin": "https:\/\/\.\.\."\}/);
    }
    const bad = config("bad-origin.json", JSON.stringify({ elnOrigin: "https://eln.example.org/path" }));
    await expect(resolveElnOrigin({ env: {}, configPath: bad })).rejects.toThrow(/bad-origin\.json: .*must not contain a path/);
  });
});

describe("manifestForEln", () => {
  it("rewrites host_permissions and every content script's matches, and nothing else", () => {
    const out = manifestForEln(template, OTHER_PATTERN);
    expect(out.host_permissions).toEqual([OTHER_PATTERN]);
    expect(out.content_scripts.map((c: { matches: string[] }) => c.matches)).toEqual([[OTHER_PATTERN], [OTHER_PATTERN]]);
    const back = structuredClone(out);
    back.host_permissions = template.host_permissions;
    for (const [i, script] of back.content_scripts.entries()) script.matches = template.content_scripts[i].matches;
    expect(back).toEqual(template);
    expect(JSON.stringify(out)).not.toContain("unibas");
    // The template is not changed.
    expect(template.host_permissions).toEqual([`${DEFAULT_ELN_ORIGIN}/*`]);
  });

  it("gives the Chrome manifest the same address", () => {
    const chrome = chromeManifest(manifestForEln(template, OTHER_PATTERN), chromeKey);
    expect(chrome.host_permissions).toEqual([OTHER_PATTERN]);
    expect(chrome.content_scripts.map((c: { matches: string[] }) => c.matches)).toEqual([[OTHER_PATTERN], [OTHER_PATTERN]]);
  });

  it("refuses a template that names anything but the default address", () => {
    const extra = structuredClone(template);
    extra.host_permissions.push("https://other.example.org/*");
    expect(() => manifestForEln(extra, OTHER_PATTERN)).toThrow(/must name exactly/);
    const script = structuredClone(template);
    script.content_scripts[1].matches = ["<all_urls>"];
    expect(() => manifestForEln(script, OTHER_PATTERN)).toThrow(/must name exactly/);
  });
});

describe("a build for another ELN address", () => {
  const run = (args: string[], env: Record<string, string> = {}) =>
    spawnSync(process.execPath, [join(root, "build.mjs"), ...args], {
      cwd: root,
      encoding: "utf8",
      env: { ...process.env, [ELN_ORIGIN_ENV]: "", ...env },
    });

  it("writes the address into the manifest and into every bundle that uses it", () => {
    const out = join(tmp, "other-eln");
    const result = run(["--out", out, "--eln-origin", OTHER]);
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout).toContain(`ELN address: ${OTHER} (from --eln-origin)`);

    const manifest = JSON.parse(readFileSync(join(out, "manifest.json"), "utf8"));
    expect(manifest.host_permissions).toEqual([OTHER_PATTERN]);
    expect(manifest.content_scripts.map((c: { matches: string[] }) => c.matches)).toEqual([[OTHER_PATTERN], [OTHER_PATTERN]]);
    expect(manifest).toEqual(manifestForEln(template, OTHER_PATTERN));

    for (const name of ["background.js", "content.js", "compounds.js"]) {
      const text = readFileSync(join(out, name), "utf8");
      expect(text, name).toContain(`"${OTHER}"`);
      expect(text, name).not.toContain("unibas");
    }
    const bridge = readFileSync(join(out, "page-bridge.js"), "utf8");
    expect(bridge).not.toContain("unibas");

    // Each release talks to its own page bridge: the built bridge listens on
    // an event named after the version and marks the window with it.
    const listened: string[] = [];
    const window = { document: { addEventListener: (type: string) => listened.push(type) } };
    vm.runInNewContext(bridge, { window });
    expect(listened).toEqual([`compound-registry-bridge-request/${pkg.version}`]);
    expect(Object.getOwnPropertyNames(window)).toContain(`__compoundRegistryBridgeInstalled/${pkg.version}`);
    // The content script, the panel and the compounds page know the same version.
    const version = new RegExp(`\\bEXTENSION_VERSION = [^;\\n]*"${pkg.version.replaceAll(".", "\\.")}"`);
    for (const name of ["content.js", "compounds.js", "page-bridge.js"]) {
      expect(readFileSync(join(out, name), "utf8"), name).toMatch(version);
    }
  }, 120_000);

  it("stops with a clear message, and writes nothing, when the address is not usable", () => {
    const out = join(tmp, "refused");
    const path = run(["--out", out, "--eln-origin", "https://eln.example.org/sciformation"]);
    expect(path.status).toBe(1);
    expect(path.stderr).toContain("--eln-origin:");
    expect(path.stderr).toContain("Sciformation has to be served at the root of the host");
    expect(path.stderr).not.toMatch(/\n\s+at /); // a message, no stack trace
    const env = run(["--out", out], { [ELN_ORIGIN_ENV]: "http://eln.example.org" });
    expect(env.status).toBe(1);
    expect(env.stderr).toContain("COMPOUND_REGISTRY_ELN_ORIGIN:");
    expect(env.stderr).toContain("it must start with https://");
    expect(() => readFileSync(join(out, "manifest.json"))).toThrow();
  });
});
