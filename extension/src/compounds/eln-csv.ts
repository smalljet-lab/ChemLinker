/**
 * Reads the experiment list the ELN exports as a CSV file ("Import" view of
 * the compounds page). Bytes in, a list of experiments out. No DOM, no host.
 *
 * The export: first line is the header; a named header cell followed by empty
 * header cells forms one group of columns (e.g. "Yisol (%)" is 7 columns
 * wide). Columns used:
 *   pk                                       the ELN's internal key (required): the registry's experiment_ref
 *   Lab notebook entry                       the ELN number ("ABC-006"): experiment_code
 *   Reaction equation (molecular formulas)   "2.1 LiCH3 (l) + C21H19N ---&gt; C22H23N + C22H23N"
 *   Yisol (%) / Yana (%)                     the first (width - 2) columns are the yields of
 *                                            product 1, 2, ...; the last two hold a ratio and a "de" text
 *   Product 1, Product 2, ...                an amount: the product exists
 *   Experiment status, Title, Type           for display only
 * Text cells carry HTML entities (&gt; ...), which are decoded.
 *
 * Tolerant of a file re-saved by Excel: ";" as separator (decided from the
 * header line), a byte order mark, windows-1252 bytes, LF line ends, rows
 * shorter or longer than the header, empty rows.
 */

export interface ElnProduct {
  /** 1-based product number (the registry's product_index). */
  index: number;
  /** Sum formula as the list gives it (any element order), null when the list has none for this position. */
  formula: string | null;
  /** Isolated yield in percent, null when the list has none. */
  isolated: number | null;
  /** Analytical yield in percent, null when the list has none. */
  analytical: number | null;
  /** The "Product N" cell (an amount such as "8.23 mmol"), null when empty. */
  amount: string | null;
}

export interface ElnExperiment {
  /** The ELN's internal key (digits only). */
  ref: string;
  /** The ELN number, e.g. "ABC-006"; null when the cell is empty. */
  code: string | null;
  status: string | null;
  title: string | null;
  type: string | null;
  /** Line of the file where the row starts (the header is line 1). */
  line: number;
  products: ElnProduct[];
}

export interface ElnProblemRow {
  line: number;
  reason: string;
}

export interface ElnParseResult {
  /** In file order. */
  experiments: ElnExperiment[];
  /** Rows that are not imported, with the reason. */
  problems: ElnProblemRow[];
  /** Set when the file as a whole cannot be used; then `experiments` is empty. */
  error: string | null;
}

export const NO_PK_COLUMN =
  'This does not look like an experiment list exported from the ELN: there is no "pk" column.';
export const EMPTY_FILE = "The file is empty.";

// ------------------------------------------------------------------ bytes to text

/** windows-1252 characters for the bytes 0x80..0x9F (the rest is ISO-8859-1). */
const CP1252_HIGH = [
  0x20ac, 0x81, 0x201a, 0x0192, 0x201e, 0x2026, 0x2020, 0x2021, 0x02c6, 0x2030, 0x0160, 0x2039, 0x0152, 0x8d, 0x017d, 0x8f,
  0x90, 0x2018, 0x2019, 0x201c, 0x201d, 0x2022, 0x2013, 0x2014, 0x02dc, 0x2122, 0x0161, 0x203a, 0x0153, 0x9d, 0x017e, 0x0178,
];

function decodeWindows1252(bytes: Uint8Array): string {
  let out = "";
  const chunk: number[] = [];
  for (const b of bytes) {
    chunk.push(b >= 0x80 && b <= 0x9f ? CP1252_HIGH[b - 0x80]! : b);
    if (chunk.length >= 8192) {
      out += String.fromCharCode(...chunk);
      chunk.length = 0;
    }
  }
  return out + String.fromCharCode(...chunk);
}

/** UTF-8 (with or without BOM); bytes that are not valid UTF-8 are read as windows-1252. */
export function decodeCsvBytes(input: Uint8Array | ArrayBuffer): string {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch {
    text = decodeWindows1252(bytes);
  }
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

// ------------------------------------------------------------------ entities

const NAMED_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
  deg: "°",
  micro: "µ",
  middot: "·",
};

