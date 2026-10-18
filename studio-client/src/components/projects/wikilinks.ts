import type { FileEntry } from "@/api/projects";

// A rendered wikilink's href is the only thing that survives from the resolver to the
// click that follows it: MarkdownView puts the resolved href on the anchor, and a
// delegated click handler reads the project-relative path back out of it. The scheme
// keeps the anchor inert if the click handler ever misses it.
const HREF_PREFIX = "#file:";

function basenameOf(path: string): string {
  const index = path.lastIndexOf("/");
  return index === -1 ? path : path.slice(index + 1);
}

/**
 * The project file a `[[target]]` points at: an exact relative path first, then a
 * unique basename, each tried with and without the `.md` a vault leaves off. An
 * unknown or ambiguous target is `null`, which the view renders as a dead link.
 */
export function resolveWikilinkTarget(entries: FileEntry[], target: string): string | null {
  const wanted = target.trim();
  if (wanted === "") return null;

  const candidates = [wanted, `${wanted}.md`];
  const files = entries.filter((entry) => entry.kind === "file");

  const exact = files.find((entry) => candidates.includes(entry.path));
  if (exact) return exact.path;

  const byName = files.filter((entry) => candidates.includes(basenameOf(entry.path)));
  return byName.length === 1 ? byName[0].path : null;
}

/** The href a resolved wikilink carries; `null` in stays `null` out, which renders dead. */
export function wikilinkHref(path: string | null): string | null {
  return path === null ? null : `${HREF_PREFIX}${encodeURIComponent(path)}`;
}

/** The project-relative path inside a wikilink href, or `null` for any other href. */
export function pathFromWikilinkHref(href: string | null): string | null {
  if (href === null || !href.startsWith(HREF_PREFIX)) return null;
  return decodeURIComponent(href.slice(HREF_PREFIX.length));
}
