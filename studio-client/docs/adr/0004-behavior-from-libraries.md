---
id: studio-client-adr-0004-behavior-from-libraries
aliases: ["studio-client-adr-0004-behavior-from-libraries"]
type: adr
app: studio-client
status: accepted
tags: []
title: "0004 — Overlay, streaming-prose, and toast behavior come from libraries"
---

# 0004 — Overlay, streaming-prose, and toast behavior come from libraries

**Status:** Accepted (2026-08-28). Extends ADR-0003's
direction: libraries own interaction mechanics, the app owns every pixel and all flow.

## Context

The dashboard hand-rolled its overlay behavior: client-core's `ConfirmDialog`/`Drawer` and six app surfaces each carried an
Escape listener, a backdrop button, and (for the popover) DOM-containment outside-click detection — with no focus trap,
scroll lock, or return-focus anywhere. Chat prose went through react-markdown, which renders half-streamed syntax as
literal markers. There was no toast outlet.

## Decision

- **Overlays: radix behavior under our markup.** client-core's `ui/modal` (`Modal`, `ModalTitle`, `ModalDescription`) is
  the one centered-dialog primitive, built on `Dialog` from the consolidated `radix-ui` package (a client-core dependency;
  the consumer pins the same major): the panel is the dialog surface itself, so a backdrop press lands outside it and
  dismisses. `ConfirmDialog` and every app modal build on it. `Drawer` is the side panel on the same Dialog and takes
  `modal`: a non-modal drawer leaves the page live and dismisses on an outside pointer or focus, so anything it nests
  renders inside its own tree (the chat turn inspector, whose payload viewer is a modal drawer within it; a non-modal
  drawer also draws no scrim, the live page is not dimmed). The note surfaces, which own their pixel positioning, take
  their dismissal from `DismissableLayer` (`radix-ui/internal`): the thread views close on outside-press and Escape, the
  new-note composer on Escape only, so a stray click never discards a note being typed. Radix is headless: it renders
  nothing, so the tokens and classNames stay the app's own. Panels portal to `document.body`; outside-detection by DOM
  containment is therefore forbidden anywhere a portaled layer can open. The primitives record the opener themselves on
  open and hand focus back to it on close, since every open is controlled and radix never sees a trigger.
- **Streaming prose: streamdown.** `AgentMarkdown` (every conversation surface) renders through `Streamdown` — streaming
  block parsing, incomplete-markdown repair, shiki fences through the `@streamdown/code` plugin, link safety off so a
  link is a real anchor. streamdown owns its code-block and table chrome (header, copy control, capped height with an
  inner scrollbar); the wrapper keeps only width and overflow guards and the accent on links. The projects station's
  `MarkdownView` deliberately stays on react-markdown: its remark-level machinery (source-position stamps for note
  anchors, wikilinks, selection mapping) is a legitimate deep integration, and the surface is static. Two pipelines,
  split by "does the text stream".
- **Toasts: sonner, unstyled.** One `<Toaster>` in the root providers, `unstyled: true`, painted entirely with the app's
  tokens.

## Consequences

- An open modal now traps focus, locks scroll, hides the rest of the page from assistive tech, and pointer-locks the body.
  Tests interact with the page behind a dialog only through the dialog; radix arms its outside-pointer listener one tick
  after open and dismisses on the click after the pointerdown.
- New deps: `radix-ui` (client-core), `streamdown`, `sonner` (studio-client). highlight.js and react-markdown remain for
  `MarkdownView` only. `radix-ui/internal` is an internal entry point: re-check the two note surfaces on every radix
  upgrade.
- A modal hides the page behind it from assistive tech, so a failure that must be read from the page (the project rail's
  unregister alert) closes its confirm first.
- streamdown's dist ships Tailwind classes and is registered as an `@source` in `globals.css`, like client-core's copy;
  its `sidebar` token is aliased onto `well` next to the other shadcn names.
- A ref on an element inside a radix portal is still null in the effect of the commit that opens it: read such elements
  through callback-ref state and put that state in the effect's deps (the terminal pane).
