"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { cn } from "@/shared/lib/cn";
import { Card } from "@/shared/ui/card";
import { useState } from "react";

import { harnessApi } from "@/api/harness";
import { studioErrorMessage } from "@/api/studio";

// The agent's built-in setup, honest about what can move: every functional plugin
// wears a switch (a flip reaches the very next turn, no restart), and the always-on set is
// listed read-only because the runtime refuses to toggle it -- the UI never promises
// control the runtime does not have.
export function PluginsCard() {
  const queryClient = useQueryClient();
  const [flipError, setFlipError] = useState<string | null>(null);

  const plugins = useQuery({
    queryKey: ["harness", "plugins"],
    queryFn: () => harnessApi.plugins(),
  });
  const selection = useQuery({
    queryKey: ["harness", "selection"],
    queryFn: () => harnessApi.selection(),
  });
  const governed = selection.data?.name ?? null;

  const flip = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      harnessApi.setPlugin(name, enabled),
    onMutate: () => setFlipError(null),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["harness", "plugins"] }),
    onError: (error) => setFlipError(studioErrorMessage(error)),
  });

  const toggleable = plugins.data?.plugins.filter((plugin) => !plugin.fixed) ?? [];
  const alwaysOn = plugins.data?.plugins.filter((plugin) => plugin.fixed) ?? [];

  return (
    <Card className="px-4 py-3">
      <h2 className="font-medium text-label-1 text-sm">Plugins</h2>
      <p className="mt-0.5 text-caption text-label-3">
        {governed !== null
          ? `The plugins follow the profile ${governed}. Clear the selection above to hand-toggle.`
          : "What the agent loads by default. A flip takes effect on the very next turn."}
      </p>

      {plugins.isPending ? <p className="mt-2 text-caption text-label-3">Loading...</p> : null}
      {plugins.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {studioErrorMessage(plugins.error)}
        </p>
      ) : null}
      {flipError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {flipError}
        </p>
      ) : null}

      {toggleable.length > 0 ? (
        <ul className="mt-2 grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
          {toggleable.map((plugin) => (
            <li key={plugin.name} className="flex items-center gap-2.5 py-0.5">
              <PluginSwitch
                name={plugin.name}
                checked={plugin.enabled}
                busy={flip.isPending || governed !== null}
                onFlip={() => flip.mutate({ name: plugin.name, enabled: !plugin.enabled })}
              />
              <span className="min-w-0">
                <span
                  className={cn(
                    "block font-mono text-caption",
                    plugin.enabled ? "text-label-1" : "text-label-3",
                  )}
                >
                  {plugin.name}
                </span>
                <span className="block truncate text-label-3 text-micro" title={plugin.description}>
                  {plugin.description}
                </span>
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {alwaysOn.length > 0 ? (
        <details className="mt-3 border-line border-t pt-2">
          <summary className="cursor-pointer select-none text-label-3 text-micro uppercase tracking-wider">
            always on
          </summary>
          <ul className="mt-1.5 grid gap-x-6 gap-y-1 sm:grid-cols-2">
            {alwaysOn.map((plugin) => (
              <li key={plugin.name} className="min-w-0 py-0.5">
                <span className="block font-mono text-caption text-label-2">{plugin.name}</span>
                <span className="block truncate text-label-3 text-micro" title={plugin.description}>
                  {plugin.description}
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </Card>
  );
}

function PluginSwitch({
  name,
  checked,
  busy,
  onFlip,
}: {
  name: string;
  checked: boolean;
  busy: boolean;
  onFlip: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={name}
      disabled={busy}
      onClick={onFlip}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-60",
        checked ? "bg-accent" : "border border-line bg-muted-soft",
      )}
    >
      <span
        className={cn(
          "absolute top-0.5 left-0.5 h-4 w-4 rounded-full bg-surface shadow-sm transition-transform",
          checked && "translate-x-4",
        )}
      />
    </button>
  );
}
