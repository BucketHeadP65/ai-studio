import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { parseSync } from "vite";
import { expect, test } from "vitest";

// src/shared/ is app-agnostic: it imports packages and its own files, never the rest of
// studio-client. Both import forms that can reach the app are checked: the `@/` alias
// outside `@/shared/`, and a relative path that climbs out of src/shared/.

const SHARED = dirname(fileURLToPath(import.meta.url));
const SRC = dirname(SHARED);

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

// Every module a file names: static imports, re-exports, and dynamic imports of a plain string.
function importedModules(file: string): string[] {
  const source = readFileSync(file, "utf8");
  const { module, errors } = parseSync(file, source);
  // A file the parser cannot read would hide its imports.
  if (errors.length > 0) throw new Error(`${file} does not parse: ${errors[0].message}`);
  const dynamic = module.dynamicImports.flatMap(({ moduleRequest }) => {
    const literal = /^(["'`])([^"'`$]*)\1$/.exec(
      source.slice(moduleRequest.start, moduleRequest.end),
    );
    return literal ? [literal[2]] : [];
  });
  return [
    ...module.staticImports.map((imported) => imported.moduleRequest.value),
    ...module.staticExports.flatMap((exported) =>
      exported.entries.flatMap((entry) => (entry.moduleRequest ? [entry.moduleRequest.value] : [])),
    ),
    ...dynamic,
  ];
}

function leavesShared(file: string, specifier: string): boolean {
  if (specifier.startsWith("@/")) {
    return specifier !== "@/shared" && !specifier.startsWith("@/shared/");
  }
  if (specifier.startsWith(".")) {
    const target = resolve(dirname(file), specifier);
    return target !== SHARED && !target.startsWith(SHARED + sep);
  }
  return false;
}

test("code under src/shared imports nothing from the rest of studio-client", () => {
  const offenders = sourceFiles(SHARED).flatMap((file) =>
    importedModules(file)
      .filter((specifier) => leavesShared(file, specifier))
      .map((specifier) => `${relative(SRC, file)} imports ${specifier}`),
  );
  expect(offenders).toEqual([]);
});
