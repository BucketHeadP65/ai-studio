"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card } from "client-core/ui/card";
import { Select } from "client-core/ui/select";
import { useState } from "react";

import { harnessApi } from "@/api/harness";
import { studioErrorMessage } from "@/api/studio";

// Which profile governs the composition. The picker offers the default (the
// hand-set plugins) plus every ACTIVE profile -- a pending one never appears,
// because offering it would promise a selection the backend refuses. Switching
// is refused mid-conversation; the backend's sentence names the fix and it is
// shown verbatim.
export function ProfileCard() {
  const queryClient = useQueryClient();
  const [switchError, setSwitchError] = useState<string | null>(null);

  const selection = useQuery({
    queryKey: ["harness", "selection"],
    queryFn: () => harnessApi.selection(),
  });
  const profiles = useQuery({
    queryKey: ["harness", "profile"],
    queryFn: () => harnessApi.list("profile"),
  });

  const choose = useMutation({
    mutationFn: (name: string | null) => harnessApi.select(name),
    onMutate: () => setSwitchError(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["harness"] });
    },
    onError: (error) => setSwitchError(studioErrorMessage(error)),
  });

  const active = (profiles.data?.artifacts ?? []).filter((row) => row.status === "active");
  const chosen = selection.data?.name ?? null;
  const chosenRow = active.find((row) => row.name === chosen) ?? null;

  return (
    <Card className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <h2 className="font-medium text-label-1 text-sm">Profile</h2>
        <Select
          aria-label="Profile"
          value={chosen ?? ""}
          onChange={(event) => choose.mutate(event.target.value === "" ? null : event.target.value)}
          disabled={choose.isPending || selection.isPending}
          className="h-7 py-0"
        >
          <option value="">default (hand-set plugins)</option>
          {active.map((row) => (
            <option key={row.name} value={row.name}>
              {row.name}
            </option>
          ))}
        </Select>
        <span className="min-w-0 flex-1 truncate text-caption text-label-3">
          {chosen !== null
            ? `${chosen} governs the plugins and the custom items. ${chosenRow?.descriptor ?? ""}`
            : "No profile selected. The plugins below are yours to hand-toggle."}
        </span>
      </div>
      <p className="mt-1 text-label-3 text-micro">
        A profile is a named set of plugins, custom items, and standing instructions, chosen whole.
        Activate one in the Profiles list below, then select it here. Switching waits for a forgotten
        conversation.
      </p>
      {switchError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {switchError}
        </p>
      ) : null}
    </Card>
  );
}
