import type { Trust, TrustCounts, VersionTrust } from "@/api/studio-types";

// Builders for the `trust` object the library card, the workflow route and the approve route
// answer. Every count starts at zero, every level at unverified, and a caller names only the
// fields its test reads.

// The current version's level and counts, each one optional.
export type CurrentTrust = Partial<TrustCounts> & { level?: string };

export function trustCounts(overrides: Partial<TrustCounts> = {}): TrustCounts {
  return {
    runs_with_evidence: 0,
    failed_runs: 0,
    approved_by: [],
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
    ...overrides,
  };
}

export function versionTrust(
  version: number,
  level: string,
  overrides: Partial<TrustCounts> = {},
): VersionTrust {
  return { version, level, ...trustCounts(overrides) };
}

// A workflow with one saved version: `total` and the one `versions` entry repeat its own counts.
export function singleVersionTrust(version: number, overrides: CurrentTrust = {}): Trust {
  const { level = "unverified", ...counts } = overrides;
  return {
    level,
    ...trustCounts(counts),
    total: trustCounts(counts),
    versions: [versionTrust(version, level, counts)],
  };
}

// Any trust object, the fields a test names laid over an unverified v1 with zero counts. A test
// of several versions names `total` and `versions` itself.
export function trust(overrides: Partial<Trust> = {}): Trust {
  return { ...singleVersionTrust(1), ...overrides };
}
