// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { DEFAULT_ID_FORMAT, idExamples, idExamplesOf } from "../src/content/id-examples";
import { pingStateFromReply } from "../src/content/ping-state";
import { HOST_TAG, mountPanel, type PanelHandle } from "../src/content/ui/panel";
import { FakeClient, StaticSource, flush, manualTimers, pingResult } from "./helpers";

const PREFIXED = "EX{core:04d}_{stereo:02d}_{batch:03d}";

describe("idExamples", () => {
  it("gives the default format's examples", () => {
    expect(idExamples(DEFAULT_ID_FORMAT)).toEqual({ core: "000042", coreStereo: "000042_02", full: "000042_02_003" });
  });

  it("follows a prefixed format", () => {
    expect(idExamples(PREFIXED)).toEqual({ core: "EX0042", coreStereo: "EX0042_02", full: "EX0042_02_003" });
  });

  it("follows other separators and widths", () => {
    expect(idExamples("C{core:04d}-{stereo:01d}/{batch:02d}")).toEqual({ core: "C0042", coreStereo: "C0042-2", full: "C0042-2/03" });
  });

  it.each([null, undefined, "", "nonsense", "{core}_{stereo:02d}_{batch:03d}", "{stereo:02d}_{core:06d}_{batch:03d}", "x".repeat(300)])(
    "falls back to the default examples for %j",
    (template) => {
      expect(idExamples(template as string | null)).toEqual(idExamples(DEFAULT_ID_FORMAT));
    },
  );

  it("uses the format of a successful ping only", () => {
    expect(idExamplesOf({ kind: "idle" }).core).toBe("000042");
    expect(idExamplesOf({ kind: "pending" }).core).toBe("000042");
    const ok = pingStateFromReply({ ok: true, result: pingResult({ id_format: PREFIXED }) });
    expect(idExamplesOf(ok).full).toBe("EX0042_02_003");
    const failed = pingStateFromReply({ ok: false, error: { code: "host_unavailable", message: "no" } });
    expect(idExamplesOf(failed).core).toBe("000042");
  });
});

describe("panel: the find field shows IDs in the registry's format", () => {
  let handles: PanelHandle[] = [];
  afterEach(() => {
    for (const h of handles) h.destroy();
    handles = [];
    document.documentElement.querySelectorAll(HOST_TAG).forEach((n) => n.remove());
  });

  it("starts with the default examples and switches after the ping", async () => {
    const client = new FakeClient();
    const source = new StaticSource({ kind: "idle", message: "No experiment is shown here." }, { autoLookup: true, confirmBeforeCreate: false });
    const handle = mountPanel({ doc: document, source, client, schedule: manualTimers().schedule });
    handles.push(handle);
    handle.open();
    const input = handle.root.querySelector<HTMLInputElement>(".search input")!;
    expect(input.placeholder).toBe("e.g. 000042_02 or 000042_02_003");
    client.last("ping").resolve({ ok: true, result: pingResult({ id_format: PREFIXED }) });
    await flush();
    expect(input.placeholder).toBe("e.g. EX0042_02 or EX0042_02_003");
  });
});
