"use client";

import { EmptyState } from "client-core/ui/empty-state";
import { ScanSearch } from "lucide-react";
import Link from "next/link";
import type { ReactElement } from "react";

import type { Finding } from "@/api/inspection";
import { runLocator, runName, shortDate, spaced } from "@/api/inspection-words";
import { SurfaceChip } from "./surface-chip";

// The findings list: one row per finding, newest first, its symptom naming the row and opening the finding page.

const CELL = "px-3 py-2 text-left align-top";

export function FindingsTable({ findings, loading }: { findings: Finding[]; loading: boolean }): ReactElement {
  if (loading) {
    return <p className="text-body text-label-3">Reading the findings.</p>;
  }
  if (findings.length === 0) {
    return <EmptyState icon={<ScanSearch size={20} />} title="Nothing found yet." hint="A watch has to run before there is anything here." />;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-line bg-surface">
      <table className="w-full text-body">
        <caption className="sr-only">Findings</caption>
        <thead className="border-line border-b bg-well text-caption text-label-3">
          <tr>
            <th className={CELL}>Symptom</th>
            <th className={CELL}>Run</th>
            <th className={CELL}>Found by</th>
            <th className={CELL}>Model</th>
            <th className={CELL}>Decided</th>
            <th className={CELL}>Blame surfaces</th>
            <th className={CELL}>Question</th>
          </tr>
        </thead>
        <tbody>
          {findings.map((found) => {
            const { agent, short } = runName(runLocator(found.source));
            return (
              <tr key={found.finding_id} className="border-line border-b last:border-b-0 hover:bg-well">
                <td className={CELL}>
                  <Link href={`/inspection/${found.finding_id}`} className="font-medium text-label-1">
                    {spaced(found.symptom)}
                  </Link>
                  <p className="text-caption text-label-3">{found.symptom_text}</p>
                </td>
                <td className={CELL}>
                  <div className="whitespace-nowrap">{agent}</div>
                  {short === "" ? null : <div className="font-mono text-caption text-label-3">{short}</div>}
                </td>
                <td className={CELL}>
                  {found.found_by === "rule" ? (
                    <>
                      <div>rule</div>
                      {found.lead_found_by === null ? null : (
                        <span className="font-mono text-caption text-label-3">{found.lead_found_by}</span>
                      )}
                    </>
                  ) : (
                    found.found_by
                  )}
                </td>
                <td className={CELL}>
                  {found.model === null ? (
                    <span className="text-label-3">none</span>
                  ) : (
                    <span className="whitespace-nowrap font-mono text-caption">{found.model}</span>
                  )}
                </td>
                <td className={`${CELL} text-caption text-label-3`}>{shortDate(found.decided_at)}</td>
                <td className={CELL}>
                  <div className="flex flex-wrap gap-1">
                    {found.blames.map((blame, index) => (
                      <SurfaceChip key={index} blame={blame} />
                    ))}
                  </div>
                </td>
                <td className={CELL}>
                  {found.question_id === null ? (
                    <span className="text-label-3">none</span>
                  ) : (
                    <Link href={`/inspection/${found.finding_id}#trace`}>Read</Link>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
