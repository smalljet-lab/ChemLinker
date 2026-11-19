import { describe, expect, it, vi } from "vitest";
import { compoundsPageUrl, createPageOpener, registerToolbarButton } from "../src/background/compounds-page";
import { createMessageHandler, senderKind, SITE_OPS } from "../src/background/message-handler";
import { COMPOUNDS_PAGE, EXTENSION_ID, PAGE_OPS, SITE_ORIGIN, type BackgroundReply } from "../src/shared/protocol";

const OK: BackgroundReply = { ok: true, result: { hello: 1 } as never };
const PAGE_URL = `moz-extension://0b2c7d1e-uuid/${COMPOUNDS_PAGE}`;
const goodSender = { id: EXTENSION_ID, url: `${SITE_ORIGIN}/experiment/edit?id=1`, frameId: 0, tab: { id: 3 } };
const pageSender = { id: EXTENSION_ID, url: PAGE_URL, tab: { id: 9 } };

function setup(reply: BackgroundReply = OK) {
  const request = vi.fn(async () => reply);
  const openCompoundsPage = vi.fn(async () => {});
  const handle = createMessageHandler({ runtimeId: EXTENSION_ID, pageUrl: PAGE_URL, request: request as never, openCompoundsPage });
  return { request, openCompoundsPage, handle };
}

type Refusal = Extract<BackgroundReply, { ok: false }>;

