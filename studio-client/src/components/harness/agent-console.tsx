"use client";

import { useQueryClient } from "@tanstack/react-query";

import { Console } from "@/components/conversation/console";
import { useConversation } from "@/components/conversation/use-conversation";

// THE studio agent with its self-authoring tools loaded, summoned into the control
// room's drawer. You tell it what to craft or change, it acts, and the shelves
// refresh the moment the turn settles. Same conversation as the Studio station's
// rail: one agent, one memory, two rooms. A `seed` pre-fills the composer when the
// drawer was opened scoped to one artifact.

const CRAFT_STARTERS = [
  {
    label: "Craft a capability",
    prompt:
      "Author a new capability for yourself. First propose what it should do given the tools you have, then write it with write_custom_capability.",
  },
  {
    label: "Craft a script",
    prompt:
      "Author a new custom step: a small, typed async function a workflow could wire in. Propose it first, then write it with write_custom_step.",
  },
  {
    label: "Craft an agent",
    prompt:
      "Draft a declarative agent document (model, instructions, named capabilities) for a task you think this Studio needs, using write_custom_agent.",
  },
  {
    label: "Craft a profile",
    prompt:
      "Author a profile with write_custom_profile: a YAML set of plugins, custom items to hold active, and standing instructions, specialized for one type of work you think this Studio does often.",
  },
] as const;

export function AgentConsole({ seed }: { seed?: string }) {
  const queryClient = useQueryClient();
  const conversation = useConversation({
    // The agent may have authored or disabled mid-turn: refresh every shelf beside this drawer.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["harness"] }),
  });

  return (
    <Console
      title="The agent"
      turns={conversation.turns}
      thinking={conversation.thinking}
      live={conversation.live}
      error={conversation.error}
      onSend={conversation.send}
      sendDisabled={!conversation.ready}
      onForget={conversation.forget}
      forgetLabel="Forget"
      forgetDisabled={conversation.forgetPending}
      placeholder="Tell the agent what to craft or change..."
      statusText={{ busy: "crafting...", idle: "ready" }}
      seed={seed}
      emptyState={
        <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
          <p className="max-w-sm text-body text-label-3">
            This is the Studio's own agent with its self-authoring tools loaded. Ask it to craft a
            capability, a script, or an agent. What it writes lands among the custom items, pending
            your review.
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            {/* The starters sit in the empty state, which is also what shows while the
                conversation read is in flight, so each one waits for the session the turn
                would be sent to. */}
            {CRAFT_STARTERS.map((starter) => (
              <button
                key={starter.label}
                type="button"
                disabled={!conversation.ready}
                onClick={() => conversation.send(starter.prompt)}
                className="rounded-md border border-line bg-well px-2.5 py-1.5 text-caption text-label-2 transition-colors hover:border-line-strong hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
              >
                {starter.label}
              </button>
            ))}
          </div>
        </div>
      }
    />
  );
}
