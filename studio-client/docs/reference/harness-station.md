---
id: studio-client-harness-station
aliases: ["studio-client-harness-station"]
type: reference
app: studio-client
status: current
tags: [harness, plugins, self-authoring, interface-contract]
title: "Harness station: interface contract"
---

# Harness station: interface contract

The first of the per-station interface contracts: one page saying what the station is for, what its states are, and what each
control is legal to do when. A UI slice on a station starts by updating its contract. Merge review walks the page against it.
The standing rule, adopted from the deepseek-harness study: **nothing in the UI promises control the runtime does not actually
have.**

## Purpose

The control room for the studio agent. The owner comes here to answer one question and act on it: *what does my agent load, and
is that what I want?* The setup is the page. The agent itself is a drawer summoned on demand, never the centerpiece.

## Layout, top to bottom

1. **Header**: h1 `Harness`, one-line purpose, and the `Ask the agent` button (opens the drawer with an empty composer).
2. **Live composition** (card): read-only facts: model (a working select when the deployment declares more than one choice, with
   a `reset` action only while a non-default selection is stored), run engine, active-custom counts, scripts registered,
   promoted capabilities.
3. **Profile** (card): which profile governs: a select offering the default (hand-set plugins) plus every ACTIVE profile, never a
   pending one. Switching is refused mid-conversation (409) and the backend's sentence shows verbatim. While a profile governs, the
   plugins card says so and its switches stand down.
4. **Plugins** (card): the built-in setup. Toggleable plugins render as switches (`role="switch"`, named by the plugin). A flip takes
   effect on the very next turn and the card says so. Always-on plugins (spend limits, loop detection, output limits, step persistence,
   instructions, code mode, workflows) are listed under `always on` with no control, because the runtime refuses to toggle them.
5. **Custom listings** (four cards: Capabilities, Scripts, Agents, Profiles): one row per custom item: name, status badge,
   `promoted`/`broken` badges where they apply, descriptor, then the acts.

## Control legality (per row)

- `Activate`: only on a disabled row. Primary when the listing names it as the next step. Disabled while the row is broken.
- `Disable`: only on an active row.
- `Promote` / `Demote`: capability rows only, per the promotion state the listing carries.
- `Delete`: only on a **disabled** row, behind a confirm dialog. The composition never changes as a side effect of tidying a
  listing. The backend refuses an active delete with the fix named, and the UI simply never offers it.
- `Discuss`: every row. Opens the drawer with the composer seeded `About the <kind> <name> (<descriptor>): `, ready to edit.
  Quick chat and edit-with-the-agent are the same drawer with a different opening line.

## States

- **Empty listing**: `EmptyState` with the standing copy `Nothing authored yet.` plus a kind-specific hint naming the authoring
  tool. An empty listing still shows a running agent above it, never a blank page.
- **Broken custom item**: a `broken` badge plus the stored validation error verbatim under the row. Activate disabled. Delete
  available (a broken row is disabled by definition).
- **Backend unreachable / refusal**: every card surfaces its own `role="alert"` with the backend's sentence. One failing card
  never blanks the others.
- **Filter with no match**: `No <kind> matches the filter.` in place of the rows.

## Vocabulary

agent, workflow, capability, **script** (never "leaf" in copy, as the code does), plugin,
always on, listing (only ever the custom-item listings here). The drawer's agent is "the agent", the same one everywhere.

## Wire surface

`/api/harness/composition`, `/api/harness/tiers` (GET), `/api/harness/tiers/{tier}` (PUT/DELETE), `/api/harness/models?q=` (search),
`/api/harness/selection` (GET/PUT), `/api/harness/plugins` (+ `/{name}/enable|disable`), `/api/harness/{kind}` (+ `/{name}/activate|disable`,
`DELETE /{name}`, capability `promote|demote`), and the shared `/api/conversation` plus `POST /api/sessions/{id}/messages` (the Vercel
AI v6 data stream. The settled reply rides the `data-settle` chunk) behind the drawer. CLI parity:
`harness list|activate|disable|delete|plugins|profile|promote|demote`.
