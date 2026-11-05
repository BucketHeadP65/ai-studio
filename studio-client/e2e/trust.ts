import type { Trust, TrustCounts } from "@/api/studio-types";

// The `trust` object a route answers for a workflow with one saved version. `total` and the one
// `versions` entry repeat its own counts, every count starts at zero and the level at unverified.
export function singleVersionTrust(
  version: number,
  overrides: Partial<TrustCounts> & { level?: string } = {},
): Trust {
  const { level = "unverified", ...named } = overrides;
  const counts: TrustCounts = {
    runs_with_evidence: 0,
    failed_runs: 0,
    approved_by: [],
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
    ...named,
  };
  return { level, ...counts, total: counts, versions: [{ version, level, ...counts }] };
}
