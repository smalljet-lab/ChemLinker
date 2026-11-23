/**
 * The floating panel: one host element with a CLOSED shadow root, a launcher
 * button, the panel head (title, experiment, close), host problems, one
 * compact card per product, and one "Advanced" toggle that reveals details,
 * the registry database path, the versions of the extension and the registry
 * program, and the manual input fallback.
 *
 * The panel knows nothing about where products come from: it gets a
 * `ProductSource` (the page, normally) and a `HostClient`, and optionally the
 * manual source with its form as a fallback under "Advanced".
 *
 * With `reactions` (the page source only), the panel also stores what the
 * experiment shows about its reaction (`ReactionCapture`): after each
 * successful "Create ID", and while the experiment is shown saved and one of
 * its products has a batch made in it. Only a line under "Advanced" reports it.
 */

import type { ProductSource, SkippedProduct, SourceState } from "../../shared/adapter";
import { REGISTRY_CHANGED, type BackgroundReply, type Op, type ParamsByOp } from "../../shared/protocol";
import { EXTENSION_VERSION } from "../../shared/site";
import { CardController } from "../card-state";
import { ReactionCapture, type ReactionBridge } from "../reaction-capture";
import type { HostClient, OpenReply } from "../host-client";
import { generatorLabel, NameEditor } from "../name-edit";
import { idExamplesOf } from "../id-examples";
import { hostVersionOf, nameGeneratorOf, pingStateFromReply, versionLine, type PingState } from "../ping-state";
import { SearchController } from "../search-state";
import { renderCard } from "./card-view";
import { renderErrorBox } from "./common";
import { makeEl, type El } from "./dom";
import { buildSearchView } from "./search-view";
import { PANEL_CSS } from "./styles";

export const HOST_TAG = "compound-registry-panel";

export interface ManualFallback {
  source: ProductSource;
  form: { element: HTMLElement; prefill(key: string | null, code: string | null): void };
}

export interface PanelDeps {
  doc: Document;
  source: ProductSource;
  client: HostClient;
  /** The manual input, offered under "Advanced" as a fallback. */
  manual?: ManualFallback | null;
  /** This extension's version, compared with the registry program's (default: the version this build was made with). */
  extensionVersion?: string | null;
  copyText?: (text: string) => Promise<void>;
  /** Timer for the cards' debounced automatic lookups (tests). */
  schedule?: (fn: () => void, ms: number) => () => void;
  debounceMs?: number;
  /**
   * Opens the extension's page with all compounds in a new tab (the
   * `open-compounds` message). Without it the panel shows no "All compounds" link.
   */
  openCompounds?: (() => Promise<OpenReply>) | null;
  /**
   * Store the experiment's reaction in the registry (`set_reaction`), read
   * through the page bridge. Only used with a source that reads the page
   * (`autoLookup`); absent: no reaction is stored.
   */
  reactions?: ReactionsDeps | null;
}

export interface ReactionsDeps {
  bridge: ReactionBridge;
  /** Watches the page for changes (default: a MutationObserver on `doc`). */
  observe?: ((onChange: () => void) => () => void) | null;
}

export interface PanelHandle {
  host: HTMLElement;
  /** Closed shadow root; only our own code holds this reference. */
  root: ShadowRoot;
  open(): void;
  close(): void;
  isOpen(): boolean;
  isAdvancedOpen(): boolean;
  destroy(): void;
}

interface CardEntry {
  controller: CardController;
  element: HTMLElement;
  label: string;
  /** The card's own "Details" toggle; kept across re-renders. */
  detailsOpen: boolean;
  names: NameEditor;
}

interface CardList {
  sync(): void;
  entries(): CardEntry[];
  rerenderAll(): void;
  /** Products shown and how many of them have no ID yet (skipped ones count as without ID). */
  counts(): { total: number; withoutId: number };
  destroy(): void;
}

const HOST_STYLE: [string, string][] = [
  ["all", "initial"],
  ["position", "fixed"],
  ["right", "16px"],
  ["bottom", "16px"],
  ["z-index", "2147483647"],
  ["display", "block"],
];

function skippedElement(el: El, item: SkippedProduct): HTMLElement {
  return el(
    "article",
    { class: "card skipped" },
    el("div", { class: "card-head" }, el("div", { class: "card-title" }, el("h3", { text: item.label }))),
    item.pending
      ? el("p", { class: "small busy", text: item.reason })
      : el("div", { class: "banner warn", attrs: { role: "note" }, text: item.reason }),
  );
}

