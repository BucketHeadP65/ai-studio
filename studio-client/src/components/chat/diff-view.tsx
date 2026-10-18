import type { ReactElement } from "react";

import { editChanges } from "./tool-presentation";

// A unified diff of two text blobs, one row per line with both line-number columns, a
// change marker, and the line's text. Removed lines dim, added lines brighten, context
// lines stay plain — the reading order the app's other diff surfaces (the turn inspector)
// do not yet share.

interface DiffRow {
  key: number;
  oldNo: number | null;
  newNo: number | null;
  marker: "+" | "−" | "";
  text: string;
  kind: "added" | "removed" | "context";
}

function splitLines(value: string): string[] {
  const lines = value.split("\n");
  if (lines.at(-1) === "") {
    lines.pop();
  }
  return lines;
}

function buildRows(oldText: string, newText: string): DiffRow[] {
  const rows: DiffRow[] = [];
  let oldNo = 1;
  let newNo = 1;
  for (const change of editChanges(oldText, newText)) {
    for (const text of splitLines(change.value)) {
      if (change.added) {
        rows.push({
          key: rows.length,
          oldNo: null,
          newNo: newNo++,
          marker: "+",
          text,
          kind: "added",
        });
      } else if (change.removed) {
        rows.push({
          key: rows.length,
          oldNo: oldNo++,
          newNo: null,
          marker: "−",
          text,
          kind: "removed",
        });
      } else {
        rows.push({
          key: rows.length,
          oldNo: oldNo++,
          newNo: newNo++,
          marker: "",
          text,
          kind: "context",
        });
      }
    }
  }
  return rows;
}

const ROW_CLASS: Record<DiffRow["kind"], string> = {
  removed: "bg-danger-soft/60 text-label-3",
  added: "bg-accent-soft text-label-1",
  context: "text-label-2",
};

const MARKER_CLASS: Record<DiffRow["kind"], string> = {
  removed: "text-danger-ink",
  added: "text-accent-ink",
  context: "",
};

export function DiffView({ oldText, newText }: { oldText: string; newText: string }): ReactElement {
  const rows = buildRows(oldText, newText);
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse">
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className={ROW_CLASS[row.kind]}>
              <td className="select-none pr-2 text-right font-mono text-caption">
                {row.oldNo ?? ""}
              </td>
              <td className="select-none pr-2 text-right font-mono text-caption">
                {row.newNo ?? ""}
              </td>
              <td className={`select-none pr-2 font-mono text-caption ${MARKER_CLASS[row.kind]}`}>
                {row.marker}
              </td>
              <td className="font-mono text-caption whitespace-pre-wrap break-words">{row.text}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
