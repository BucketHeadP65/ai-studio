"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "client-core/ui/button";
import { Card } from "client-core/ui/card";
import { useState } from "react";

import { harnessApi } from "@/api/harness";
import type { ModelEntry, Thinking, Tier, TierView } from "@/api/harness-types";
import { studioErrorMessage } from "@/api/studio";
import type { RadioCardOption } from "@/components/workflows/radio-card";
import { RadioCardGroup } from "@/components/workflows/radio-card";

import { Fact } from "./composition";
import { ModelPicker, formatContext, formatMoney } from "./model-picker";

// The three tiers the harness routes work onto: the orchestrator that plans and
// reviews, the worker that does ordinary implementation, and the helper that runs
// mechanical single-file jobs. Each is one owner-set model and one thinking level,
// stored per-request in the tier store, so a change here reaches the very next turn
// with no restart.

const TIER_TITLE: Record<Tier, string> = {
  orchestrator: "Orchestrator",
  worker: "Worker",
  helper: "Helper",
};

const THINKING_OPTIONS: readonly RadioCardOption<Thinking>[] = [
  { value: "low", label: "Low", short: "Low" },
  { value: "medium", label: "Medium", short: "Med" },
  { value: "high", label: "High", short: "High" },
  { value: "xhigh", label: "Maximum", short: "Max" },
];

export function ModelTiers() {
  const tiers = useQuery({ queryKey: ["harness", "tiers"], queryFn: () => harnessApi.tiers() });

  return (
    <div className="flex flex-col gap-3">
      {tiers.isPending ? (
        <Card className="px-4 py-3">
          <p className="text-caption text-label-3">Loading...</p>
        </Card>
      ) : null}
      {tiers.isError ? (
        <Card className="px-4 py-3">
          <p
            role="alert"
            className="rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
          >
            {studioErrorMessage(tiers.error)}
          </p>
        </Card>
      ) : null}
      {tiers.data ? tiers.data.tiers.map((view) => <TierCard key={view.tier} view={view} />) : null}
    </div>
  );
}

function TierCard({ view }: { view: TierView }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);

  const settle = {
    onSuccess: () => {
      setChanged(true);
      queryClient.invalidateQueries({ queryKey: ["harness", "tiers"] });
      queryClient.invalidateQueries({ queryKey: ["harness", "composition"] });
    },
    onError: (mutationError: unknown) => setError(studioErrorMessage(mutationError)),
  };
  const setTier = useMutation({
    mutationFn: (body: { model: string; thinking: Thinking }) =>
      harnessApi.setTier(view.tier, body),
    onMutate: () => setError(null),
    ...settle,
  });
  const resetTier = useMutation({
    mutationFn: () => harnessApi.resetTier(view.tier),
    onMutate: () => setError(null),
    ...settle,
  });

  const busy = setTier.isPending || resetTier.isPending;

  function pickModel(entry: ModelEntry) {
    setTier.mutate({ model: entry.id, thinking: view.thinking });
  }

  function changeThinking(level: Thinking) {
    setTier.mutate({ model: view.model.id, thinking: level });
  }

  return (
    <Card className="px-4 py-3">
      <h2 className="font-medium text-label-1 text-sm">{TIER_TITLE[view.tier]}</h2>
      <p className="mt-0.5 text-caption text-label-2">{view.role}</p>

      <div className="mt-2 flex flex-wrap items-center gap-x-6 gap-y-1">
        <Fact label="name" value={view.model.name} />
        <Fact label="route" value={view.model.route} />
        <Fact label="context" value={formatContext(view.model.context_length)} />
        <Fact label="price in" value={formatMoney(view.model.price_in_per_m)} />
        <Fact label="price out" value={formatMoney(view.model.price_out_per_m)} />
      </div>

      <div className="mt-3 max-w-sm">
        <ModelPicker
          label={`${TIER_TITLE[view.tier]} model`}
          value={view.model}
          onPick={pickModel}
          disabled={busy}
        />
      </div>

      <div className="mt-3">
        <RadioCardGroup
          name={`thinking-${view.tier}`}
          legend="Thinking"
          value={view.thinking}
          onChange={changeThinking}
          options={THINKING_OPTIONS}
          disabled={busy}
          variant="segmented"
        />
      </div>

      <div className="mt-3 flex items-center gap-3">
        <Button
          variant="secondary"
          onClick={() => resetTier.mutate()}
          disabled={busy || view.default}
        >
          Reset to default
        </Button>
        {changed ? (
          <span className="text-caption text-label-3">In effect on the next turn.</span>
        ) : null}
      </div>

      {error !== null ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {error}
        </p>
      ) : null}
    </Card>
  );
}
