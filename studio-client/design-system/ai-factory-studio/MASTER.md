# AI Factory Studio, design system

> **LOGIC:** When building a specific page, first check `design-system/ai-factory-studio/pages/[page-name].md`.
> If that file exists, its rules override this file. If not, follow the rules below.

## Identity

This language is Wire. It exists so a person can read a workflow run at a glance and never wonder
who or what made a decision. Its rule: shape carries what kind of thing a node is, fill carries
whether it ran, and a word carries everything else. Color alone never carries meaning.

## Fonts

Two faces, one rule. **Space Grotesk** sets everything a person wrote or the product says:
sentences, button labels, headers, pill words. **DM Mono** sets everything the system generated:
run names, node ids, payload data, durations, counts. Never mix the two inside one phrase, a
sentence stays entirely in Space Grotesk even when it names an identifier, quote the identifier in
its own mono span instead of switching the sentence's face.

Weights: Space Grotesk 400, 500, 600, 700. DM Mono 400, 500.

## Tokens

The only place literal colors may appear is `src/app/globals.css`, tier 1 of the token system.
Every component reads the semantic alias, never the hex. Values below are the light theme
(`:root`, the default, name `wire`). The two dark presets, `factory` and `graphite`, remap the same
aliases to their own palettes, so a component that only ever writes `bg-surface` or `text-warn-ink`
never branches on theme.

| Alias | Tailwind class | Role | Light value |
|---|---|---|---|
| `--ground` | `bg-ground` | page background | `#efede8` |
| `--surface` | `bg-surface` | card and panel background | `#ffffff` |
| `--well` | `bg-well` | recessed background, info boxes, selected rows | `#f6f4f0` |
| `--line` | `border-line` | default border | `#dbd6cd` |
| `--line-strong` | `border-line-strong` | emphasized border, dashed states | `#b9b2a6` |
| `--label-1` | `text-label-1` | primary text | `#17181a` |
| `--label-2` | `text-label-2` | secondary text | `#4a453d` |
| `--label-3` | `text-label-3` | tertiary text, captions | `#6e6357` |
| `--accent` | `border-accent`, `fill-accent` | in-flight border, mark, fill strength | `#2a4fe0` |
| `--accent-soft` | `bg-accent-soft` | in-flight wash | `#edf0fd` |
| `--accent-ink` | `text-accent-ink` | text on the accent wash | `#2440b5` |
| `--ok-soft` | `bg-ok-soft` | completed wash, neutral on purpose | `#f4f2ee` |
| `--ok-ink` | `text-ok-ink` | completed text and border | `#17181a` |
| `--warn` | `border-warn`, `fill-warn` | needs-a-person border and mark | `#db4a24` |
| `--warn-soft` | `bg-warn-soft` | needs-a-person wash | `#fdf0ec` |
| `--warn-ink` | `text-warn-ink` | text on the warn wash | `#b03a1b` |
| `--danger-soft` | `bg-danger-soft` | destructive wash | `#fdf0ec` |
| `--danger-ink` | `text-danger-ink` | destructive text | `#b03a1b` |
| `--muted-soft` | `bg-muted-soft` | disabled background | `#f6f4f0` |
| `--muted-ink` | `text-muted-ink` | disabled and queued text | `#6e6357` |

Warn and danger share the vermilion family on purpose, both mean a person should look here. Where
they need to read apart, the icon and the words do it, never the color.

## Type scale

Five named steps. Feature code writes `text-micro`, `text-caption`, `text-body`, `text-subtitle`,
`text-title`, never an arbitrary pixel size.

| Class | Size | Line height | Use |
|---|---|---|---|
| `text-micro` | 10px | 1.4 | dense metadata, column heads |
| `text-caption` | 12px | 1.45 | secondary labels, controls, pill words |
| `text-body` | 14.5px | 1.55 | reading text, messages |
| `text-subtitle` | 17px | 1.45 | object names, card titles |
| `text-title` | 22px | 1.3, `tracking: -0.02em` | the station h1 |

