"use client";

import { useQuery } from "@tanstack/react-query";

import { harnessApi } from "@/api/harness";
import { studioErrorMessage } from "@/api/studio";
import { Card } from "@/shared/ui/card";

// The live half of the harness story: what this process is actually running right now.
// The plugins and the custom shelves below are the knobs; this card is the world those
// settings compose, so an empty shelf still shows a running agent, not a blank page.
export function CompositionCard() {
  const composition = useQuery({
    queryKey: ["harness", "composition"],
    queryFn: () => harnessApi.composition(),
  });

  return (
    <Card className="px-4 py-3">
      <h2 className="font-medium text-label-1 text-sm">Live composition</h2>
      <p className="mt-0.5 text-caption text-label-3">
        What the agent is running right now. Plugins and activated custom items join it here.
      </p>

      {composition.isPending ? <p className="mt-2 text-caption text-label-3">Loading...</p> : null}
      {composition.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {studioErrorMessage(composition.error)}
        </p>
      ) : null}

      {composition.data ? (
        <div className="mt-2 flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1">
            <Fact
              label="run engine"
              value={composition.data.run_engine === "temporal" ? "temporal" : "in process"}
            />
            <Fact
              label="active custom items"
              value={`${composition.data.active.capability} capabilities, ${composition.data.active.step} scripts, ${composition.data.active.agent} agents`}
            />
          </div>
          <NameRow
            label="Scripts registered"
            names={composition.data.steps_registered}
            empty="none registered, activated scripts become callable on the next process start"
          />
          <NameRow
            label="Promoted capabilities"
            names={composition.data.promoted}
            empty="none yet, promote an active capability below to make it declarable by name"
          />
        </div>
      ) : null}
    </Card>
  );
}

export function Fact({ label, value }: { label: string; value: string }) {
  return (
    <span className="text-caption text-label-3">
      {label} <span className="font-medium text-body text-label-2 tabular-nums">{value}</span>
    </span>
  );
}

function NameRow({ label, names, empty }: { label: string; names: string[]; empty: string }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
      <span className="text-caption text-label-3">{label}</span>
      {names.length === 0 ? (
        <span className="text-caption text-label-3 italic">{empty}</span>
      ) : (
        names.map((name) => (
          <span
            key={name}
            className="rounded bg-muted-soft px-1.5 py-0.5 font-mono text-muted-ink text-xs"
          >
            {name}
          </span>
        ))
      )}
    </div>
  );
}