describe("background message handler: the site's content script", () => {
  it("forwards a valid request from the site's content script", async () => {
    const { request, handle } = setup();
    const params = { smiles: "CCO", mode: null };
    const reply = await handle({ type: "host-request", op: "analyze", params }, goodSender);
    expect(reply).toEqual(OK);
    expect(request).toHaveBeenCalledWith("analyze", params);
  });

  it("serves exactly ping, analyze, register, find, set_name, generate_name, set_yield and set_reaction to the site", () => {
    expect([...SITE_OPS]).toEqual(["ping", "analyze", "register", "find", "set_name", "generate_name", "set_yield", "set_reaction"]);
  });

  it.each(["ping", "analyze", "register", "find", "set_name", "generate_name", "set_yield", "set_reaction"])("accepts op %s from the site", async (op) => {
    const { request, handle } = setup();
    await handle({ type: "host-request", op, params: {} }, goodSender);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("opens the compounds page for open-compounds and answers ok", async () => {
    const { request, openCompoundsPage, handle } = setup();
    const reply = await handle({ type: "open-compounds" }, goodSender);
    expect(reply).toEqual({ ok: true });
    expect(openCompoundsPage).toHaveBeenCalledTimes(1);
    expect(request).not.toHaveBeenCalled();
  });

  it("answers internal_error when the tab cannot be opened", async () => {
    const handle = createMessageHandler({
      runtimeId: EXTENSION_ID,
      pageUrl: PAGE_URL,
      request: (async () => OK) as never,
      openCompoundsPage: async () => {
        throw new Error("no tabs");
      },
    });
    const reply = (await handle({ type: "open-compounds" }, goodSender)) as Refusal;
    expect(reply.ok).toBe(false);
    expect(reply.error.code).toBe("internal_error");
    expect(reply.error.message).toContain("no tabs");
  });
});

describe("background message handler: the compounds page", () => {
  it("serves exactly ping, find, compounds, set_name, generate_name, yields, compound_files, read_pdf, route and set_limiting to the page", () => {
    expect([...PAGE_OPS]).toEqual(["ping", "find", "compounds", "set_name", "generate_name", "yields", "compound_files", "read_pdf", "route", "set_limiting"]);
  });

  it.each(["ping", "find", "compounds", "set_name", "generate_name", "yields", "compound_files", "read_pdf", "route", "set_limiting"])("accepts op %s from the page", async (op) => {
    const { request, handle } = setup();
    const params =
      op === "compounds"
        ? { text: "CCO" }
        : op === "yields"
          ? { text: "ABC-006", only_recorded: true }
          : op === "compound_files"
            ? { core_stereo_id: "000042_01" }
            : op === "read_pdf"
              ? { core_stereo_id: "000042_01", name: "a.pdf", offset: 0 }
              : op === "route"
                ? { core_stereo_id: "000042_01", open: true }
                : op === "set_limiting"
                  ? { experiment_ref: "4120", row: 2 }
                  : {};
    const reply = await handle({ type: "host-request", op, params }, pageSender);
    expect(reply).toEqual(OK);
    expect(request).toHaveBeenCalledWith(op, params);
  });

  it.each([
    ["with a query", `${PAGE_URL}?x=1`],
    ["with a fragment", `${PAGE_URL}#top`],
  ])("recognises the page URL %s", async (_name, url) => {
    const { request, handle } = setup();
    await handle({ type: "host-request", op: "compounds", params: {} }, { ...pageSender, url });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("tells the two senders apart", () => {
    expect(senderKind(goodSender, EXTENSION_ID, PAGE_URL)).toBe("site");
    expect(senderKind(pageSender, EXTENSION_ID, PAGE_URL)).toBe("page");
    expect(senderKind({ ...pageSender, id: "evil@x" }, EXTENSION_ID, PAGE_URL)).toBeNull();
    expect(senderKind({ ...pageSender, url: `${PAGE_URL}x` }, EXTENSION_ID, PAGE_URL)).toBeNull();
    expect(senderKind(pageSender, EXTENSION_ID, "")).toBeNull();
  });
});

describe("background message handler: refusals", () => {
  const ping = { type: "host-request", op: "ping", params: {} };
  const generate = { type: "host-request", op: "generate_name", params: { core_stereo_id: "000042_01", replace: false } };
  const refused: [string, unknown, unknown][] = [
    ["another extension", ping, { ...goodSender, id: "evil@x" }],
    ["no sender id", ping, { url: goodSender.url }],
    ["no sender", ping, undefined],
    ["foreign site", ping, { ...goodSender, url: "https://example.org/" }],
    ["http instead of https", ping, { ...goodSender, url: "http://sciformation.chemie.unibas.ch/" }],
    ["look-alike host", ping, { ...goodSender, url: "https://sciformation.chemie.unibas.ch.evil.org/" }],
    ["another extension page", ping, { ...goodSender, url: "moz-extension://0b2c7d1e-uuid/other.html" }],
    ["the page of another extension", ping, { id: "evil@x", url: PAGE_URL }],
    ["the page under another uuid", ping, { ...pageSender, url: `moz-extension://ffff-uuid/${COMPOUNDS_PAGE}` }],
    ["the page as a path suffix", ping, { ...pageSender, url: `${PAGE_URL}/x` }],
    ["no url", ping, { id: EXTENSION_ID }],
    ["bad url", ping, { ...goodSender, url: "::::" }],
    ["wrong type", { type: "other", op: "ping", params: {} }, goodSender],
    ["unknown op", { type: "host-request", op: "export", params: {} }, goodSender],
    ["retire (not served yet)", { type: "host-request", op: "retire", params: { id: "000001_01_001", reason: "x" } }, goodSender],
    ["compounds from the site", { type: "host-request", op: "compounds", params: {} }, goodSender],
    ["analyze from the page", { type: "host-request", op: "analyze", params: { smiles: "C" } }, pageSender],
    ["register from the page", { type: "host-request", op: "register", params: {} }, pageSender],
    ["retire from the page", { type: "host-request", op: "retire", params: {} }, pageSender],
    ["yields from the site", { type: "host-request", op: "yields", params: {} }, goodSender],
    ["set_yield from the page", { type: "host-request", op: "set_yield", params: {} }, pageSender],
    ["compound_files from the site", { type: "host-request", op: "compound_files", params: { core_stereo_id: "000042_01" } }, goodSender],
    ["read_pdf from the site", { type: "host-request", op: "read_pdf", params: { core_stereo_id: "000042_01", name: "a.pdf" } }, goodSender],
    ["read_pdf from another extension page", { type: "host-request", op: "read_pdf", params: { core_stereo_id: "000042_01", name: "a.pdf" } }, { ...pageSender, url: "moz-extension://0b2c7d1e-uuid/other.html" }],
    ["read_pdf from a foreign site", { type: "host-request", op: "read_pdf", params: { core_stereo_id: "000042_01", name: "a.pdf" } }, { ...goodSender, url: "https://example.org/" }],
    ["compound_files without params", { type: "host-request", op: "compound_files" }, pageSender],
    [
      "set_yield from a foreign site",
      { type: "host-request", op: "set_yield", params: { id: "000042_01_001" } },
      { ...goodSender, url: "https://example.org/" },
    ],
    ["set_yield from another extension", { type: "host-request", op: "set_yield", params: {} }, { ...goodSender, id: "evil@x" }],
    ["set_yield without params", { type: "host-request", op: "set_yield" }, goodSender],
    ["generate_name from a foreign site", generate, { ...goodSender, url: "https://example.org/" }],
    ["generate_name from another extension", generate, { ...goodSender, id: "evil@x" }],
    ["generate_name from the page of another extension", generate, { id: "evil@x", url: PAGE_URL }],
    ["generate_name from another extension page", generate, { ...pageSender, url: "moz-extension://0b2c7d1e-uuid/other.html" }],
    ["generate_name without params", { type: "host-request", op: "generate_name" }, goodSender],
    ["open-compounds from the page", { type: "open-compounds" }, pageSender],
    ["open-compounds from a foreign site", { type: "open-compounds" }, { ...goodSender, url: "https://example.org/" }],
    ["open-compounds from another extension", { type: "open-compounds" }, { ...goodSender, id: "evil@x" }],
    ["op not text", { type: "host-request", op: 1, params: {} }, goodSender],
    ["params missing", { type: "host-request", op: "ping" }, goodSender],
    ["params array", { type: "host-request", op: "ping", params: [] }, goodSender],
    ["params missing (page)", { type: "host-request", op: "compounds" }, pageSender],
    ["message not an object", "ping", goodSender],
    ["message null", null, goodSender],
    ["route from the site", { type: "host-request", op: "route", params: { core_stereo_id: "000042_01" } }, goodSender],
    ["route from a foreign site", { type: "host-request", op: "route", params: { core_stereo_id: "000042_01" } }, { ...goodSender, url: "https://example.org/" }],
    ["route from another extension page", { type: "host-request", op: "route", params: { core_stereo_id: "000042_01" } }, { ...pageSender, url: "moz-extension://0b2c7d1e-uuid/other.html" }],
    ["set_reaction from the page", { type: "host-request", op: "set_reaction", params: { experiment_ref: "1" } }, pageSender],
    ["set_reaction from a foreign site", { type: "host-request", op: "set_reaction", params: { experiment_ref: "1" } }, { ...goodSender, url: "https://example.org/" }],
    ["set_reaction from another extension", { type: "host-request", op: "set_reaction", params: { experiment_ref: "1" } }, { ...goodSender, id: "evil@x" }],
    ["set_reaction without params", { type: "host-request", op: "set_reaction" }, goodSender],
    ["set_limiting from the site", { type: "host-request", op: "set_limiting", params: { experiment_ref: "1", row: null } }, goodSender],
    ["set_limiting from a foreign site", { type: "host-request", op: "set_limiting", params: { experiment_ref: "1", row: null } }, { ...goodSender, url: "https://example.org/" }],
    ["set_limiting from another extension page", { type: "host-request", op: "set_limiting", params: { experiment_ref: "1", row: null } }, { ...pageSender, url: "moz-extension://0b2c7d1e-uuid/other.html" }],
  ];

  it.each(refused)("refuses %s with not_allowed", async (_name, message, sender) => {
    const { request, openCompoundsPage, handle } = setup();
    const reply = (await handle(message, sender as never)) as Refusal;
    expect(reply.ok).toBe(false);
    expect(reply.error.code).toBe("not_allowed");
    expect(request).not.toHaveBeenCalled();
    expect(openCompoundsPage).not.toHaveBeenCalled();
  });

  it("never rejects, even when the client throws", async () => {
    const handle = createMessageHandler({
      runtimeId: EXTENSION_ID,
      pageUrl: PAGE_URL,
      request: (async () => {
        throw new Error("boom");
      }) as never,
      openCompoundsPage: async () => {},
    });
    const reply = (await handle({ type: "host-request", op: "ping", params: {} }, goodSender)) as Refusal;
    expect(reply.ok).toBe(false);
    expect(reply.error.message).toContain("boom");
  });
});

describe("opening the compounds page", () => {
  function fakeApi() {
    const create = vi.fn(async (_props: { url: string }) => ({ id: 5 }));
    const getURL = vi.fn((path: string) => `moz-extension://0b2c7d1e-uuid/${path}`);
    return { create, getURL, api: { runtime: { getURL }, tabs: { create } } };
  }

  it("opens compounds.html of this extension in a new tab", async () => {
    const { create, api } = fakeApi();
    expect(compoundsPageUrl(api)).toBe(PAGE_URL);
    await createPageOpener(api)();
    expect(create).toHaveBeenCalledWith({ url: PAGE_URL });
  });

  it("the toolbar button opens the same page on click", async () => {
    const { create, api } = fakeApi();
    let listener: (() => void) | null = null;
    registerToolbarButton({ onClicked: { addListener: (fn) => (listener = fn) } }, createPageOpener(api));
    expect(create).not.toHaveBeenCalled();
    listener!();
    await Promise.resolve();
    expect(create).toHaveBeenCalledWith({ url: PAGE_URL });
  });

  it("a failing toolbar click does not throw", async () => {
    let listener: (() => void) | null = null;
    registerToolbarButton({ onClicked: { addListener: (fn) => (listener = fn) } }, async () => {
      throw new Error("no window");
    });
    expect(() => listener!()).not.toThrow();
    await Promise.resolve();
  });
});
