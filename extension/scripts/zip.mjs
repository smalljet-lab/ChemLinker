// Minimal, dependency-free ZIP writer (deflate via node:zlib, CRC-32 via
// zlib.crc32). Enough for an .xpi: regular files only, no directories, no
// ZIP64 (every size and offset must fit in 32 bits), UTF-8 names.
import { crc32, deflateRawSync } from "node:zlib";

const FLAG_UTF8 = 0x0800;
const METHOD_STORE = 0;
const METHOD_DEFLATE = 8;
const VERSION = 20; // 2.0: deflate
const MAX_32 = 0xffffffff;

/** Fixed timestamp (1980-01-01 00:00) so the same input gives the same archive. */
const DOS_TIME = 0;
const DOS_DATE = (0 << 9) | (1 << 5) | 1;

/**
 * @param {string} name
 * @returns {string}
 */
export function checkEntryName(name) {
  if (typeof name !== "string" || name.length === 0) throw new Error("empty entry name");
  if (name.includes("\\")) throw new Error(`entry name must use forward slashes: ${name}`);
  if (name.startsWith("/") || /^[A-Za-z]:/.test(name)) throw new Error(`entry name must be relative: ${name}`);
  if (name.split("/").some((part) => part === "" || part === "." || part === "..")) {
    throw new Error(`bad path segment in entry name: ${name}`);
  }
  return name;
}

/**
 * @param {{ name: string, data: Uint8Array }[]} entries
 * @returns {Buffer}
 */
export function createZip(entries) {
  const seen = new Set();
  /** @type {Buffer[]} */
  const chunks = [];
  /** @type {Buffer[]} */
  const central = [];
  let offset = 0;

  for (const entry of entries) {
    const name = checkEntryName(entry.name);
    if (seen.has(name)) throw new Error(`duplicate entry: ${name}`);
    seen.add(name);
    const nameBytes = Buffer.from(name, "utf8");
    const data = Buffer.from(entry.data);
    const crc = crc32(data) >>> 0;
    const deflated = deflateRawSync(data, { level: 9 });
    const useDeflate = deflated.length < data.length;
    const body = useDeflate ? deflated : data;
    const method = useDeflate ? METHOD_DEFLATE : METHOD_STORE;
    if (data.length > MAX_32 || body.length > MAX_32 || offset > MAX_32) {
      throw new Error("archive too large for a ZIP without ZIP64");
    }

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(VERSION, 4);
    local.writeUInt16LE(FLAG_UTF8, 6);
    local.writeUInt16LE(method, 8);
    local.writeUInt16LE(DOS_TIME, 10);
    local.writeUInt16LE(DOS_DATE, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(body.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    local.writeUInt16LE(0, 28);
    chunks.push(local, nameBytes, body);

    const header = Buffer.alloc(46);
    header.writeUInt32LE(0x02014b50, 0);
    header.writeUInt16LE(VERSION, 4); // version made by (MS-DOS / FAT attributes)
    header.writeUInt16LE(VERSION, 6);
    header.writeUInt16LE(FLAG_UTF8, 8);
    header.writeUInt16LE(method, 10);
    header.writeUInt16LE(DOS_TIME, 12);
    header.writeUInt16LE(DOS_DATE, 14);
    header.writeUInt32LE(crc, 16);
    header.writeUInt32LE(body.length, 20);
    header.writeUInt32LE(data.length, 24);
    header.writeUInt16LE(nameBytes.length, 28);
    header.writeUInt16LE(0, 30); // extra
    header.writeUInt16LE(0, 32); // comment
    header.writeUInt16LE(0, 34); // disk
    header.writeUInt16LE(0, 36); // internal attributes
    header.writeUInt32LE(0, 38); // external attributes
    header.writeUInt32LE(offset, 42);
    central.push(header, nameBytes);

    offset += local.length + nameBytes.length + body.length;
  }

  const centralBuf = Buffer.concat(central);
  if (entries.length > 0xffff || offset > MAX_32) throw new Error("too many entries for a ZIP without ZIP64");
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralBuf.length, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([...chunks, centralBuf, end]);
}
