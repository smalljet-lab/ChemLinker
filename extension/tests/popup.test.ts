// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountPopup, replyProblem } from "../src/popup/popup";
import { flush } from "./helpers";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

afterEach(() => {
  document.body.replaceChildren();
});

function mount(send: (message: unknown) => Promise<unknown>) {
  const host = document.createElement("div");
  document.body.append(host);
  const close = vi.fn();
  const sent = vi.fn(send);
  mountPopup({ doc: document, root: host, send: sent, close });
  const buttons = Array.from(host.querySelectorAll("button"));
  return { host, close, sent, buttons };
}

describe("toolbar popup", () => {
  it("has exactly two entries, Capture reaction first and focused", () => {
    const { buttons } = mount(async () => ({ ok: true }));
    expect(buttons.map((b) => b.querySelector(".menu-title")!.textContent)).toEqual(["Capture reaction", "Open compound catalogue"]);
    expect(document.activeElement).toBe(buttons[0]);
    expect(buttons.every((b) => b.getAttribute("type") === "button")).toBe(true);
  });

  it("Capture reaction sends capture-start and closes the popup", async () => {
    const { buttons, sent, close } = mount(async () => ({ ok: true }));
    buttons[0]!.click();
    await flush();
    expect(sent.mock.calls).toEqual([[{ type: "capture-start" }]]);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("Open compound catalogue sends open-compounds and closes the popup", async () => {
    const { buttons, sent, close } = mount(async () => ({ ok: true }));
    buttons[1]!.click();
    await flush();
    expect(sent.mock.calls).toEqual([[{ type: "open-compounds" }]]);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("sends nothing but these two messages, and nothing before a click", async () => {
    const { buttons, sent } = mount(async () => ({ ok: true }));
    expect(sent).not.toHaveBeenCalled();
    buttons[0]!.click();
    await flush();
    buttons[1]!.click();
    await flush();
    expect(sent.mock.calls.map((c) => c[0])).toEqual([{ type: "capture-start" }, { type: "open-compounds" }]);
  });

  it("stays open and shows the reason when the tab cannot be captured", async () => {
    const message = "This tab cannot be captured (the browser reports: Missing activeTab permission).";
    const { host, buttons, close } = mount(async () => ({ ok: false, error: { code: "capture_failed", message } }));
    const problem = host.querySelector<HTMLElement>('[role="alert"]')!;
    expect(problem.hidden).toBe(true);
    buttons[0]!.click();
    await flush();
    expect(close).not.toHaveBeenCalled();
    expect(problem.hidden).toBe(false);
    expect(problem.textContent).toBe(message);
    expect(buttons.every((b) => !b.disabled)).toBe(true);
  });

  it("shows a message when the background does not answer or answers nonsense", async () => {
    const rejecting = mount(async () => {
      throw new Error("Receiving end does not exist");
    });
    rejecting.buttons[0]!.click();
    await flush();
    expect(rejecting.host.querySelector('[role="alert"]')!.textContent).toContain("Receiving end does not exist");
    expect(rejecting.close).not.toHaveBeenCalled();
    expect(replyProblem(undefined)).toContain("unreadable answer");
    expect(replyProblem({ ok: false })).toContain("unreadable answer");
    expect(replyProblem({ ok: true })).toBeNull();
  });

  it("shows text from the background as text", async () => {
    const evil = `<img src=x onerror="window.__pwned=1">`;
    const { host, buttons } = mount(async () => ({ ok: false, error: { code: "capture_failed", message: evil } }));
    buttons[0]!.click();
    await flush();
    expect(host.querySelector("img")).toBeNull();
    expect(host.querySelector('[role="alert"]')!.textContent).toBe(evil);
  });

  it("ignores a second click while the first is under way", async () => {
    let finish: (value: unknown) => void = () => {};
    const { buttons, sent } = mount(() => new Promise((resolve) => (finish = resolve)));
    buttons[0]!.click();
    expect(buttons.every((b) => b.disabled)).toBe(true);
    buttons[0]!.click();
    buttons[1]!.click();
    expect(sent).toHaveBeenCalledTimes(1);
    finish({ ok: true });
    await flush();
  });

  it("the entry script wires sendMessage and window.close, and uses no other extension API", () => {
    const entry = readFileSync(join(root, "src", "popup", "index.ts"), "utf8");
    expect(entry).toContain("browser.runtime.sendMessage(message)");
    expect(entry).toContain("window.close()");
    const used = [...(entry + readFileSync(join(root, "src", "popup", "popup.ts"), "utf8")).matchAll(/\bbrowser\.(\w+)\.(\w+)/g)].map((m) => `${m[1]}.${m[2]}`);
    expect([...new Set(used)]).toEqual(["runtime.sendMessage"]);
  });
});
