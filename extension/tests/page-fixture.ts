/**
 * Synthetic Sciformation page fixtures (made-up ids and simple structures),
 * shaped like the reduced product grid the orchestrator verified on the live
 * site. Tests may build DOM from strings; the extension's sources may not.
 */

export interface FixtureYields {
  /** `value` of the analytical yield input (default ""). */
  analytical?: string;
  /** `value` of the isolated yield input (default ""). */
  isolated?: string;
  /** `title` (the unrounded number the ELN puts there) of both inputs, when given. */
  title?: string;
  /** "view" (read-only inputs, the default) or "edit". */
  mode?: "view" | "edit";
}

export interface FixtureRow {
  /** DOM id of the structure macro; omitted = no macro in the row. */
  id?: string;
  /** `smiles` attribute; "" = macro without a drawn structure. */
  smiles?: string;
  /** The yield cells (9, 10) and the comment cell (11); "bare" = cells without inputs. */
  yields?: FixtureYields | "bare";
  /** Number of direct cells (default 15). */
  cells?: number;
}

function yieldInput(placeholder: string, value: string, y: FixtureYields): string {
  const view = (y.mode ?? "view") === "view";
  const title = y.title !== undefined && value !== "" ? ` title="${escapeAttr(y.title)}"` : "";
  return (
    `<input style="min-width: 20px; width: 74px;" class="${view ? "z-textbox z-textbox-readonly" : "z-textbox"}" ` +
    `value="${escapeAttr(value)}"${title} type="text"${view ? ' readonly="readonly"' : ""} placeholder="${placeholder}">`
  );
}

/** A yield cell as on the live page: the input sits in tables nested in the cell. */
function yieldCellHtml(placeholder: string, value: string, y: FixtureYields, withSpan: boolean): string {
  return (
    '<div style="width:100%;" class="z-macro"><table class="z-hbox"><tbody><tr valign="middle"><td>' +
    '<table><tbody><tr valign="middle"><td>' +
    yieldInput(placeholder, value, y) +
    "</td></tr></tbody></table></td></tr></tbody></table></div>" +
    (withSpan ? `<br><span class="z-label z-html">${value !== "" ? "(0.130 mmol)" : ""}</span>` : "")
  );
}

function commentCellHtml(y: FixtureYields): string {
  const view = (y.mode ?? "view") === "view";
  const input = (placeholder: string) =>
    `<input class="${view ? "z-textbox z-textbox-readonly" : "z-textbox"}" value="" type="text"${view ? ' readonly="readonly"' : ""} placeholder="${placeholder}">`;
  return `${input("Comment")}<br>${input("Lot no.")}`;
}

export interface FixtureOptions {
  key?: string;
  code?: string | null;
  rows?: FixtureRow[];
  /** Key of the visible incident link; null = no such link. */
  incidentKey?: string | null;
  /** Key of the hidden template link. */
  templateKey?: string;
  /** Leave out the rows container. */
  noRowsContainer?: boolean;
}

/** Elements inside `[data-hidden]` count as invisible (jsdom has no layout). */
export const testIsVisible = (el: Element): boolean => !el.closest("[data-hidden]");

function escapeAttr(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function rowHtml(row: FixtureRow, odd: boolean): string {
  const cells: string[] = [];
  const y = row.yields === "bare" ? null : (row.yields ?? {});
  for (let i = 0; i < (row.cells ?? 15); i++) {
    let inner = "";
    if (i === 0) inner = '<span class="row-number"></span>';
    if (i === 1) inner = '<input class="z-decimalbox" value="1">';
    if (i === 2 && row.id !== undefined) {
      inner =
        `<div id="${escapeAttr(row.id)}" class="z-macro" smiles="${escapeAttr(row.smiles ?? "")}">` +
        '<img class="z-image" src="/zkau/view/abc/foo.png"></div>';
    }
    if (y && i === 9) inner = yieldCellHtml("analytical", y.analytical ?? "", y, false);
    if (y && i === 10) inner = yieldCellHtml("isolated", y.isolated ?? "", y, true);
    if (i === 11) inner = y ? commentCellHtml(y) : '<input class="comment"><input class="lot">';
    cells.push(`<td class="z-cell">${inner}</td>`);
  }
  return `<tr class="decimal z-row${odd ? " z-grid-odd" : ""}">${cells.join("")}</tr>`;
}

export function experimentHtml(options: FixtureOptions = {}): string {
  const key = options.key ?? "4180";
  const code = options.code === undefined ? "ABC-008" : options.code;
  const rows = options.rows ?? [
    { id: "cNfVkc3", smiles: "CCO" },
    { id: "cNfVkd7", smiles: "CC(=O)O" },
  ];
  const incidentKey = options.incidentKey === undefined ? key : options.incidentKey;
  const templateKey = options.templateKey ?? "1";
  const productLink = (k: string) =>
    `<a class="imgButton" title="Transfer chemical to storage" href="/startUseCase?useCase=addProductToInventory.uc&amp;fromReactionId=${escapeAttr(k)}"><img src="/images/product_to_inventory.png"></a>`;
  return (
    (code !== null ? `<h2 title="${escapeAttr(code)}"><span class="z-html">${escapeAttr(code)}</span></h2>` : "<h2></h2>") +
    `<div data-hidden style="display:none">${productLink(templateKey)}</div>` +
    (incidentKey !== null
      ? `<a href="/startUseCase?useCase=addOrgIncident.uc&amp;elnReaction=${escapeAttr(incidentKey)}">Incident</a>`
      : "") +
    '<div class="decimal z-grid">' +
    `<div class="z-grid-header"><table><tbody><tr class="z-columns"><th class="z-column">${productLink(key)}</th></tr></tbody></table></div>` +
    (options.noRowsContainer
      ? '<div class="z-grid-body"><table></table></div>'
      : `<div class="z-grid-body"><table><tbody class="z-rows">${rows.map((r, i) => rowHtml(r, i % 2 === 1)).join("")}</tbody></table></div>`) +
    "</div>"
  );
}

/** Puts a fixture into the test document's body. */
export function showExperiment(doc: Document, options: FixtureOptions = {}): void {
  // Test-only DOM building; never done in src/.
  doc.body.innerHTML = experimentHtml(options);
}