/** Decodes the HTML entities the ELN writes into text cells (&gt; &amp; &#39; &#x3E; ...). Unknown ones stay as they are. */
export function decodeEntities(text: string): string {
  if (!text.includes("&")) return text;
  return text.replace(/&(#x[0-9a-fA-F]{1,6}|#[0-9]{1,7}|[a-zA-Z]{2,8});/g, (whole, body: string) => {
    if (body[0] === "#") {
      const code = body[1] === "x" || body[1] === "X" ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      if (!Number.isFinite(code) || code <= 0 || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return whole;
      return String.fromCodePoint(code);
    }
    return NAMED_ENTITIES[body.toLowerCase()] ?? whole;
  });
}

// ------------------------------------------------------------------ CSV reader

export interface CsvRow {
  /** Line of the file where the row starts (1-based). */
  line: number;
  cells: string[];
}

/** The separator of the file, decided from its first line: ";" when it has more semicolons than commas (outside quotes). */
export function detectSeparator(text: string): "," | ";" {
  let commas = 0;
  let semicolons = 0;
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') inQuotes = !inQuotes;
    else if (!inQuotes && (c === "\n" || c === "\r")) break;
    else if (!inQuotes && c === ",") commas++;
    else if (!inQuotes && c === ";") semicolons++;
  }
  return semicolons > commas ? ";" : ",";
}

/**
 * RFC 4180 with leniency: quoted cells may hold separators, doubled quotes and
 * line breaks (CRLF, LF or CR; returned as "\n"); a quote inside an unquoted
 * cell is kept as a character; a final line break adds no row.
 */
export function readCsv(text: string, separator: string): CsvRow[] {
  const rows: CsvRow[] = [];
  let cells: string[] = [];
  let cell = "";
  let inQuotes = false;
  let line = 1;
  let rowLine = 1;
  let rowStarted = false;
  const n = text.length;
  let i = 0;
  while (i < n) {
    const c = text[i]!;
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i += 2;
        } else {
          inQuotes = false;
          i += 1;
        }
      } else if (c === "\r" || c === "\n") {
        cell += "\n";
        line += 1;
        i += c === "\r" && text[i + 1] === "\n" ? 2 : 1;
      } else {
        cell += c;
        i += 1;
      }
      continue;
    }
    if (c === '"' && cell === "") {
      inQuotes = true;
      rowStarted = true;
      i += 1;
    } else if (c === separator) {
      cells.push(cell);
      cell = "";
      rowStarted = true;
      i += 1;
    } else if (c === "\r" || c === "\n") {
      cells.push(cell);
      rows.push({ line: rowLine, cells });
      cells = [];
      cell = "";
      rowStarted = false;
      i += c === "\r" && text[i + 1] === "\n" ? 2 : 1;
      line += 1;
      rowLine = line;
    } else {
      cell += c;
      rowStarted = true;
      i += 1;
    }
  }
  if (rowStarted || cell !== "" || cells.length > 0) {
    cells.push(cell);
    rows.push({ line: rowLine, cells });
  }
  return rows;
}

// ------------------------------------------------------------------ the ELN's columns

/** A header name for comparison: entities decoded, spaces collapsed, lower case. */
function headerKey(text: string): string {
  return decodeEntities(text).replace(/\s+/g, " ").trim().toLowerCase();
}

interface Group {
  key: string;
  start: number;
  width: number;
}

function groupsOf(header: readonly string[]): Group[] {
  const groups: Group[] = [];
  let current: Group | null = null;
  header.forEach((raw, i) => {
    const key = headerKey(raw);
    if (key !== "") {
      current = { key, start: i, width: 1 };
      groups.push(current);
    } else if (current) {
      current.width += 1;
    }
  });
  return groups;
}

/** The text of a cell: entities decoded, trimmed; null when empty. */
function cellText(cells: readonly string[], index: number | undefined): string | null {
  if (index === undefined) return null;
  const value = decodeEntities(cells[index] ?? "").trim();
  return value === "" ? null : value;
}

const YIELD_CELL = /^(\d+(?:[.,]\d+)?)\s*%$/;

/** "52.38%", "52,38 %" -> 52.38; anything else (empty, "1 : 3.40", "(0 % de)") -> null. */
export function parseYieldCell(text: string | null): number | null {
  if (text === null) return null;
  const m = YIELD_CELL.exec(text.trim());
  if (!m) return null;
  const value = Number(m[1]!.replace(",", "."));
  return Number.isFinite(value) ? value : null;
}

/** How many columns of a yield group hold yields: all but the last two (ratio and "de"). */
export function yieldPositions(width: number): number {
  return width >= 3 ? width - 2 : width;
}

/**
 * The product formulas right of the arrow of "Reaction equation (molecular
 * formulas)", in order: coefficient and state removed. An empty position
 * gives null. No arrow: no products.
 */
