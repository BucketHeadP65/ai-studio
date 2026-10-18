"use client";

import type { ReactElement } from "react";

import type { Readiness, ReadinessField, ReadinessFinding } from "@/api/studio-types";

// What the backend says a workflow still lacks, shown in the backend's own words. Nothing here
// judges a workflow or composes a sentence: the list is whatever reading came off the wire, and a
// reading nobody took (`null`) draws nothing at all. An error is what refuses a save; a warning is
// advice about a workflow that may still be saved, so the two never look the same.

function findings(
  readiness: Readiness | null | undefined,
  fields?: readonly ReadinessField[],
): ReadinessFinding[] {
  if (!readiness) return [];
  return fields === undefined
    ? readiness.findings
    : readiness.findings.filter((finding) => fields.includes(finding.field));
}

// The one line that belongs under the box it names. Renders nothing when that field is clean.
export function FieldFinding({
  readiness,
  field,
}: {
  readiness: Readiness | null | undefined;
  field: ReadinessField;
}): ReactElement | null {
  const found = findings(readiness, [field])[0];
  if (found === undefined) return null;
  return (
    <p
      className={
        found.severity === "error" ? "text-caption text-danger-ink" : "text-caption text-warn-ink"
      }
    >
      {found.message}
    </p>
  );
}

// Everything left over, as a list. `exclude` drops the fields a form already shows beside a box,
// so a finding is never read twice on one screen.
export function ReadinessNotice({
  readiness,
  exclude = [],
}: {
  readiness: Readiness | null | undefined;
  exclude?: readonly ReadinessField[];
}): ReactElement | null {
  const rest = findings(readiness).filter((finding) => !exclude.includes(finding.field));
  if (rest.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1">
      {rest.map((finding) => (
        <li
          key={`${finding.field}:${finding.message}`}
          className={
            finding.severity === "error"
              ? "text-caption text-danger-ink"
              : "text-caption text-warn-ink"
          }
        >
          {finding.message}
        </li>
      ))}
    </ul>
  );
}
