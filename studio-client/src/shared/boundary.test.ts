import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
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
    ts
      .preProcessFile(readFileSync(file, "utf8"), true, true)
      .importedFiles.map((imported) => imported.fileName)
      .filter((specifier) => leavesShared(file, specifier))
      .map((specifier) => `${relative(SRC, file)} imports ${specifier}`),
  );
  expect(offenders).toEqual([]);
});
