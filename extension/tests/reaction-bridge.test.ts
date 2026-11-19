// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import { createDomBridgeClient, parseBridgeResponse, queueMolfiles } from "../src/content/bridge-client";
import { installBridge, readPageReaction, type BridgeWindow } from "../src/page/bridge";
import { BRIDGE_REQUEST_EVENT, BRIDGE_RESPONSE_EVENT, type BridgeResponse, type PageComponent, type PageReaction } from "../src/shared/bridge";

const here = dirname(fileURLToPath(import.meta.url));
const ORIGIN = "https://sciformation.chemie.unibas.ch";

interface Widgets {
  widgets: Record<string, { uuid: string; html: string }>;
}
interface PageFixture {
  experiments: Record<string, { reaction: PageReaction; molfiles: Record<string, string | null> }>;
}
const widgets = (JSON.parse(readFileSync(join(here, "fixtures", "reaction-widgets-ABC-002.json"), "utf8")) as Widgets).widgets;
const pages = JSON.parse(readFileSync(join(here, "fixtures", "page-reactions.json"), "utf8")) as PageFixture;

/** Puts the ELN's widgets of ABC-002 on the page; `zk.Widget.$('$id')` finds them like the ELN's runtime does. */
function elnPage(options: { skip?: string[]; edit?: (doc: Document) => void } = {}): BridgeWindow {
  const host = document.createElement("div");
  for (const [zkId, w] of Object.entries(widgets)) {
    if (options.skip?.includes(zkId)) continue;
    const box = document.createElement("div");
    box.innerHTML = w.html; // test fixture only
    host.append(box);
  }
  document.body.append(host);
  options.edit?.(document);
  const zk = {
    Widget: {
      $: (selector: unknown) => {
        if (typeof selector !== "string" || !selector.startsWith("$")) return null;
        const w = widgets[selector.slice(1)];
        if (!w || options.skip?.includes(selector.slice(1))) return null;
        return { $n: () => document.getElementById(w.uuid) };
      },
    },
  };
  return {
    document,
    location: { href: `${ORIGIN}/main`, origin: ORIGIN },
    CustomEvent,
    fetch: (async () => {
      throw new Error("no fetch in this test");
    }) as unknown as BridgeWindow["fetch"],
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (t) => clearTimeout(t),
    zk,
    zAu: { cmd0: { download: () => {} } },
  };
}

const withoutHandles = (r: PageReaction) => {
  const strip = (rows: PageComponent[]) => rows.map(({ handle: _h, ...rest }) => rest);
  return { ...r, reactants: strip(r.reactants), reagents: strip(r.reagents) };
};

afterEach(() => {
  document.body.replaceChildren();
});

