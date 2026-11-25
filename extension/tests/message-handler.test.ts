import { describe, expect, it, vi } from "vitest";
import { compoundsPageUrl, createPageOpener } from "../src/background/compounds-page";
import { createMessageHandler, MESSAGE_SENDERS, OPS_BY_SENDER, senderKind, SITE_OPS, type MessageHandlerDeps, type SenderKind } from "../src/background/message-handler";
import {
  CAPTURE_EXPIRED,
  CAPTURE_OPS,
  CAPTURE_PAGE,
  COMPOUNDS_PAGE,
  EXTENSION_ID,
  PAGE_OPS,
  POPUP_PAGE,
  SITE_ORIGIN,
  type BackgroundReply,
  type CaptureImageReply,
  type Op,
} from "../src/shared/protocol";

const OK: BackgroundReply = { ok: true, result: { hello: 1 } as never };
const PAGE_URL = `moz-extension://0b2c7d1e-uuid/${COMPOUNDS_PAGE}`;
const goodSender = { id: EXTENSION_ID, url: `${SITE_ORIGIN}/experiment/edit?id=1`, frameId: 0, tab: { id: 3 } };
const CAPTURE_URL = `moz-extension://0b2c7d1e-uuid/${CAPTURE_PAGE}`;
const POPUP_URL = `moz-extension://0b2c7d1e-uuid/${POPUP_PAGE}`;
const pageSender = { id: EXTENSION_ID, url: PAGE_URL, tab: { id: 9 } };
const captureSender = { id: EXTENSION_ID, url: `${CAPTURE_URL}?id=3f0c2a9e-1b7d-4c55-9e21-0a6d5b8f7c13`, tab: { id: 11 } };
const popupSender = { id: EXTENSION_ID, url: POPUP_URL };
const CAPTURE_OK: BackgroundReply = { ok: true, result: { capture: 1 } as never };
const IMAGE: CaptureImageReply = { ok: true, result: { image: "data:image/png;base64,AAAA" } };

/** Every operation of the protocol (the type makes the list complete). */
const ALL_OPS = Object.keys({
  ping: 0, analyze: 0, register: 0, retire: 0, find: 0, compounds: 0, set_name: 0, generate_name: 0, set_yield: 0,
  yields: 0, compound_files: 0, read_pdf: 0, set_reaction: 0, route: 0, set_limiting: 0,
  recognition_status: 0, recognize: 0, describe_structures: 0, reaction_to_chemdraw: 0,
} satisfies Record<Op, 0>);