export function productFormulas(equation: string | null): (string | null)[] {
  if (equation === null) return [];
  const text = decodeEntities(equation);
  const arrow = /-+>|→/.exec(text);
  if (!arrow) return [];
  const right = text.slice(arrow.index + arrow[0].length).trim();
  if (right === "") return [];
  return right.split(/\s+\+\s+/).map((part) => {
    const formula = part
      .trim()
      .replace(/^\d+(?:[.,]\d+)?\s+/, "")
      .replace(/\s*\((?:s|l|g|aq)\)$/i, "")
      .trim();
    return formula === "" ? null : formula;
  });
}

function yieldsOf(cells: readonly string[], group: Group | undefined): (number | null)[] {
  if (!group) return [];
  const out: (number | null)[] = [];
  for (let k = 0; k < yieldPositions(group.width); k++) out.push(parseYieldCell(cellText(cells, group.start + k)));
  return out;
}

function lastIndexWithValue(values: readonly (number | null)[]): number {
  for (let k = values.length - 1; k >= 0; k--) if (values[k] !== null) return k + 1;
  return 0;
}

/** Parses an exported experiment list. Never throws. */
export function parseElnCsv(input: Uint8Array | ArrayBuffer): ElnParseResult {
  const text = decodeCsvBytes(input);
  if (text.trim() === "") return { experiments: [], problems: [], error: EMPTY_FILE };
  const rows = readCsv(text, detectSeparator(text));
  const header = rows[0];
  if (!header) return { experiments: [], problems: [], error: EMPTY_FILE };
  const groups = groupsOf(header.cells);
  const first = (test: (key: string) => boolean): Group | undefined => groups.find((g) => test(g.key));
  const pk = first((k) => k === "pk");
  if (!pk) return { experiments: [], problems: [], error: NO_PK_COLUMN };
  const code = first((k) => k === "lab notebook entry");
  const status = first((k) => k === "experiment status");
  const title = first((k) => k === "title");
  const type = first((k) => k === "type");
  const equation = first((k) => k === "reaction equation (molecular formulas)");
  const isolatedGroup = first((k) => k === "yisol (%)" || k === "yisol");
  const analyticalGroup = first((k) => k === "yana (%)" || k === "yana");
  const productColumns = new Map<number, number>();
  for (const g of groups) {
    const m = /^product (\d+)$/.exec(g.key);
    if (m) {
      const n = Number(m[1]);
      if (n >= 1 && !productColumns.has(n)) productColumns.set(n, g.start);
    }
  }

  const experiments: ElnExperiment[] = [];
  const problems: ElnProblemRow[] = [];
  const seen = new Map<string, number>();
  for (const row of rows.slice(1)) {
    const cells = row.cells;
    if (cells.every((c) => c.trim() === "")) continue;
    const ref = cellText(cells, pk.start);
    if (ref === null) {
      problems.push({ line: row.line, reason: "The pk cell is empty, so the experiment is not known. The row is not imported." });
      continue;
    }
    if (!/^\d+$/.test(ref)) {
      problems.push({ line: row.line, reason: `The pk "${ref}" is not a number. The row is not imported.` });
      continue;
    }
    const earlier = seen.get(ref);
    if (earlier !== undefined) {
      problems.push({
        line: row.line,
        reason: `The pk ${ref} was already listed on line ${earlier}. This second row is not imported.`,
      });
      continue;
    }
    seen.set(ref, row.line);

    const formulas = productFormulas(cellText(cells, equation?.start));
    const isolated = yieldsOf(cells, isolatedGroup);
    const analytical = yieldsOf(cells, analyticalGroup);
    let highestAmount = 0;
    for (const [n, column] of productColumns) if (cellText(cells, column) !== null) highestAmount = Math.max(highestAmount, n);
    const count = Math.max(formulas.length, highestAmount, lastIndexWithValue(isolated), lastIndexWithValue(analytical));
    const products: ElnProduct[] = [];
    for (let index = 1; index <= count; index++) {
      products.push({
        index,
        formula: formulas[index - 1] ?? null,
        isolated: isolated[index - 1] ?? null,
        analytical: analytical[index - 1] ?? null,
        amount: cellText(cells, productColumns.get(index)),
      });
    }
    experiments.push({
      ref,
      code: cellText(cells, code?.start),
      status: cellText(cells, status?.start),
      title: cellText(cells, title?.start),
      type: cellText(cells, type?.start),
      line: row.line,
      products,
    });
  }
  return { experiments, problems, error: null };
}
