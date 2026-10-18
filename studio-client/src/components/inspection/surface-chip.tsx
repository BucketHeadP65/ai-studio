import type { ReactElement } from "react";

import type { Blame } from "@/api/inspection";
import { surfaceLabel } from "@/api/inspection-words";

// Where a fix lands, as one chip: the surface kind in words, the surface's own name in mono when it has one.
export function SurfaceChip({ blame }: { blame: Pick<Blame, "surface_kind" | "surface_name"> }): ReactElement {
  return (
    <span className="inline-flex h-5 items-center gap-1 whitespace-nowrap rounded border border-line bg-well px-1.5 text-caption text-label-2">
      {surfaceLabel(blame.surface_kind)}
      {blame.surface_name === "" ? null : <span className="font-mono text-label-1">{blame.surface_name}</span>}
    </span>
  );
}