function setup(reply: BackgroundReply = OK, over: Partial<MessageHandlerDeps> = {}) {
  const request = vi.fn(async () => reply);
  const captureRequest = vi.fn(async () => CAPTURE_OK);
  const openCompoundsPage = vi.fn(async () => {});
  const startCapture = vi.fn(async () => ({ ok: true }) as const);
  const takeScreenshot = vi.fn((_id: unknown) => IMAGE);
  const handle = createMessageHandler({
    runtimeId: EXTENSION_ID,
    pageUrl: PAGE_URL,
    captureUrl: CAPTURE_URL,
    popupUrl: POPUP_URL,
    request: request as never,
    captureRequest: captureRequest as never,
    openCompoundsPage,
    startCapture,
    takeScreenshot,
    ...over,
  });
  return { request, captureRequest, openCompoundsPage, startCapture, takeScreenshot, handle };
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
    const { handle } = setup(OK, {
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
    expect(senderKind(captureSender, EXTENSION_ID, PAGE_URL, CAPTURE_URL, POPUP_URL)).toBe("capture");
    expect(senderKind(popupSender, EXTENSION_ID, PAGE_URL, CAPTURE_URL, POPUP_URL)).toBe("popup");
    expect(senderKind(pageSender, EXTENSION_ID, PAGE_URL, CAPTURE_URL, POPUP_URL)).toBe("page");
    // Without their addresses the two new pages are nobody.
    expect(senderKind(captureSender, EXTENSION_ID, PAGE_URL)).toBeNull();
    expect(senderKind(popupSender, EXTENSION_ID, PAGE_URL)).toBeNull();
    expect(senderKind({ ...captureSender, id: "evil@x" }, EXTENSION_ID, PAGE_URL, CAPTURE_URL, POPUP_URL)).toBeNull();
    expect(senderKind({ ...captureSender, url: `${CAPTURE_URL}/x` }, EXTENSION_ID, PAGE_URL, CAPTURE_URL, POPUP_URL)).toBeNull();
    expect(senderKind({ ...popupSender, url: `moz-extension://ffff-uuid/${POPUP_PAGE}` }, EXTENSION_ID, PAGE_URL, CAPTURE_URL, POPUP_URL)).toBeNull();
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
    const { request, captureRequest, openCompoundsPage, startCapture, takeScreenshot, handle } = setup();
    const reply = (await handle(message, sender as never)) as Refusal;
    expect(reply.ok).toBe(false);
    expect(reply.error.code).toBe("not_allowed");
    expect(request).not.toHaveBeenCalled();
    expect(captureRequest).not.toHaveBeenCalled();
    expect(openCompoundsPage).not.toHaveBeenCalled();
    expect(startCapture).not.toHaveBeenCalled();
    expect(takeScreenshot).not.toHaveBeenCalled();
  });

  it("never rejects, even when the client throws", async () => {
    const { handle } = setup(OK, {
      request: (async () => {
        throw new Error("boom");
      }) as never,
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
});

describe("background message handler: the capture page and the popup", () => {
  const SENDERS: Record<SenderKind, { id: string; url: string }> = {
    site: goodSender,
    page: pageSender,
    capture: captureSender,
    popup: popupSender,
  };
  const KINDS = Object.keys(SENDERS) as SenderKind[];

  it("serves exactly the four capture operations to the capture page, and no operation to the popup", () => {
    expect([...CAPTURE_OPS]).toEqual(["recognition_status", "recognize", "describe_structures", "reaction_to_chemdraw"]);
    expect(OPS_BY_SENDER).toEqual({ site: SITE_OPS, page: PAGE_OPS, capture: CAPTURE_OPS, popup: [] });
    expect(MESSAGE_SENDERS).toEqual({
      "open-compounds": ["site", "popup"],
      "capture-start": ["popup"],
      "capture-image": ["capture"],
    });
  });

  it.each([...CAPTURE_OPS])("sends %s from the capture page over the capture connection, not the registry one", async (op) => {
    const { request, captureRequest, handle } = setup();
    const params = { any: 1 };
    const reply = await handle({ type: "host-request", op, params }, captureSender);
    expect(reply).toEqual(CAPTURE_OK);
    expect(captureRequest).toHaveBeenCalledWith(op, params);
    expect(request).not.toHaveBeenCalled();
  });

  it("keeps the registry operations on the registry connection", async () => {
    const { request, captureRequest, handle } = setup();
    await handle({ type: "host-request", op: "ping", params: {} }, goodSender);
    await handle({ type: "host-request", op: "compounds", params: {} }, pageSender);
    expect(request).toHaveBeenCalledTimes(2);
    expect(captureRequest).not.toHaveBeenCalled();
  });

  // Every operation of the protocol from every kind of sender: served only where the lists say so.
  const combos = KINDS.flatMap((kind) => ALL_OPS.map((op) => [kind, op] as [SenderKind, string]));
  it.each(combos)("from the %s, operation %s is served only when it is on that list", async (kind, op) => {
    const { request, captureRequest, handle } = setup();
    const reply = await handle({ type: "host-request", op, params: {} }, SENDERS[kind]);
    const allowed = OPS_BY_SENDER[kind].includes(op);
    if (allowed) {
      expect(reply.ok).toBe(true);
      expect(request.mock.calls.length + captureRequest.mock.calls.length).toBe(1);
      expect(captureRequest.mock.calls.length).toBe(kind === "capture" ? 1 : 0);
    } else {
      expect(reply).toMatchObject({ ok: false, error: { code: "not_allowed" } });
      expect(request).not.toHaveBeenCalled();
      expect(captureRequest).not.toHaveBeenCalled();
    }
  });

  it("no capture operation is on the list of the site or of the compounds page, and the other way round", () => {
    for (const op of CAPTURE_OPS) {
      expect(SITE_OPS).not.toContain(op);
      expect(PAGE_OPS).not.toContain(op);
    }
    for (const op of [...SITE_OPS, ...PAGE_OPS]) expect(CAPTURE_OPS as readonly string[]).not.toContain(op);
  });

  // Every other message type from every kind of sender.
  const messages: Record<string, unknown> = {
    "open-compounds": { type: "open-compounds" },
    "capture-start": { type: "capture-start" },
    "capture-image": { type: "capture-image", id: "3f0c2a9e-1b7d-4c55-9e21-0a6d5b8f7c13" },
  };
  const messageCombos = KINDS.flatMap((kind) => Object.keys(messages).map((type) => [kind, type] as [SenderKind, string]));
  it.each(messageCombos)("from the %s, the message %s is served only to its senders", async (kind, type) => {
    const { request, captureRequest, openCompoundsPage, startCapture, takeScreenshot, handle } = setup();
    const reply = await handle(messages[type], SENDERS[kind]);
    const allowed = MESSAGE_SENDERS[type]!.includes(kind);
    const calls = openCompoundsPage.mock.calls.length + startCapture.mock.calls.length + takeScreenshot.mock.calls.length;
    if (allowed) {
      expect(reply.ok).toBe(true);
      expect(calls).toBe(1);
    } else {
      expect(reply).toMatchObject({ ok: false, error: { code: "not_allowed" } });
      expect(calls).toBe(0);
    }
    expect(request).not.toHaveBeenCalled();
    expect(captureRequest).not.toHaveBeenCalled();
  });

  it("starts a capture for the popup and passes the answer on", async () => {
    const { startCapture, handle } = setup();
    expect(await handle({ type: "capture-start" }, popupSender)).toEqual({ ok: true });
    expect(startCapture).toHaveBeenCalledTimes(1);
    const failing = setup(OK, { startCapture: async () => ({ ok: false, error: { code: "capture_failed", message: "no" } }) });
    expect(await failing.handle({ type: "capture-start" }, popupSender)).toEqual({ ok: false, error: { code: "capture_failed", message: "no" } });
  });

  it("opens the compounds page for the popup", async () => {
    const { openCompoundsPage, handle } = setup();
    expect(await handle({ type: "open-compounds" }, popupSender)).toEqual({ ok: true });
    expect(openCompoundsPage).toHaveBeenCalledTimes(1);
  });

  it("hands the screenshot to the capture page by id", async () => {
    const { takeScreenshot, handle } = setup();
    expect(await handle({ type: "capture-image", id: "abc" }, captureSender)).toEqual(IMAGE);
    expect(takeScreenshot).toHaveBeenCalledWith("abc");
    const expired = setup(OK, { takeScreenshot: () => ({ ok: false, error: { code: CAPTURE_EXPIRED, message: "gone" } }) });
    expect(await expired.handle({ type: "capture-image", id: "abc" }, captureSender)).toMatchObject({ ok: false, error: { code: CAPTURE_EXPIRED } });
  });

  const refused: [string, unknown, unknown][] = [
    ["capture-image without an id", { type: "capture-image" }, captureSender],
    ["capture-image with an id that is not text", { type: "capture-image", id: 7 }, captureSender],
    ["capture-image from another extension", messages["capture-image"], { ...captureSender, id: "evil@x" }],
    ["capture-image from a web page", messages["capture-image"], { id: EXTENSION_ID, url: "https://example.org/capture.html" }],
    ["capture-image from the capture page of another extension", messages["capture-image"], { id: "evil@x", url: CAPTURE_URL }],
    ["capture-image from the capture page under another uuid", messages["capture-image"], { ...captureSender, url: `moz-extension://ffff-uuid/${CAPTURE_PAGE}` }],
    ["capture-start from a web page", messages["capture-start"], { id: EXTENSION_ID, url: "https://example.org/popup.html" }],
    ["capture-start without a sender", messages["capture-start"], undefined],
    ["recognize from another extension page", { type: "host-request", op: "recognize", params: {} }, { ...captureSender, url: "moz-extension://0b2c7d1e-uuid/other.html" }],
    ["recognize from a foreign site", { type: "host-request", op: "recognize", params: {} }, { ...goodSender, url: "https://example.org/" }],
    ["recognize without params", { type: "host-request", op: "recognize" }, captureSender],
    ["an unknown message from the popup", { type: "other" }, popupSender],
    ["an unknown message from the capture page", { type: "other" }, captureSender],
  ];
  it.each(refused)("refuses %s with not_allowed", async (_name, message, sender) => {
    const { request, captureRequest, openCompoundsPage, startCapture, takeScreenshot, handle } = setup();
    const reply = (await handle(message, sender as never)) as Refusal;
    expect(reply.ok).toBe(false);
    expect(reply.error.code).toBe("not_allowed");
    for (const fn of [request, captureRequest, openCompoundsPage, startCapture, takeScreenshot]) expect(fn).not.toHaveBeenCalled();
  });
});
