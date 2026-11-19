import { describe, expect, it } from "vitest";
import { GENERIC_HEADING, presentError, retireHint, RUN_INSTALLER_HINT, SAME_RELEASE_HINT } from "../src/content/errors";
import { createRuntimeHostClient } from "../src/content/host-client";
import { hostVersionOf, pingStateFromReply, sameRelease, versionLine } from "../src/content/ping-state";
import { experimentLink, MAX_SVG_CHARS, svgDataUrl } from "../src/content/safe-values";
import { SITE_ORIGIN, type BackgroundReply } from "../src/shared/protocol";
import { analyzeResult, pingResult, registerResult } from "./helpers";

describe("svg data URL", () => {
  it("encodes the SVG into a data URL", () => {
    const svg = "<svg xmlns='http://www.w3.org/2000/svg'><text>#&?</text></svg>";
    const url = svgDataUrl(svg)!;
    expect(url.startsWith("data:image/svg+xml;charset=utf-8,")).toBe(true);
    expect(decodeURIComponent(url.slice("data:image/svg+xml;charset=utf-8,".length))).toBe(svg);
    expect(url).not.toContain("<");
    expect(url).not.toContain("#");
  });
  it("skips missing and oversized thumbnails", () => {
    expect(svgDataUrl(null)).toBeNull();
    expect(svgDataUrl(undefined)).toBeNull();
    expect(svgDataUrl("")).toBeNull();
    expect(svgDataUrl(42)).toBeNull();
    expect(svgDataUrl("x".repeat(MAX_SVG_CHARS + 1))).toBeNull();
    expect(svgDataUrl("x".repeat(MAX_SVG_CHARS))).not.toBeNull();
  });
});

describe("experiment link", () => {
  it.each([
    [`${SITE_ORIGIN}/experiment/4180`, `${SITE_ORIGIN}/experiment/4180`],
    [`${SITE_ORIGIN}`, `${SITE_ORIGIN}/`],
    ["javascript:alert(1)", null],
    ["JAVASCRIPT:alert(1)", null],
    ["data:text/html,<script>alert(1)</script>", null],
    ["http://sciformation.chemie.unibas.ch/x", null],
    ["https://sciformation.chemie.unibas.ch.evil.org/x", null],
    ["https://evil.org/?https://sciformation.chemie.unibas.ch", null],
    ["https://user@evil.org/", null],
    ["/relative/path", null],
    ["", null],
    [null, null],
    [7, null],
  ])("%j -> %j", (input, expected) => {
    expect(experimentLink(input)).toBe(expected);
  });
});

describe("error presentation", () => {
  const codes = [
    "host_unavailable", "host_timeout", "host_bad_response", "protocol_mismatch", "structure_empty",
    "structure_parse_failed", "structure_has_placeholders", "structure_unsupported", "atropisomer_unsupported",
    "inchi_failed", "invalid_mode", "slot_conflict", "stereo_not_in_key", "confirmation_mismatch",
    "id_format_mismatch", "id_overflow", "database_older_than_journal", "journal_conflict",
    "chemistry_version_changed", "config_invalid", "storage_error", "internal_error",
  ];
  it.each(codes)("has a heading for %s and keeps the host message", (code) => {
    const view = presentError({ code, message: "Message from the host." });
    expect(view.heading).not.toBe(GENERIC_HEADING);
    expect(view.message).toBe("Message from the host.");
    expect(view.code).toBe(code);
  });
  it("uses a generic heading for unknown codes", () => {
    const view = presentError({ code: "something_new", message: "m" });
    expect(view.heading).toBe(GENERIC_HEADING);
    expect(view.message).toBe("m");
  });
  it("does not trip over prototype names", () => {
    expect(presentError({ code: "constructor", message: "m" }).heading).toBe(GENERIC_HEADING);
    expect(presentError({ code: "__proto__", message: "m" }).heading).toBe(GENERIC_HEADING);
  });
  it("tells how to install when the host is unavailable, for every operating system", () => {
    const hint = presentError({ code: "host_unavailable", message: "x" }).hint;
    expect(hint).toBe(`The local registry program is not installed or could not start. ${RUN_INSTALLER_HINT}`);
    expect(RUN_INSTALLER_HINT).toBe(
      "Run the installer again (install.ps1 on Windows, install.sh on macOS/Linux), then press Retry.",
    );
  });
  it("tells how to bring both halves to the same release, also for an operation the host does not know", () => {
    expect(SAME_RELEASE_HINT).toBe(
      "Run the update script (update.ps1 on Windows, update.sh on macOS/Linux), then reload the extension, " +
        "so that both come from the same release.",
    );
    expect(presentError({ code: "protocol_mismatch", message: "x" }).hint).toBe(SAME_RELEASE_HINT);
    const unknown = presentError({ code: "unknown_op", message: "Unknown operation 'route'." });
    expect(unknown.heading).toBe("Operation not supported by the registry program");
    expect(unknown.hint).toBe(`The registry program is probably older than this extension. ${SAME_RELEASE_HINT}`);
  });
  it("no hint tells the user to rebuild anything or names a project folder", () => {
    for (const code of ["host_unavailable", "protocol_mismatch", "unknown_op"]) {
      expect(presentError({ code, message: "x" }).hint).not.toMatch(/[Rr]ebuild|project folder/);
    }
  });
  it("names the occupying ID of a slot conflict and how it is retired", () => {
    const view = presentError({ code: "slot_conflict", message: "x", existing_id: "000001_01_001" });
    expect(view.hint).toContain("000001_01_001");
    expect(view.hint).toContain('compound-registry retire 000001_01_001 --reason "<why>"');
    expect(view.existingId).toBe("000001_01_001");
    const unnamed = presentError({ code: "slot_conflict", message: "x" });
    expect(unnamed.hint).toContain(retireHint(null));
    expect(retireHint(null)).toBe('An ID is retired in a terminal with: compound-registry retire <ID> --reason "<why>"');
  });
  it("warns that a register timeout may have issued the ID", () => {
    expect(presentError({ code: "host_timeout", message: "x" }, "register").hint).toMatch(/may or may not/);
  });
});

