---
id: studio-client-adr-0003-ai-elements-vendored-components
aliases: ["studio-client-adr-0003-ai-elements-vendored-components"]
type: adr
app: studio-client
status: accepted
tags: []
title: "0003 — Work tool calls render through vendored AI Elements cards"
---

# 0003 — Work tool calls render through vendored AI Elements cards

**Status:** Accepted (2026-08-28).

## Context

ADR-0001 put the chat on the Vercel AI UIMessage wire, so every tool call the agent makes already reaches the client as a typed
part. The bubble drew only the companion tools (verbs, console acts) as one-line text and dropped the rest; the agent's real
work was invisible mid-turn. We wanted the coding-agent-surface treatment — tool cards with parameters and results — without
authoring or maintaining a component set of our own (no designer on the project), and without replacing the chat's
server-authoritative state (which ruled out assistant-ui's runtime, and ACP as a UI-less wire swap).

## Decision

- **Vendor Vercel AI Elements** (Apache-2.0) the shadcn way: copied source under `src/components/ui/` (badge, button,
  collapsible, select) and `src/components/ai-elements/` (tool, code-block), installed by `npx ai-elements add`, configured by
  `components.json`. `src/lib/utils.ts` re-exports client-core's `cn` so the vendored imports resolve to the app's own merge.
- **Theme by aliasing, never by editing.** shadcn's token names are registered in `globals.css` `@theme inline` as aliases onto
  the existing tier-1 variables, and Tailwind's `dark:` variant is rebound to the app's `data-theme` choice. The vendored files
  stay as shipped (repo formatting aside) so upstream re-pulls stay cheap.
- **The chat owns the mapping.** `work-tool.tsx` is the one seam between wire parts and Elements components; per-tool
  specializations land there, not in the vendored files. The seam draws the card's own one-row header over the vendored
  Collapsible (the vendored `ToolHeader` is not used), reads each call through `tool-presentation.ts` (kind, verb line,
  result summary), renders an edit as a unified diff (`diff-view.tsx`), and folds runs of same-kind calls
  (`work-groups.ts`). A live turn wears its own thinking mark at the foot of its bubble (`thinking-mark.tsx`: a turning
  glyph, a verb that moves on, the elapsed time, and the output tokens summed from the turn's `data-step` parts, one per
  model response on the wire and rebuilt per transcript row by `transcriptToUiMessages`); each card carries a live
  elapsed while running and, settled, its duration and its step's tokens (`step-usage.ts`). A settled turn keeps its
  steps open unless the Chat setting folds them. The conversation itself sits in the vendored `Conversation` (stick-to-bottom) rather than a
  scroll-into-view effect.

## Consequences

- Updates are re-pulls of copied source, not `npm update`; heavy edits to a vendored file mean owning it from then on. The
  vendored base is `ai-elements` 1.9.0 (Apache-2.0, per the npm registry at install time) — re-pulls diff against that.
- `bg-accent` is shared: shadcn's hover wash resolves to the app's signal hue (with `surface` as its ink), a deliberate
  loudness accepted to keep one accent token.
- Two aliasing gaps stay open by choice: the card's status icons use raw palette hues (`text-green-600` …) inside the vendored
  file, and bare `border` utilities take their color from a base-layer `border-color: var(--line)` rule in `globals.css`
  rather than per-class tokens.
- New deps: `radix-ui`, `shiki`, `class-variance-authority`, `tw-animate-css` — all component-layer, none touching chat state.
