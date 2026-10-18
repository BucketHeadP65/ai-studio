import type { Finding } from "@/api/inspection";
import { plural, runLocator } from "@/api/inspection-words";

// What the list narrows by. Empty strings and 0 days mean All.
export interface FindingsFilters {
  symptom: string;
  run: string;
  foundBy: string;
  days: number;
}

const DAY_MS = 24 * 60 * 60 * 1000;

export function filterFindings(findings: Finding[], filters: FindingsFilters, now: Date = new Date()): Finding[] {
  const since = filters.days > 0 ? now.getTime() - filters.days * DAY_MS : null;
  return findings.filter(
    (row) =>
      (filters.symptom === "" || row.symptom === filters.symptom) &&
      (filters.run === "" || runLocator(row.source) === filters.run) &&
      (filters.foundBy === "" || row.found_by === filters.foundBy) &&
      (since === null || new Date(row.decided_at).getTime() >= since),
  );
}

export function countLine(findings: Finding[], now: Date = new Date()): string {
  const lastDay = findings.filter((row) => now.getTime() - new Date(row.decided_at).getTime() <= DAY_MS).length;
  const runs = new Set(findings.map((row) => row.source)).size;
  return `${plural(findings.length, "finding", "findings")}, ${lastDay} in the last day, ${plural(runs, "run", "runs")}`;
}
