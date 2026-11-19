// Runs vitest from the canonical spelling of the project folder.
//
// On Windows a working directory spelled with a lowercase drive letter
// ("c:\Users\...", what VS Code terminals and cmd's `cd /d c:\...` give) makes
// vitest load its own module twice (once as c:\..., once as C:\...), and every
// suite then fails with "Cannot read properties of undefined (reading 'config')".
// `realpathSync.native` returns the spelling Windows stores (upper-case drive
// letter, real case of every folder), so vitest is started from there.
import { spawnSync } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = realpathSync.native(dirname(dirname(fileURLToPath(import.meta.url))));
const cli = join(root, "node_modules", "vitest", "vitest.mjs");
if (!existsSync(cli)) {
  console.error(`vitest is not installed (${cli} is missing); run \`npm ci\` first.`);
  process.exit(1);
}

const result = spawnSync(process.execPath, [cli, ...process.argv.slice(2)], { cwd: root, stdio: "inherit" });
if (result.error) {
  console.error(result.error);
  process.exit(1);
}
process.exit(result.status ?? 1);
