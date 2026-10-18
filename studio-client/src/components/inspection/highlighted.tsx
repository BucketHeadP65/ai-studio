import type { ReactElement, ReactNode } from "react";

// `text`, with every case-insensitive occurrence of any of `phrases` wrapped in a `mark`. The
// longest phrase wins at a position more than one could open at, and matches never overlap.

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function Highlighted({ text, phrases }: { text: string; phrases: string[] }): ReactElement {
  if (phrases.length === 0) return <>{text}</>;

  const pattern = new RegExp(
    [...phrases].sort((a, b) => b.length - a.length).map(escapeRegExp).join("|"),
    "gi",
  );
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) nodes.push(text.slice(cursor, index));
    key += 1;
    nodes.push(
      <mark key={key} className="bg-accent-soft text-inherit underline decoration-accent underline-offset-2">
        {match[0]}
      </mark>,
    );
    cursor = index + match[0].length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return <>{nodes}</>;
}
