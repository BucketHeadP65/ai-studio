"use client";

import { useQuery } from "@tanstack/react-query";
import type { ReactElement } from "react";

import { Drawer } from "@/shared/ui/drawer";

import { inspectionApi } from "@/api/inspection";
import { inspectionKeys } from "@/api/query-keys";
import { studioErrorMessage } from "@/api/studio";

import { TurnMessages } from "./turn-messages";

// One step of the run, read on demand: the messages it added, drawn exactly as the chat draws
// them. Opened from the timeline and from the span pane; `step` null keeps it closed.
export function StepDrawer({ runId, step, onClose }: { runId: string; step: number | null; onClose: () => void }): ReactElement {
  const activeStep = step ?? 0;
  const entry = useQuery({
    queryKey: inspectionKeys.step(runId, activeStep),
    queryFn: () => inspectionApi.step(runId, activeStep),
    enabled: step !== null,
  });

  return (
    <Drawer open={step !== null} title={`Step ${step} of the run`} onClose={onClose} className="w-[min(92vw,720px)]">
      <div className="h-full overflow-y-auto p-4">
        {entry.isError ? (
          <p role="alert" className="text-caption text-danger-ink">
            Could not read the step. {studioErrorMessage(entry.error)}
          </p>
        ) : !entry.data ? (
          <p className="text-body text-label-3">Reading step {step}.</p>
        ) : entry.data.messages.length === 0 ? (
          <p className="text-body text-label-3">This step added no messages.</p>
        ) : (
          <TurnMessages messages={entry.data.messages} />
        )}
      </div>
    </Drawer>
  );
}