describe("ping state", () => {
  it("shows the db path, a non-default home and warnings", () => {
    const state = pingStateFromReply({ ok: true, result: pingResult({ home_source: "env", home: "D:\\Reg", warnings: ["w1"] }) });
    expect(state).toMatchObject({ kind: "ok", nonDefaultHome: "D:\\Reg", warnings: ["w1"] });
  });
  it("has no banner for the default home", () => {
    expect(pingStateFromReply({ ok: true, result: pingResult() })).toMatchObject({ kind: "ok", nonDefaultHome: null });
  });
  it("detects a protocol mismatch", () => {
    const state = pingStateFromReply({ ok: true, result: { protocol_version: 2, host_version: "0.9" } as never });
    expect(state.kind).toBe("mismatch");
    expect(state).toMatchObject({ hostProtocol: 2, hostVersion: "0.9" });
    expect(state.kind === "mismatch" && state.message).toBe(
      `The registry program speaks protocol version 2, this extension version 1. ${SAME_RELEASE_HINT}`,
    );
    expect(hostVersionOf(state)).toBe("0.9");
  });
  it("keeps the host version, and warns only when the first two numbers differ from the extension's", () => {
    const ping = (host: string | undefined, extension: string | null) =>
      pingStateFromReply({ ok: true, result: { ...pingResult(), host_version: host as string } }, undefined, extension);
    expect(ping("0.10.0", "0.10.0")).toMatchObject({ kind: "ok", hostVersion: "0.10.0", versionNotice: null });
    expect(ping("0.10.7", "0.10.0")).toMatchObject({ kind: "ok", versionNotice: null });
    const older = ping("0.10.7", "0.11.0");
    expect(older.kind).toBe("ok");
    expect(older.kind === "ok" && older.versionNotice).toBe(
      "The extension is version 0.11.0, the registry program version 0.10.7: " +
        `they come from different releases, and some functions may not work. ${SAME_RELEASE_HINT}`,
    );
    expect(ping("1.10.0", "0.10.0")).toMatchObject({ kind: "ok", versionNotice: expect.stringContaining("1.10.0") });
    // Nothing to compare: no warning.
    expect(ping("0.10.0", null)).toMatchObject({ kind: "ok", hostVersion: "0.10.0", versionNotice: null });
    expect(ping(undefined, "0.10.0")).toMatchObject({ kind: "ok", hostVersion: null, versionNotice: null });
    expect(ping("unknown", "0.10.0")).toMatchObject({ kind: "ok", hostVersion: "unknown", versionNotice: null });
    // Outside a build the extension version is not known.
    expect(pingStateFromReply({ ok: true, result: pingResult({ host_version: "7.7.7" }) })).toMatchObject({ versionNotice: null });
  });
  it("compares releases by their first two numbers", () => {
    expect(sameRelease("0.10.0", "0.10.9")).toBe(true);
    expect(sameRelease("0.10.0", "0.10.0.dev1")).toBe(true);
    expect(sameRelease("0.10.0", "0.1.0")).toBe(false);
    expect(sameRelease("0.10.0", "0.100.0")).toBe(false);
    expect(sameRelease("1.2", "1.2.3")).toBe(true);
    expect(sameRelease("1.2.0", "2.2.0")).toBe(false);
    expect(sameRelease("1", "1.0.0")).toBeNull();
    expect(sameRelease("", "1.0.0")).toBeNull();
    expect(sameRelease("v1.0", "1.0.0")).toBeNull();
  });
  it("writes the versions line from what is known", () => {
    expect(versionLine("0.10.0", "0.10.1")).toBe("Extension 0.10.0 · registry program 0.10.1");
    expect(versionLine("0.10.0", null)).toBe("Extension 0.10.0");
    expect(versionLine(null, "0.10.1")).toBe("Registry program 0.10.1");
    expect(versionLine(null, null)).toBeNull();
    expect(hostVersionOf({ kind: "pending" })).toBeNull();
    expect(hostVersionOf({ kind: "error", error: { code: "host_unavailable", message: "m" } })).toBeNull();
  });
  it("passes errors through", () => {
    expect(pingStateFromReply({ ok: false, error: { code: "host_unavailable", message: "m" } }).kind).toBe("error");
  });
});

