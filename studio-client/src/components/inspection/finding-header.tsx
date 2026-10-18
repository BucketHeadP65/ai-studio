import { Route } from "lucide-react";
import Link from "next/link";
import type { ReactElement } from "react";

import type { Finding } from "@/api/inspection";
import { longDate, runLocator, spaced } from "@/api/inspection-words";

import { SYMPTOM_ICON } from "./icons";

// The finding's own header: the breadcrumb and its place in the list, the symptom as the title
// with the taxonomy's sentence under it, who found it and on which run, and the link to that
// run's own page.

export function FindingHeader({
  finding,
  position,
}: {
  finding: Finding;
  position: { index: number; total: number; prev: number | null; next: number | null } | null;
}): ReactElement {
  const locator = runLocator(finding.source);
  const runHref = `/inspection/runs/${locator}`;

  return (
    <div className="flex flex-col gap-3">
      <nav aria-label="Breadcrumb" className="flex h-8 items-center justify-between text-caption">
        <div className="flex items-center gap-2">
          <Link href="/inspection">Findings</Link>
          <span className="text-label-3">/</span>
          <span className="text-label-2">{spaced(finding.symptom)}</span>
        </div>
        {position ? (
          <div className="flex items-center gap-3.5 text-label-3">
            <span>
              {position.index} of {position.total}
            </span>
            {position.prev !== null ? <Link href={`/inspection/${position.prev}`}>prev</Link> : null}
            {position.next !== null ? <Link href={`/inspection/${position.next}`}>next</Link> : null}
          </div>
        ) : null}
      </nav>

      <div className="flex justify-between border-line border-b pb-3">
        <div className="flex gap-3">
          <SYMPTOM_ICON size={24} className="mt-1 shrink-0 text-label-1" />
          <div>
            <h1 className="font-semibold text-label-1 text-title">{spaced(finding.symptom)}</h1>
            <p className="mt-1 text-body text-label-2">{finding.symptom_text}</p>
            <p className="mt-1.5 text-caption text-label-3">
              {finding.found_by === "rule" ? (
                <>Found by the rule <span className="font-mono">{finding.lead_found_by}</span></>
              ) : finding.model === null ? (
                <>Found by the inspector on <span className="text-label-3">none</span></>
              ) : (
                <>Found by the inspector on <span className="font-mono">{finding.model}</span></>
              )}
              , {longDate(finding.decided_at)}, run{" "}
              <Link href={runHref} className="font-mono">
                {locator}
              </Link>
            </p>
          </div>
        </div>
        <Link
          href={runHref}
          className="inline-flex h-7 items-center gap-1.5 rounded-md border border-line-strong bg-surface px-2.5 text-caption text-label-1 hover:bg-well"
        >
          <Route size={14} />
          Open run
        </Link>
      </div>
    </div>
  );
}