export function mountPanel(deps: PanelDeps): PanelHandle {
  const { doc, source } = deps;
  const manual = deps.manual ?? null;
  const extensionVersion = deps.extensionVersion === undefined ? EXTENSION_VERSION : deps.extensionVersion;

  // Every reply passes through here. A registry_changed answer to ANY request
  // means the host now uses another registry: ping again so the header shows
  // it, and make every shown analysis need a fresh lookup.
  const client: HostClient = {
    async request<K extends Op>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>> {
      const reply = await deps.client.request(op, params);
      if (!reply.ok && reply.error.code === REGISTRY_CHANGED) registryChanged();
      return reply;
    },
  };
  const el = makeEl(doc);
  const copyText = deps.copyText ?? ((text: string) => navigator.clipboard.writeText(text));

  const host = doc.createElement(HOST_TAG);
  for (const [name, value] of HOST_STYLE) host.style.setProperty(name, value, "important");
  const root = host.attachShadow({ mode: "closed" });

  const style = doc.createElement("style");
  style.textContent = PANEL_CSS;

  const statusLine = el("p", { class: "status-line", attrs: { role: "status", "aria-live": "polite" } });
  const announce = (message: string): void => {
    statusLine.textContent = message;
  };

  // Search by ID: independent of the cards; read-only.
  const searchController: SearchController = new SearchController(client, {
    onChange: () => searchView.render(),
    announce,
  });
  const searchView = buildSearchView(doc, searchController);

  // ---------------------------------------------------------------- shell
  const panelId = "crm-panel";
  const advancedId = "crm-advanced";
  const launcher = el("button", {
    class: "launcher",
    attrs: { type: "button", "aria-expanded": "false", "aria-controls": panelId, title: "ChemLinker" },
  });
  const closeButton = el("button", {
    class: "icon-btn",
    attrs: { type: "button", "aria-label": "Close the ChemLinker panel", title: "Close" },
    text: "×",
  });
  const experimentLine = el("p", { class: "experiment" });
  // "All compounds": always in the main view, also when no experiment is shown.
  const navFeedback = el("div", { class: "nav-feedback" });
  let navRow: HTMLElement | null = null;
  if (deps.openCompounds) {
    const openCompounds = deps.openCompounds;
    const allCompounds = el("button", {
      class: "btn small-btn all-compounds",
      attrs: {
        type: "button",
        "aria-label": "All compounds (opens the list of registered compounds in a new tab)",
        title: "Open the list of all registered compounds, with text and structure search, in a new tab",
      },
      text: "All compounds",
    });
    allCompounds.addEventListener("click", () => {
      allCompounds.disabled = true;
      navFeedback.replaceChildren();
      openCompounds()
        .catch((error: unknown) => ({ ok: false as const, error: { code: "internal_error", message: String(error) } }))
        .then((reply) => {
          allCompounds.disabled = false;
          if (reply.ok) {
            announce("The list of all compounds opened in a new tab.");
          } else {
            navFeedback.replaceChildren(renderErrorBox(el, reply.error));
            announce(`The list of all compounds could not be opened: ${reply.error.message}`);
          }
        });
    });
    navRow = el("div", { class: "nav-row" }, allCompounds, navFeedback);
  }
  const pingArea = el("div", { class: "ping" });
  const sourceMessage = el("div", { class: "source-message" });
  const cardsArea = el("div", { class: "cards" });
  const advancedButton = el("button", {
    class: "disclosure",
    attrs: { type: "button", "aria-expanded": "false", "aria-controls": advancedId },
    text: "Advanced",
  });
  const dbPathLine = el("p", { class: "small" });
  const versionsLine = el("p", { class: "small versions" });
  const reactionLine = el("p", { class: "small reaction-line" });
  reactionLine.hidden = true;
  const manualMessage = el("div");
  const manualCards = el("div", { class: "cards" });
  const advancedArea = el(
    "div",
    { class: "advanced", attrs: { id: advancedId } },
    dbPathLine,
    versionsLine,
    reactionLine,
    manual
      ? el(
          "section",
          { class: "section manual", attrs: { "aria-label": "Manual input" } },
          el("h3", { text: "Manual input" }),
          el("p", {
            class: "small",
            text: "Not needed normally: products are read from the page. Use this only when a structure cannot be read.",
          }),
          manual.form.element,
          manualMessage,
          manualCards,
        )
      : null,
  );
  advancedArea.hidden = true;

  const panel = el(
    "section",
    { class: "panel", attrs: { id: panelId, "aria-label": "ChemLinker" } },
    el(
      "div",
      { class: "panel-head" },
      el("div", { class: "panel-title" }, el("h2", { text: "ChemLinker" }), experimentLine),
      closeButton,
    ),
    navRow,
    pingArea,
    sourceMessage,
    cardsArea,
    searchView.element,
    el("div", { class: "section advanced-toggle" }, advancedButton),
    advancedArea,
    statusLine,
  );
  panel.hidden = true;

  const shell = el("div", { class: "root" }, launcher, panel);
  root.append(style, shell);

  // Keystrokes typed in the panel are ours: keep them from the site's own shortcuts.
  for (const type of ["keydown", "keyup", "keypress"]) {
    shell.addEventListener(type, (event) => {
      if (type === "keydown" && (event as KeyboardEvent).key === "Escape" && !panel.hidden) {
        event.preventDefault();
        userClose();
      }
      event.stopPropagation();
    });
  }
  // A file dropped next to the drop zone must not make the tab navigate to it.
  for (const type of ["dragover", "drop"]) {
    shell.addEventListener(type, (event) => {
      const dt = (event as DragEvent).dataTransfer;
      if (dt && Array.from(dt.types ?? []).includes("Files")) event.preventDefault();
    });
  }

  // ---------------------------------------------------------------- ping
  let ping: PingState = { kind: "idle" };
  let pingSeq = 0;
  /** True while the ping in flight was started because the registry changed. */
  let pingIsAutomatic = false;

  function registryChanged(): void {
    for (const list of lists()) for (const entry of list.entries()) entry.controller.invalidate();
    announce("The registry configuration changed. Checking which registry is now in use; look up again before creating an ID.");
    // One automatic ping at a time; if that ping itself answers registry_changed, stop and offer Retry.
    if (ping.kind === "pending" && pingIsAutomatic) return;
    void runPing(true);
  }

  function renderPing(): void {
    const children: (HTMLElement | null)[] = [];
    if (ping.kind === "pending") {
      children.push(el("p", { class: "small busy", text: "Contacting the local registry program…" }));
    } else if (ping.kind === "ok") {
      if (ping.nonDefaultHome !== null) {
        children.push(
          el("p", { class: "banner info compact", attrs: { role: "note" }, text: `Registry: ${ping.nonDefaultHome} (not the default)` }),
        );
      }
      if (ping.warnings.length > 0) {
        children.push(
          el(
            "div",
            { class: "banner warn", attrs: { role: "note" } },
            el("strong", { text: "Registry warnings" }),
            el("ul", null, ...ping.warnings.map((w) => el("li", { text: w }))),
          ),
        );
      }
      if (ping.versionNotice !== null) {
        children.push(
          el(
            "div",
            { class: "banner warn version-notice", attrs: { role: "note" } },
            el("strong", { text: "Extension and registry program are from different releases" }),
            el("p", { text: ping.versionNotice }),
          ),
        );
      }
    } else if (ping.kind === "mismatch") {
      children.push(
        el(
          "div",
          { class: "banner error", attrs: { role: "alert" } },
          el("strong", { text: "Extension and registry program do not match" }),
          el("p", { text: ping.message }),
          ping.hostVersion ? el("p", { class: "code", text: `Host version ${ping.hostVersion}` }) : null,
        ),
      );
    } else if (ping.kind === "error") {
      children.push(renderErrorBox(el, ping.error, "ping"));
    }
    if (ping.kind === "error" || ping.kind === "mismatch") {
      const retry = el("button", { class: "btn", attrs: { type: "button" }, text: "Retry" });
      retry.addEventListener("click", () => {
        void runPing();
      });
      children.push(el("div", { class: "row" }, retry));
    }
    pingArea.replaceChildren(...children.filter((c): c is HTMLElement => c !== null));
    dbPathLine.replaceChildren();
    if (ping.kind === "ok") {
      dbPathLine.append("Registry database: ", el("span", { class: "mono", text: ping.dbPath }));
    }
    const versions = versionLine(extensionVersion, hostVersionOf(ping));
    versionsLine.textContent = versions ?? "";
    versionsLine.hidden = versions === null;
  }

  async function runPing(automatic = false): Promise<void> {
    const seq = ++pingSeq;
    pingIsAutomatic = automatic;
    ping = { kind: "pending" };
    renderPing();
    const reply = await client.request("ping", {});
    if (seq !== pingSeq) return;
    pingIsAutomatic = false;
    ping = pingStateFromReply(reply, undefined, extensionVersion);
    renderPing();
    // The registry's ID format is known now: the search field shows IDs of that shape.
    searchView.setExamples(idExamplesOf(ping));
    // Whether names can be got from ChemDraw is known now: the name rows offer it (or not).
    for (const list of lists()) list.rerenderAll();
    if (ping.kind === "error") announce(`Registry program not reachable: ${ping.error.message}`);
    else if (ping.kind === "mismatch") announce("Extension and registry program do not match.");
  }

  // ---------------------------------------------------------------- reactions
  const capture =
    deps.reactions && source.autoLookup
      ? new ReactionCapture({
          bridge: deps.reactions.bridge,
          client,
          doc,
          observe: deps.reactions.observe ?? null,
          onChange: () => {
            const text = capture?.status() ?? null;
            reactionLine.textContent = text ?? "";
            reactionLine.hidden = text === null;
          },
          ...(deps.schedule ? { schedule: deps.schedule } : {}),
        })
      : null;

  /** True when a card of the shown experiment knows a batch registered in it (its lookup or its own Create ID). */
  function hasBatchIn(ref: string): boolean {
    for (const entry of mainList.entries()) {
      const s = entry.controller.state;
      if (s.analysis?.result.batches.some((b) => b.experiment_ref === ref)) return true;
      if (s.registration?.result.batch.experiment_ref === ref) return true;
    }
    return false;
  }

  function syncCapture(): void {
    if (!capture) return;
    const state = source.getState();
    const experiment = state.kind === "ready" ? state.experiment : null;
    capture.setContext({ experiment, registered: experiment !== null && hasBatchIn(experiment.ref) });
  }

  // ---------------------------------------------------------------- cards
  let advancedOpen = false;

  function createCardList(src: ProductSource, container: HTMLElement, alwaysAdvanced: boolean): CardList {
    const entries = new Map<number, CardEntry>();
    const skippedCache = new Map<string, HTMLElement>();
    let currentSkipped: SkippedProduct[] = [];
    let currentProducts: number[] = [];

    function rerender(entry: CardEntry): void {
      const active = root.activeElement as HTMLElement | null;
      const inside = active !== null && entry.element.contains(active);
      const activeKey = inside ? (active.dataset?.key ?? null) : null;
      // A text input keeps its caret across the re-render.
      const selection =
        inside && active instanceof HTMLInputElement && active.type === "text"
          ? { start: active.selectionStart, end: active.selectionEnd }
          : null;
      renderCard(entry.element, entry.controller, entry.label, { doc, copyText, announce }, {
        advanced: alwaysAdvanced || advancedOpen,
        details: {
          open: entry.detailsOpen,
          toggle: () => {
            entry.detailsOpen = !entry.detailsOpen;
            rerender(entry);
          },
          names: entry.names,
          saveName: (target) => {
            // Answers to lookups sent up to now were computed before this name was stored.
            const sentAfterSeq = entry.controller.state.latestSeq;
            void entry.names.save(target).then((result) => {
              if (result) entry.controller.applyName(result.core_stereo_id, result.name, sentAfterSeq);
            });
          },
          removeName: (target) => {
            const sentAfterSeq = entry.controller.state.latestSeq;
            void entry.names.remove(target).then((result) => {
              if (result) entry.controller.applyName(result.core_stereo_id, result.name, sentAfterSeq);
            });
          },
          generator: nameGeneratorOf(ping),
          generateName: (target, replace) => generateName(entry, target, replace, false),
        },
      });
      // A control the name editor asked for (after Edit / Save / Cancel), else the one that had focus.
      const requested = entry.names.takeFocusKey();
      const focusKey = requested ?? activeKey;
      if (focusKey !== null && (requested !== null || inside)) {
        for (const node of Array.from(entry.element.querySelectorAll<HTMLElement>("[data-key]"))) {
          if (node.dataset.key === focusKey && !(node as HTMLButtonElement).disabled) {
            node.focus();
            if (requested === null && selection && node instanceof HTMLInputElement) {
              node.setSelectionRange(selection.start, selection.end);
            }
            break;
          }
        }
      }
    }

    /**
     * `generate_name` for one card's CORE_STEREO; the stored name the host
     * answers with is shown on the card (like after a save). `auto`: after
     * "Create ID", only when the naming program is available.
     */
    function generateName(entry: CardEntry, target: string, replace: boolean, auto: boolean, sentAfterSeq?: number): void {
      const generator = nameGeneratorOf(ping);
      if (!generator.available) return;
      const seq = sentAfterSeq ?? entry.controller.state.latestSeq;
      void entry.names.generate(target, replace, { label: generatorLabel(generator), auto }).then((result) => {
        if (result) entry.controller.applyName(result.core_stereo_id, result.name, seq);
      });
    }

    function newCard(): CardEntry {
      const entry: CardEntry = {
        element: el("article", { class: "card" }),
        label: "",
        controller: undefined as unknown as CardController,
        detailsOpen: false,
        names: undefined as unknown as NameEditor,
      };
      entry.names = new NameEditor(client, { onChange: () => rerender(entry), announce });
      entry.controller = new CardController(
        client,
        {
          onChange: () => {
            rerender(entry);
            refresh();
          },
          announce,
        },
        {
          autoLookup: src.autoLookup,
          confirmBeforeCreate: src.confirmBeforeCreate,
          recheck: src.recheck ? (product) => src.recheck!(product) : undefined,
          // A new ID without a name: ask ChemDraw once, in the background (the panel does not open for it).
          onRegisteredWithoutName: (target, sentAfterSeq) => generateName(entry, target, false, true, sentAfterSeq),
          // The page's yields are stored with the IDs in the background, like the lookups;
          // never from the manual form (it has no yields anyway).
          saveYields: src.autoLookup && !alwaysAdvanced,
          // The experiment's reaction is stored right after its product got an ID (page source only).
          onRegistered: capture && !alwaysAdvanced ? (experiment) => capture.afterCreate(experiment) : undefined,
          ...(deps.schedule ? { schedule: deps.schedule } : {}),
          ...(deps.debounceMs !== undefined ? { debounceMs: deps.debounceMs } : {}),
        },
      );
      return entry;
    }

    function place(nodes: HTMLElement[]): void {
      const current = Array.from(container.children);
      if (current.length === nodes.length && current.every((n, i) => n === nodes[i])) return;
      container.replaceChildren(...nodes);
    }

    function sync(): void {
      const state = src.getState();
      if (state.kind !== "ready") {
        for (const entry of entries.values()) entry.controller.setInput(null);
        currentSkipped = [];
        currentProducts = [];
        place([...entries.values()].map((e) => e.element));
        return;
      }
      const items: { index: number; node: HTMLElement }[] = [];
      for (const product of state.products) {
        let entry = entries.get(product.productIndex);
        if (!entry) {
          entry = newCard();
          entries.set(product.productIndex, entry);
        }
        entry.label = product.label;
        entry.controller.setInput({ product, experiment: state.experiment });
        rerender(entry);
        items.push({ index: product.productIndex, node: entry.element });
      }
      const wanted = new Set(state.products.map((p) => p.productIndex));
      const orphans: HTMLElement[] = [];
      for (const [index, entry] of [...entries]) {
        if (wanted.has(index)) continue;
        if (entry.controller.state.pending?.op === "register") {
          // Never drop a card that is creating an ID: its answer must be shown.
          entry.controller.setInput(null);
          orphans.push(entry.element);
          continue;
        }
        entry.controller.dispose();
        entries.delete(index);
      }
      currentSkipped = state.skipped ?? [];
      currentProducts = [...wanted];
      const usedKeys = new Set<string>();
      for (const item of currentSkipped) {
        const key = JSON.stringify([item.productIndex, item.label, item.reason, item.pending === true]);
        usedKeys.add(key);
        let node = skippedCache.get(key);
        if (!node) {
          node = skippedElement(el, item);
          skippedCache.set(key, node);
        }
        items.push({ index: item.productIndex, node });
      }
      for (const key of [...skippedCache.keys()]) if (!usedKeys.has(key)) skippedCache.delete(key);
      items.sort((a, b) => a.index - b.index);
      place([...items.map((i) => i.node), ...orphans]);
    }

    return {
      sync,
      entries: () => [...entries.values()],
      rerenderAll: () => {
        for (const entry of entries.values()) rerender(entry);
      },
      counts: () => {
        let withoutId = currentSkipped.length;
        for (const index of currentProducts) {
          if (entries.get(index)?.controller.view().status !== "registered_here") withoutId += 1;
        }
        return { total: currentProducts.length + currentSkipped.length, withoutId };
      },
      destroy: () => {
        for (const entry of entries.values()) entry.controller.dispose();
        entries.clear();
      },
    };
  }

  const mainList = createCardList(source, cardsArea, false);
  const manualList = manual ? createCardList(manual.source, manualCards, true) : null;
  const lists = (): CardList[] => (manualList ? [mainList, manualList] : [mainList]);

  function renderSourceMessage(target: HTMLElement, state: SourceState, showIdle: boolean): void {
    const children: HTMLElement[] = [];
    if (state.kind === "idle" && showIdle) children.push(el("p", { class: "small", text: state.message }));
    else if (state.kind === "error") children.push(el("div", { class: "banner error", attrs: { role: "alert" }, text: state.message }));
    else if (state.kind === "ready" && state.notice) children.push(el("div", { class: "banner info", text: state.notice }));
    target.replaceChildren(...children);
  }

  function currentExperiment(): { ref: string; code: string | null } | null {
    const state = source.getState();
    return state.kind === "ready" && state.experiment ? state.experiment : null;
  }

  function renderExperimentLine(): void {
    const exp = currentExperiment();
    experimentLine.textContent = exp ? (exp.code ? `${exp.code} (${exp.ref})` : `Experiment ${exp.ref}`) : "";
    experimentLine.hidden = exp === null;
  }

  function syncMain(): void {
    // A page source that shows no experiment says nothing (the panel then only offers the search).
    renderSourceMessage(sourceMessage, source.getState(), !source.autoLookup);
    renderExperimentLine();
    mainList.sync();
    refresh();
  }

  function syncManual(): void {
    if (!manual || !manualList) return;
    renderSourceMessage(manualMessage, manual.source.getState(), true);
    manualList.sync();
  }

  // ---------------------------------------------------------------- launcher
  function updateLauncher(): void {
    const { total, withoutId } = mainList.counts();
    const badge = withoutId > 0 ? String(withoutId) : total > 0 ? "✓" : "";
    launcher.replaceChildren("ID");
    if (badge) launcher.append(" ", el("span", { class: badge === "✓" ? "badge done" : "badge", text: badge }));
    const label =
      withoutId > 0
        ? `Open the ChemLinker panel: ${withoutId} ${withoutId === 1 ? "product has" : "products have"} no ID yet`
        : total > 0
          ? "Open the ChemLinker panel: every product has an ID"
          : "Open the ChemLinker panel";
    launcher.setAttribute("aria-label", label);
  }

  /** After any change of the cards: the launcher's count and what the reaction capture watches. */
  function refresh(): void {
    updateLauncher();
    syncCapture();
  }

  // ---------------------------------------------------------------- advanced
  function setAdvanced(openIt: boolean): void {
    if (advancedOpen === openIt) return;
    advancedOpen = openIt;
    advancedButton.setAttribute("aria-expanded", String(openIt));
    advancedArea.hidden = !openIt;
    if (openIt && manual) {
      const exp = currentExperiment();
      if (exp) manual.form.prefill(exp.ref, exp.code);
    }
    // Only hides or shows: no state changes, no requests.
    mainList.rerenderAll();
  }
  advancedButton.addEventListener("click", () => setAdvanced(!advancedOpen));

  // ---------------------------------------------------------------- open / close
  let pinged = false;
  function open(): void {
    panel.hidden = false;
    launcher.hidden = true;
    launcher.setAttribute("aria-expanded", "true");
    if (!pinged) {
      pinged = true;
      void runPing();
    }
    closeButton.focus();
  }
  function close(): void {
    panel.hidden = true;
    launcher.hidden = false;
    launcher.setAttribute("aria-expanded", "false");
  }
  function userClose(): void {
    close();
    launcher.focus();
  }
  launcher.addEventListener("click", () => open());
  closeButton.addEventListener("click", userClose);

  const unsubscribeMain = source.subscribe(syncMain);
  const unsubscribeManual = manual ? manual.source.subscribe(syncManual) : () => {};
  renderPing();
  syncManual();
  syncMain();

  // Leftovers of an earlier copy of the content script are removed first: one host per document.
  for (const old of Array.from(doc.documentElement.querySelectorAll(HOST_TAG))) old.remove();
  doc.documentElement.appendChild(host);

  return {
    host,
    root,
    open: () => open(),
    close: userClose,
    isOpen: () => !panel.hidden,
    isAdvancedOpen: () => advancedOpen,
    destroy: () => {
      unsubscribeMain();
      unsubscribeManual();
      capture?.dispose();
      for (const list of lists()) list.destroy();
      host.remove();
    },
  };
}