describe("page bridge: reaction", () => {
  it("reads ABC-002 exactly as the live ELN answered (handles aside)", () => {
    const answer = readPageReaction(elnPage());
    expect(answer).not.toBeNull();
    expect(withoutHandles(answer!)).toEqual(withoutHandles(pages.experiments["ABC-002"]!.reaction));
    // Each row's handle is the DOM id of its structure macro, usable for a molfile request.
    for (const row of answer!.reactants) {
      const node = document.getElementById(row.handle!);
      expect(node?.getAttribute("smiles")).toBe(row.smiles);
    }
  });

  it("reads the units, the solvent row, the reference amount and the shown amounts", () => {
    const r = readPageReaction(elnPage())!;
    expect(r.units).toEqual({ n: "mmol", m: "mg", v: "ml" });
    expect(r.solvents).toEqual([{ name: "EtOH - shared stock", volume: "25", unit: "ml" }]);
    expect(r.referenceAmount).toEqual({ value: "8.232", unit: "mmol" });
    expect(r.amountsShown).toBe("actual");
    expect(r.readOnly).toBe(true);
    expect(r.reactants[0]).toMatchObject({ name: "ABC-001", sourceRef: "4110", sourceCode: "ABC-001" });
  });

  it("is not read-only while one amount can be typed in", () => {
    const r = readPageReaction(
      elnPage({
        edit: (doc) => {
          const input = doc.querySelector<HTMLInputElement>(`#${widgets.elnReactionReactants!.uuid} tr.z-row input[placeholder="m"]`)!;
          input.readOnly = false;
        },
      }),
    )!;
    expect(r.readOnly).toBe(false);
  });

  it("tells planned amounts from the radio group", () => {
    const r = readPageReaction(
      elnPage({
        edit: (doc) => {
          const radios = doc.querySelectorAll<HTMLInputElement>(`#${widgets.displayTargetValues!.uuid} input[type="radio"]`);
          radios[0]!.checked = false;
          radios[1]!.checked = true;
        },
      }),
    )!;
    expect(r.amountsShown).toBe("planned");
  });

  it("uses the solvent field when the solvents grid is empty", () => {
    const r = readPageReaction(
      elnPage({
        edit: (doc) => {
          doc.querySelector(`#${widgets.elnReactionSolvents!.uuid} tbody.z-rows`)!.replaceChildren();
        },
      }),
    )!;
    expect(r.solvents).toEqual([{ name: "EtOH - shared stock", volume: "25", unit: "ml" }]);
  });

  it("gives empty values for missing widgets and answers no_widget outside an experiment", () => {
    const r = readPageReaction(elnPage({ skip: ["elnReactionReagents", "refAmount", "reactant_mass_unit", "displayTargetValues"] }))!;
    expect(r.reagents).toEqual([]);
    expect(r.referenceAmount).toBeNull();
    expect(r.units.m).toBeNull();
    expect(r.amountsShown).toBeNull();
    document.body.replaceChildren();
    expect(readPageReaction(elnPage({ skip: ["elnReactionReactants"] }))).toBeNull();
  });

  it("never throws when the ZK lookup itself throws", () => {
    const win = elnPage();
    win.zk.Widget.$ = () => {
      throw new Error("boom");
    };
    expect(readPageReaction(win)).toBeNull();
  });

  it("answers the request event, and the content side reads the answer", async () => {
    const win = elnPage();
    const responses: BridgeResponse[] = [];
    const listener = (e: Event) => responses.push(JSON.parse((e as CustomEvent).detail as string) as BridgeResponse);
    document.addEventListener(BRIDGE_RESPONSE_EVENT, listener);
    const uninstall = installBridge(win);
    try {
      document.dispatchEvent(new CustomEvent(BRIDGE_REQUEST_EVENT, { detail: JSON.stringify({ id: "x1", kind: "reaction" }) }));
      expect(responses).toHaveLength(1);
      expect(responses[0]).toMatchObject({ id: "x1", ok: true, kind: "reaction" });

      const client = queueMolfiles(createDomBridgeClient(document));
      const answer = await client.reaction(1000);
      expect(answer.ok).toBe(true);
      if (answer.ok) expect(withoutHandles(answer.reaction)).toEqual(withoutHandles(pages.experiments["ABC-002"]!.reaction));
      client.dispose();
    } finally {
      uninstall();
      document.removeEventListener(BRIDGE_RESPONSE_EVENT, listener);
    }
  });

  it("answers no_widget when no experiment is shown, no_zk without ZK", () => {
    const responses: BridgeResponse[] = [];
    const listener = (e: Event) => responses.push(JSON.parse((e as CustomEvent).detail as string) as BridgeResponse);
    document.addEventListener(BRIDGE_RESPONSE_EVENT, listener);
    const win = elnPage({ skip: ["elnReactionReactants"] });
    let uninstall = installBridge(win);
    document.dispatchEvent(new CustomEvent(BRIDGE_REQUEST_EVENT, { detail: JSON.stringify({ id: "a", kind: "reaction" }) }));
    uninstall();
    delete win.zk;
    uninstall = installBridge(win);
    document.dispatchEvent(new CustomEvent(BRIDGE_REQUEST_EVENT, { detail: JSON.stringify({ id: "b", kind: "reaction" }) }));
    uninstall();
    document.removeEventListener(BRIDGE_RESPONSE_EVENT, listener);
    expect(responses).toEqual([
      expect.objectContaining({ id: "a", ok: false, code: "no_widget" }),
      expect.objectContaining({ id: "b", ok: false, code: "no_zk" }),
    ]);
  });

  it("the content side refuses a reaction answer of the wrong shape", () => {
    const good = pages.experiments["ABC-002"]!.reaction;
    expect(parseBridgeResponse(JSON.stringify({ id: "a", ok: true, kind: "reaction", reaction: good }))).toMatchObject({ kind: "reaction" });
    expect(parseBridgeResponse(JSON.stringify({ id: "a", ok: true, kind: "reaction", reaction: { ...good, readOnly: "yes" } }))).toBeNull();
    expect(
      parseBridgeResponse(JSON.stringify({ id: "a", ok: true, kind: "reaction", reaction: { ...good, reactants: [{ row: 1 }] } })),
    ).toBeNull();
    expect(parseBridgeResponse(JSON.stringify({ id: "a", ok: true, kind: "reaction", reaction: null }))).toBeNull();
  });
});