Column heads and section labels additionally take `tracking-caps` (`0.16em`) set uppercase, small
and wide, in `text-micro`.

## Radius and elevation

Radius scale: `rounded-md` (10px) for controls, `rounded-lg` (13px), `rounded-xl` (16px), and
`rounded-2xl` (18px) for cards and panels of increasing size. Pills, buttons, and switches take
`rounded-full`.

Elevation is a hard offset shadow, never a soft blur, with one exception: things that genuinely
float above the page (menus, dialogs) take the soft one.

| Class | Light value | Use |
|---|---|---|
| `shadow-sm` | `0 3px 0 rgb(23 24 26 / 0.05)` | subtle lift |
| `shadow-md` | `0 4px 0 rgb(23 24 26 / 0.08)` | cards, canvas nodes |
| `shadow-lg` | `0 12px 28px rgb(23 24 26 / 0.14)` | floating elements only |

Each dark theme retunes these three to a darker, more opaque shadow so elevation still reads on a
dark surface. Components never set their own shadow values.

## Shape language

Four node glyphs, built by `node-glyph.tsx`. Shape says what kind of thing a node is, fill says
whether it ran.

- **Step**, work a machine does: a filled circle, ink fill, no border.
- **Person gate**, only a person can clear it: a rotated square (a diamond on screen), outline by
  default.
- **Agent gate**, the orchestrator judges it: a filled hexagon, always solid ink fill regardless of
  state, holding a grid icon with four corner tabs.
- **Auto gate**, passes with no judgement: the padlock-over-box icon, placed inside the person-gate
  diamond frame with a solid ink border so it is never confused with a step or an agent gate.

Fills: `ran` is solid ink. `not-run` is a dashed `line-strong` outline with no fill. `running` is
white with a cobalt (`accent`) border and a small solid cobalt play triangle, under a slow breathing
opacity pulse. `waiting` is a solid `warn` fill with a `warn-soft` halo ring, under the same pulse.
`failed` is a solid `warn` fill with a `warn-soft` halo ring and a white X.

The drawing should still make sense printed in grey: never rely on the fill's hue alone to tell
`running` from `waiting` from `failed`, the icon inside always disambiguates.

## State pills and provenance chips

**State pills** (`state-pill.tsx`) are a shape plus a word, `rounded-full`, icon plus text, weight
600, never a color alone. Tone picks the wash, border, ink, and icon together so a caller cannot
combine them into a state the language does not have.

| Tone | Background | Border | Text | Icon |
|---|---|---|---|---|
| `waiting` | `bg-warn-soft` | `border-warn` | `text-warn-ink` | filled diamond |
| `running` | `bg-accent-soft` | `border-accent` | `text-accent-ink` | filled play triangle |
| `completed` | `bg-ok-soft` | `border-ok-ink` | `text-ok-ink` | check |
| `failed` | diagonal stripe of `warn-soft` and `surface` | `border-warn` | `text-warn-ink` | X |
| `queued` | `bg-well` | dashed `border-line-strong` | `text-label-3` | none |
| `warning` | `bg-warn-soft` | `border-warn` | `text-warn-ink` | warning triangle |

Size `md` (fleet rows and headers) and `sm` (badge scale) only, the caller always supplies the
word: waiting states say "Waiting on you", running says "Running", completed says "Completed",
failed says "Failed", queued states (paused, stopped, held, queued) say their own word in the
`queued` tone. Schedule words at size `sm`: "Caught up", "Paused", "Last run failed", "Running now".

**Provenance chips** (`provenance-chip.tsx`) are not pills, plain rows with an icon plus a
sentence, no background of their own beyond the surrounding card. Variant `inline` sits in a
footer. Variant `box` is a standalone block on `bg-well` with a `border-line`, `rounded-lg`, an
icon, a heading, and body text.

