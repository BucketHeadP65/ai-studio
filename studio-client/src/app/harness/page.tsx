"use client";

import { useState } from "react";

import type { HarnessArtifact, HarnessKind } from "@/api/harness-types";
import { AgentConsole } from "@/components/harness/agent-console";
import { CompositionCard } from "@/components/harness/composition";
import { HarnessKindSection } from "@/components/harness/kind-section";
import { ModelTiers } from "@/components/harness/model-tiers";
import { PluginsCard } from "@/components/harness/plugins-card";
import { ProfileCard } from "@/components/harness/profile-card";
import { Drawer } from "@/shared/ui/drawer";
import { Input } from "@/shared/ui/input";

// The control room: the agent's setup IS the page. The live composition and its
// plugins up top, then the three authored shelves, full width, with the owner's acts
// inline. The agent itself is one button away in a drawer -- the same conversation
// as the Studio rail -- and any row can summon it pre-scoped ("Discuss") so quick
// chat and edit-with-the-agent are the same drawer with a different opening line.

const KIND_WORD: Record<HarnessKind, string> = {
  capability: "capability",
  step: "script",
  agent: "agent",
  profile: "profile",
};

export default function HarnessPage() {
  const [filter, setFilter] = useState("");
  const [drawer, setDrawer] = useState<{ seed: string } | null>(null);

  function discuss(kind: HarnessKind) {
    return (artifact: HarnessArtifact) =>
      setDrawer({
        seed: `About the ${KIND_WORD[kind]} ${artifact.name} (${artifact.descriptor}): `,
      });
  }

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-3 pb-6">
      <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="font-semibold text-label-1 text-title">Harness</h1>
          <p className="text-caption text-label-3">
            the agent's setup: plugins, model, and everything it wrote for itself, under your review
          </p>
        </div>
      </header>

      <CompositionCard />
      <ModelTiers />
      <ProfileCard />
      <PluginsCard />

      <div className="mt-1 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-medium text-caption text-label-3 uppercase tracking-wider">
          Custom items
        </h2>
        <Input
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="Filter by name or descriptor..."
          aria-label="Filter custom items"
          className="h-8 w-72 max-w-full py-0 text-caption"
        />
      </div>

      <HarnessKindSection
        kind="capability"
        title="Capabilities"
        blurb="Capabilities the agent wrote for itself, activate to try one, promote to make it standing."
        empty="Nothing here yet."
        emptyHint="Ask the agent to craft one, it lands here pending your review."
        filter={filter}
        onDiscuss={discuss("capability")}
      />
      <HarnessKindSection
        kind="step"
        title="Scripts"
        blurb="Registered functions workflow steps call, the building blocks of the library."
        empty="Nothing here yet."
        emptyHint="Ask the agent to craft one. Activated scripts load on the next process start."
        filter={filter}
        onDiscuss={discuss("step")}
      />
      <HarnessKindSection
        kind="agent"
        title="Agents"
        blurb="Declarative agents, a model and instructions written as data."
        empty="Nothing here yet."
        emptyHint="Ask the agent to draft one, it lands here pending your review."
        filter={filter}
        onDiscuss={discuss("agent")}
      />
      <HarnessKindSection
        kind="profile"
        title="Profiles"
        blurb="Named sets of plugins, custom items, and instructions. Activate one, then select it above."
        empty="Nothing here yet."
        emptyHint="Ask the agent to craft one, it lands here pending your review."
        filter={filter}
        onDiscuss={discuss("profile")}
      />

      <Drawer open={drawer !== null} title="The agent" onClose={() => setDrawer(null)}>
        {drawer !== null ? <AgentConsole seed={drawer.seed} /> : null}
      </Drawer>
    </div>
  );
}