describe("runtime host client", () => {
  const errorOf = (r: BackgroundReply) => (r as Extract<BackgroundReply, { ok: false }>).error;

  it("sends a host-request message and returns a valid reply", async () => {
    const sent: unknown[] = [];
    const client = createRuntimeHostClient(async (m) => {
      sent.push(m);
      return { ok: true, result: analyzeResult() };
    });
    const reply = await client.request("analyze", { smiles: "CCO", mode: null });
    expect(sent).toEqual([{ type: "host-request", op: "analyze", params: { smiles: "CCO", mode: null } }]);
    expect(reply.ok).toBe(true);
  });

  it("turns a rejected sendMessage into host_unavailable", async () => {
    const client = createRuntimeHostClient(async () => {
      throw new Error("Could not establish connection. Receiving end does not exist.");
    });
    const error = errorOf(await client.request("ping", {}));
    expect(error.code).toBe("host_unavailable");
    expect(error.message).toContain("Receiving end does not exist");
  });

  it.each([undefined, null, "x", { ok: true }, { ok: false, error: {} }])(
    "turns an unreadable reply %j into host_bad_response",
    async (raw) => {
      const client = createRuntimeHostClient(async () => raw);
      expect(errorOf(await client.request("ping", {})).code).toBe("host_bad_response");
    },
  );

  it("refuses an analyze result whose ignored_stereo is not a list", async () => {
    const bad = analyzeResult();
    (bad.structure as unknown as Record<string, unknown>).ignored_stereo = "allene";
    const client = createRuntimeHostClient(async () => ({ ok: true, result: bad }));
    expect(errorOf(await client.request("analyze", { smiles: "C" })).code).toBe("host_bad_response");
  });

  it("refuses a register result without an id", async () => {
    const bad = { ...registerResult(), id: "" };
    const client = createRuntimeHostClient(async () => ({ ok: true, result: bad }));
    expect(errorOf(await client.request("register", {} as never)).code).toBe("host_bad_response");
  });

  it("accepts a ping of another protocol version so the mismatch can be explained", async () => {
    const client = createRuntimeHostClient(async () => ({ ok: true, result: { protocol_version: 2 } }));
    expect((await client.request("ping", {})).ok).toBe(true);
  });
});

describe("register outcome hints", () => {
  it.each(["host_timeout", "host_unavailable", "host_bad_response", "internal_error", "frame_too_large"])(
    "a failed register with %s says the ID may or may not exist",
    (code) => {
      const view = presentError({ code, message: "host text" }, "register");
      expect(view.hint).toMatch(/may or may not have been created/);
      expect(view.hint).toMatch(/Look up again/);
      expect(view.message).toBe("host text");
    },
  );
  it.each(["host_bad_response", "internal_error", "frame_too_large"])(
    "the same code on analyze has no such hint (%s)",
    (code) => {
      expect(presentError({ code, message: "m" }, "analyze").hint ?? "").not.toMatch(/may or may not/);
    },
  );
  it("response_too_large on register says the ID WAS created", () => {
    const view = presentError(
      { code: "response_too_large", message: "ID 000042_01_003 was created; the answer is too large." },
      "register",
    );
    expect(view.hint).toMatch(/WAS created/);
    expect(view.hint).not.toMatch(/may or may not/);
    expect(view.message).toContain("000042_01_003");
  });
  it.each(["confirmation_mismatch", "slot_conflict", "stereo_not_in_key", "structure_parse_failed"])(
    "a register refused with %s does not claim an unknown outcome",
    (code) => {
      expect(presentError({ code, message: "m" }, "register").hint ?? "").not.toMatch(/may or may not/);
    },
  );
});

describe("registry errors", () => {
  it("presents registry_missing with the host's message", () => {
    const view = presentError({ code: "registry_missing", message: "D:\\Reg\\registry.sqlite does not exist." }, "ping");
    expect(view.heading).toBe("The registry does not exist");
    expect(view.message).toContain("registry.sqlite");
  });
  it("presents registry_changed", () => {
    const view = presentError({ code: "registry_changed", message: "Restart." });
    expect(view.heading).toBe("The registry configuration changed");
    expect(view.hint).toMatch(/look up again/i);
  });
});