Five actors, each with a fixed sentence template so every screen says the same thing:

- `person-approved`: "Approved by a person, {name}." The only phrasing allowed to say approved
  without a qualifier.
- `agent-decided`: "{gates} passed. The orchestrator agent decided this, no person approved it."
  (plural: "these... them.")
- `automatic`: "{gates} was configured to pass without review, no person approved it." (plural:
  "were... them.")
- `schedule-started`: "Started by a schedule, {name}."
- `person-started`: "Started by a person, {name}."

Every sentence about an agent or automatic action explicitly denies person approval, it never just
omits it.

## Buttons and fields

**Buttons** are ink pills, three variants plus a disabled state, `rounded-full`, weight 600,
`text-caption`, height 38px at default scale (30px at the small scale used in dense contexts).

| Variant | Background | Border | Text |
|---|---|---|---|
| `primary` | `bg-label-1` | none | `text-surface` |
| `secondary` | `bg-surface` | `border-line-strong` | `text-label-2` |
| `danger` | `bg-surface` | `border-danger-ink/35` | `text-danger-ink` |

Disabled state on any variant: `bg-muted-soft`, `text-label-3`, `cursor-not-allowed`. Only
`primary` carries its own shadow, independent of the elevation scale.

**Fields**:

- **Input**: `border-line` (a lighter step, `#e5e1d9`, for input borders specifically),
  `rounded-md`, label in `text-micro` with `tracking-caps` above the value, never beside it or
  inside a colon-suffixed label.
- **Select**: same border and radius as input, value set in `font-mono` `text-caption`, a chevron
  in `text-label-3`.
- **Radio card** (the "Gate mode" pattern): a label wraps a circular radio plus text. Selected:
  `border-label-1`, `bg-well`, filled dot. Unselected: `border-line`, no background, empty ring.
  An optional second line of description sits below the label in `text-caption text-label-3`.
- **Checkbox**: a square, `rounded-md` at a smaller radius, `border-line-strong`, unchecked by
  default.
- **Switch** (`role="switch"`): a `rounded-full` track. On: `bg-label-1`, white thumb at the far
  end. Off: `bg-surface`, `border-line-strong`, thumb in `line-strong` at the near end.

## Copy rules

- **Punctuation.** Only `,` `.` `?` `(` `)`. No colons, dashes, semicolons, or slashes anywhere in
  the interface.
- **Labels above values**, never a label followed by a colon and a value on the same line.
- **Machine actions name the actor every time.** "Decided by the agent." "Passed automatically."
  "Started by a schedule." A machine action never reads as a person's approval, the sentence
  explicitly denies it.
- **Sentences, not fragments.** A row says what happened in a sentence a person can read aloud.
  Counts and durations stay in mono, out of the sentence.
- **Data is quoted, not styled.** Payloads keep their own punctuation, including colons. They live
  in a mono block (`font-mono`), never reflowed into prose.

## Accessibility floor

- Text contrast at least 4.5 to 1 on its background, in every theme.
- Meaning is never carried by color alone. A state pill's icon and word, and a node glyph's shape,
  carry it too.
- Every icon-only control has an `aria-label`. Decorative icons (inside a pill or chip that already
  carries the word) are `aria-hidden`.
- `prefers-reduced-motion: reduce` is honored everywhere, transitions and animations collapse to
  effectively instant, including the pulse on running and waiting nodes.

## What not to do

- No raw hex colors or literal palette names in components, only the semantic aliases above.
- No arbitrary sizes (`text-[13.5px]`, `p-[11px]`), only the scale's named classes.
- No glassmorphism, no backdrop blur, no translucent frosted panels.
- No soft blur shadows except the one floating step reserved for genuinely floating elements.
- No decorative emoji as icons or state markers, use the inline SVG marks.
- No state communicated by color alone, a shape and a word always carry it too.
