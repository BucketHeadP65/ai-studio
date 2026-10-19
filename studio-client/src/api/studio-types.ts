import { z } from "zod";

// The Workflow studio's response boundary, mirroring the `studio.agent`
// FastAPI routes (studio/agent/features/{authoring,browsing,running}/routes.py).
// Everything the studio renders passes through
// these schemas first, so a backend contract drift surfaces as a named ZodError
// on the failing endpoint instead of as an undefined three components deep.
//
// Nullability follows the backend's own tri-states, which are load-bearing:
// `staleness: []` is CHECKED AND FRESH while `null` is "never stamped, unknown",
// and `birth`/`carried` distinguish "nothing ever ran it" from "checked, nothing
// moved". Collapsing either null into an empty list would render a weaker claim
// as a stronger one. A graded claim's `holds` is the same kind of tri-state, and
// so is a `hit_rate` of `null` against one of `0`.

// -- json ----------------------------------------------------------------------

// Any value JSON can hold. What a workflow's arguments are made of: the backend admits each one
// against its parameter's own JSON Schema without coercing it, so an array stays an array, a
// boolean stays a boolean, and nothing here is ever the printed form of a value.
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export const JsonValueSchema: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.null(),
    z.boolean(),
    z.number(),
    z.string(),
    z.array(JsonValueSchema),
    z.record(z.string(), JsonValueSchema),
  ]),
);

// -- graph ---------------------------------------------------------------------

// The three-way answer to "who decides a gate this run reaches": waiting for a person, handed to
// this run's own orchestrator instead of waiting, or passed with nobody asked. Shared by a launch
// request, a schedule's own row, and the live run/state readout, so the three wire boundaries
// agree on one closed vocabulary rather than three that could drift.
export const GateModeSchema = z.enum(["wait", "orchestrate", "auto"]);
export type GateMode = z.infer<typeof GateModeSchema>;

// One node's JSON Schema, exactly as the backend hands it back -- a pydantic step's own
// `model_json_schema()` output, or a schema built by hand for a param. Loose on purpose: read
// only through the `workflow-order` helpers (`resolveRef`, `typeLabel`) and `SchemaTable`, which
// narrow with type guards, never destructured directly by a component.
export const JsonSchemaSchema = z.record(z.string(), z.unknown());
export type JsonSchema = z.infer<typeof JsonSchemaSchema>;

// One turn of a map node's own per-item work -- the base work every map runs, on its own for a
// single-turn map, plus one more entry for every leaf a multi-turn map chains after it in order.
// `output_schema` is this turn's own single-item shape, distinct from the map node's own
// `output_schema`, which is already the list-wrapped shape of the LAST turn's output.
export const MapTurnSchema = z.object({
  work: z.string(),
  // The event stream's own id for this turn's run -- the map's own id for its first turn (and
  // for the only turn of a single-turn map), `"{id}.{work}"` for every turn after it. What
  // `buildLanes` folds a later turn's own call into the map node's own lane by, rather than
  // drawing it a stray lane of its own.
  node_id: z.string(),
  briefing: z.string().default(""),
  tools: z.array(z.string()).default([]),
  output_schema: JsonSchemaSchema.nullable(),
  source: z.string().default(""),
});
export type MapTurn = z.infer<typeof MapTurnSchema>;

export const GraphNodeSchema = z.object({
  key: z.string(),
  id: z.string(),
  kind: z.enum(["param", "run", "map", "gate", "judge", "orchestrator-gate"]),
  signature: z.string(),
  detail: z.string().default(""),
  tools: z.array(z.string()).default([]),
  is_output: z.boolean().default(false),
  unknown: z.boolean().default(false),
  brought: z.boolean().default(false),
  briefing: z.string().default(""),
  // The step name this node runs, distinct from `id` when a spec runs the same step under two
  // node ids -- absent (or empty) for a param or a person gate, neither of which names one. The
  // archive's own conversations dict is keyed by this, never by `id`.
  work: z.string().optional(),
  // Each argument's JSON Schema, keyed by name -- the leaf's own inputs for a `run` or `map`
  // node (the map's per item argument included), `{}` for a `param` or gate node and for an
  // unknown step.
  input_schemas: z.record(z.string(), JsonSchemaSchema),
  // What this node hands on: a `run` step's own output schema, a `map`'s wrapped as a JSON
  // Schema array, a `param`'s own schema, or the schema of the value a gate reviews. `null` when
  // it cannot be resolved, or for an unknown step.
  output_schema: JsonSchemaSchema.nullable(),
  // The program text of a carried program step, `""` otherwise.
  source: z.string(),
  // Whether a launch must supply this parameter. Every `param` node carries it; no other kind is
  // ever asked for, so they default rather than each stating a fact that is not theirs.
  required: z.boolean().default(true),
  // Every turn a map node runs, base work first -- one entry for a single-turn map, several for
  // a map that chains further work after it. Absent for every other kind.
  turns: z.array(MapTurnSchema).nullable().default(null).optional(),
});
export type GraphNode = z.infer<typeof GraphNodeSchema>;

export const GraphEdgeSchema = z.object({
  from: z.string(),
  to: z.string(),
  kind: z.enum(["wire", "map", "rejected"]),
  label: z.string().default(""),
});
export type GraphEdge = z.infer<typeof GraphEdgeSchema>;

export const WorkflowGraphSchema = z.object({
  name: z.string(),
  version: z.number(),
  nodes: z.array(GraphNodeSchema),
  edges: z.array(GraphEdgeSchema),
});
export type WorkflowGraph = z.infer<typeof WorkflowGraphSchema>;

// -- library -----------------------------------------------------------------

export const TrustSchema = z.object({
  level: z.string(),
  runs_with_evidence: z.number(),
  failed_runs: z.number(),
  // Who approved this exact version, in the order they did. Empty until an approval lands.
  approved_by: z.array(z.string()),
  // Reliable and right are different claims: `runs_with_evidence` says the artifact ran;
  // these say whether anything measured what it produced.
  structural_grades: z.number().int().nonnegative(),
  structural_mean: z.number().nullable(),
  structural_last: z.number().nullable(),
});

export type Trust = z.infer<typeof TrustSchema>;

export const LibraryEntrySchema = z.object({
  id: z.string(),
  title: z.string(),
  summary: z.string().default(""),
  provisional: z.boolean().default(true),
  score: z.number().nullish(),
  trust: TrustSchema,
  needs: z.array(z.string()).default([]),
  // `null` when the spec could not be read at all — a card that must say
  // "unreadable", not pretend the artifact is runnable.
  missing: z.array(z.string()).nullable(),
  brings: z.array(z.string()).default([]),
  version: z.number().nullable(),
  // When this artifact was archived, or `null` while it is live.
  archived_at: z.string().datetime({ offset: true }).nullable(),
  // Node counts off the same graph the workflow's own detail page builds, all zeros for a
  // workflow whose graph cannot be built.
  shape: z.object({ steps: z.number(), person_gates: z.number(), agent_gates: z.number() }),
  // This artifact's newest ended run off the run archive, or `null` when it has never run.
  runs: z.object({
    last: z
      .object({ outcome: z.string(), started_at: z.string(), ended_at: z.string() })
      .nullable(),
  }),
  // What every priced run of this workflow has cost so far, and how many of its runs were
  // priced at all. Absent or `null` before any run of it has been priced.
  spend: z
    .object({ cost_usd: z.string(), unpriced: z.number().default(0), runs: z.number() })
    .nullish(),
});
export type LibraryEntry = z.infer<typeof LibraryEntrySchema>;

// The approve route's answer: the trust record the approved version now carries, refreshed.
export const ApprovedSchema = z.object({
  ok: z.literal(true),
  trust: TrustSchema,
});
export type Approved = z.infer<typeof ApprovedSchema>;

// -- one saved workflow ----------------------------------------------------------

export const LineageSchema = z.object({
  parent_name: z.string(),
  parent_version: z.number(),
  touched: z.array(z.string()).default([]),
  ops: z.array(z.unknown()).default([]),
  // Why this version exists, in the words of whoever adapted it. Prose, and
  // read rather than scored: it is the reason the version exists, never an
  // input to any verdict about it. Empty is a real state.
  evidence: z.string().default(""),
  // The library's own id for the parent when it still holds that version — a link
  // the reader can open — or null. Never assembled client-side: `wf.x.v1` and
  // `x@1` are the same artifact in two library adapters.
  parent_id: z.string().nullable(),
});
export type Lineage = z.infer<typeof LineageSchema>;

// One claim an adaptation made about its child, answered off the run ledger.
// `holds` is a TRI-STATE and stays one: `true` held, `false` missed, `null`
// UNMEASURABLE — no run has been able to answer it. Absent evidence is unknown,
// never false, so `null` collapsed into a boolean would report a lie about the
// adapter. `reads` is the sentence the backend composed for this verdict; the
// fields beside it are what lets a screen draw the three states apart.
export const PredictionOutcomeSchema = z.object({
  claim: z.enum(["completes", "produces", "accepts", "approves"]),
  subject: z.string().default(""),
  shape: z.string().default(""),
  holds: z.boolean().nullable(),
  observed: z.string().default(""),
  reads: z.string(),
});
export type PredictionOutcome = z.infer<typeof PredictionOutcomeSchema>;

// What a body of graded claims adds up to. `unmeasurable` is counted apart from
// `misses` because an untested claim is not a wrong one, and `hit_rate` is
// `null` — never `0` — until something has actually been measured: a rate of
// zero reads as an adapter that is always wrong, which is a different statement.
export const PredictionRecordSchema = z.object({
  hits: z.number(),
  misses: z.number(),
  unmeasurable: z.number(),
  hit_rate: z.number().nullable(),
});
export type PredictionRecord = z.infer<typeof PredictionRecordSchema>;

// -- the author's own words, and whether a workflow is described -------------------

// One artifact action a result offers, drawn from a closed first-party set. Read as data: the
// host implements each `kind`, and there is nowhere here to put a URL, a method, or a body.
// Strict on purpose: an extra key is refused rather than stripped, so a presentation carrying an
// endpoint, a method, a URL or a body fails the parse instead of arriving as an action that quietly
// lost half of what its author meant by it.
export const ArtifactActionSchema = z.discriminatedUnion("kind", [
  z.strictObject({
    kind: z.literal("save_note"),
    id: z.string(),
    label: z.string(),
    artifact_id: z.string(),
    destination: z.literal("user_selects"),
  }),
  z.strictObject({
    kind: z.literal("record_owner_review"),
    id: z.string(),
    label: z.string(),
    artifact_ids: z.array(z.string()),
  }),
]);
export type ArtifactAction = z.infer<typeof ArtifactActionSchema>;

// Which control one declared parameter is answered through, when the author named one. A closed
// first-party set: `knowledge-base` is the only selector, and it offers the names this deployment
// has registered. Nothing in a parameter's own JSON Schema carries a hint of this kind.
export const InputControlSchema = z.enum(["text", "textarea", "knowledge-base", "json"]);
export type InputControl = z.infer<typeof InputControlSchema>;

// Every word here is the author's. An empty field is a field nobody wrote, and the screens show
// nothing for it rather than a sentence this app composed.
export const WorkflowPresentationSchema = z.object({
  purpose: z.string().default(""),
  produces: z.string().default(""),
  owner_participation: z.string().default(""),
  not_for: z.string().default(""),
  inputs: z
    .array(
      z.object({
        key: z.string(),
        label: z.string(),
        // `null` leaves the choice to whatever reads the parameter's schema.
        control: InputControlSchema.nullish().default(null),
      }),
    )
    .default([]),
  result_fields: z.array(z.object({ label: z.string(), path: z.string() })).default([]),
  actions: z.array(ArtifactActionSchema).default([]),
});
export type WorkflowPresentation = z.infer<typeof WorkflowPresentationSchema>;

// The four sentences as a card patch carries them. Absent means unchanged, so a form sends only
// the boxes it owns and the rest of the author's words stay as the library holds them.
export interface WorkflowPresentationPatch {
  purpose?: string;
  produces?: string;
  owner_participation?: string;
  not_for?: string;
}

// The field a finding belongs beside. A closed set, so a form can place every one of them.
export const ReadinessFieldSchema = z.enum([
  "title",
  "summary",
  "purpose",
  "produces",
  "owner_participation",
  "not_for",
  "work",
]);
export type ReadinessField = z.infer<typeof ReadinessFieldSchema>;

export const ReadinessFindingSchema = z.object({
  field: ReadinessFieldSchema,
  severity: z.enum(["error", "warning"]),
  message: z.string(),
});
export type ReadinessFinding = z.infer<typeof ReadinessFindingSchema>;

// The backend's own reading of one workflow: whether this run could execute it, whether anybody
// has described it, and everything either question found. The only thing a Save button is
// disabled from -- the browser never decides this for itself.
export const ReadinessSchema = z.object({
  executable: z.boolean(),
  described: z.boolean(),
  findings: z.array(ReadinessFindingSchema).default([]),
});
export type Readiness = z.infer<typeof ReadinessSchema>;

export const WorkflowDetailSchema = z.object({
  ok: z.literal(true),
  id: z.string(),
  name: z.string(),
  version: z.number(),
  description: z.string().default(""),
  // The card a chooser reads -- distinct from the spec's own `description` above, and the one
  // `PATCH .../card` rewrites. A blank `title` is the signal the header falls back to the id for.
  title: z.string(),
  summary: z.string(),
  applicability: z.string(),
  provisional: z.boolean().default(true),
  graph: WorkflowGraphSchema,
  issues: z.array(z.string()).default([]),
  fits: z.boolean(),
  missing: z.array(z.string()).default([]),
  brings: z.array(z.string()).default([]),
  spec_yaml: z.string(),
  staleness: z.array(z.string()).nullable(),
  lineage: LineageSchema.nullable(),
  birth: z.array(z.string()).nullable(),
  carried: z.array(z.string()).nullable(),
  // `null` when the adaptation claimed nothing — which is a different statement
  // from claims that graded as nothing, and must not render as an empty result.
  predictions: z.array(PredictionOutcomeSchema).nullable(),
  prediction_record: PredictionRecordSchema.nullable(),
  // What this exact artifact has earned — the same level the library card shows, now
  // reaching the person who actually opened it, not only the agent fetching the
  // spec over the contract port.
  trust: TrustSchema,
  // When this artifact was archived, or `null` while it is live.
  archived_at: z.string().datetime({ offset: true }).nullable(),
  // The same running total the library card carries -- what every priced run of this workflow has
  // cost so far, and how many of its runs were priced at all. Absent or `null` before any run
  // of it has been priced.
  spend: z
    .object({ cost_usd: z.string(), unpriced: z.number().default(0), runs: z.number() })
    .nullish(),
  // The author's own words, or `null` when the library kept none. Absent stays absent.
  presentation: WorkflowPresentationSchema.nullish(),
  // The same reading the card route refuses on. A card answer merged into a cached detail may
  // carry it as `null`, and a screen with no reading shows no findings.
  readiness: ReadinessSchema.nullish(),
});
export type WorkflowDetail = z.infer<typeof WorkflowDetailSchema>;

// The `PATCH .../card` route's answer: the refreshed card, re-read off the library rather than
// echoed back -- the same fields `WorkflowDetail` carries, id included so a caller with several
// open never has to guess which one just saved. The archive and restore routes answer with this
// same shape.
export const WorkflowCardSchema = z.object({
  ok: z.literal(true),
  id: z.string(),
  title: z.string(),
  summary: z.string(),
  applicability: z.string(),
  provisional: z.boolean(),
  archived_at: z.string().datetime({ offset: true }).nullable(),
  presentation: WorkflowPresentationSchema.nullish(),
  // `null` on the routes that changed no prose (archive, restore): they answer about a card they
  // did not describe, and a stale reading on the screen is worse than none.
  readiness: ReadinessSchema.nullish(),
});
export type WorkflowCard = z.infer<typeof WorkflowCardSchema>;

// -- authoring --------------------------------------------------------------------

// What one person asked for, in their own words, and what they expect a run to do. A record and
// never a permission: two drafts with the same graph run the same way whether or not one carries
// this. Only `request` is required, because an incomplete statement is the normal state of one
// still being written, and nothing here fills a blank field in on the person's behalf.
export const WorkflowIntentSchema = z.object({
  request: z.string(),
  expected_result: z.string().default(""),
  success_example: z.string().default(""),
  empty_input_behavior: z.string().default(""),
  failure_behavior: z.string().default(""),
  trigger: z.enum(["manual", "scheduled"]).default("manual"),
  // What was filled in without being asked, one entry each, said out loud rather than taken quietly.
  assumptions: z.array(z.string()).default([]),
});
export type WorkflowIntent = z.infer<typeof WorkflowIntentSchema>;

// Which of the four expected answers the backend counted as still blank. A closed set, so a panel
// can name every one of them, and a count rather than a verdict: a field carrying text is not
// thereby a field carrying a good answer.
export const IntentIssueSchema = z.enum([
  "expected_result",
  "success_example",
  "empty_input_behavior",
  "failure_behavior",
]);
export type IntentIssue = z.infer<typeof IntentIssueSchema>;

export const DraftSchema = z.object({
  spec_yaml: z.string().default(""),
  title: z.string().default(""),
  summary: z.string().default(""),
  applicability: z.string().default(""),
  lineage: LineageSchema.omit({ parent_id: true }).nullish(),
  // The author's words on the proposal itself, `null` until anything has written any.
  presentation: WorkflowPresentationSchema.nullish(),
  // What the person asked for, `null` until anything has recorded it. Absent is not incomplete.
  intent: WorkflowIntentSchema.nullish(),
});
export type Draft = z.infer<typeof DraftSchema>;

export const ChatReplySchema = z.object({
  message: z.string(),
  draft: DraftSchema,
  issues: z.array(z.string()).default([]),
  valid: z.boolean(),
  graph: WorkflowGraphSchema.nullable(),
  // `null` while nothing on screen parses as a spec: there is no workflow to read a card against
  // yet, which is a weaker statement than one that reads as not described.
  readiness: ReadinessSchema.nullish(),
  // Which of the intent's expected answers are still blank. Empty when the draft records no
  // intent at all, since nothing is owed until somebody starts stating one.
  intent_issues: z.array(IntentIssueSchema).default([]),
});
export type ChatReply = z.infer<typeof ChatReplySchema>;

export const TurnSchema = z.object({
  who: z.enum(["you", "agent"]),
  text: z.string(),
});
export type Turn = z.infer<typeof TurnSchema>;

export const ConversationSchema = z.object({
  turns: z.array(TurnSchema),
  session_id: z.string().nullable(),
});
export type Conversation = z.infer<typeof ConversationSchema>;

export const ConversationStartedSchema = z.object({ session_id: z.string() });
export type ConversationStarted = z.infer<typeof ConversationStartedSchema>;

export const SavedSchema = z.object({ ok: z.literal(true), id: z.string() });
export type Saved = z.infer<typeof SavedSchema>;

// What a refused save answers with: the sentence a person reads, and the field-by-field reading
// the form puts beside its own boxes.
export const SaveRefusedSchema = z.object({
  ok: z.literal(false),
  detail: z.string().default(""),
  readiness: ReadinessSchema.nullish(),
});
export type SaveRefused = z.infer<typeof SaveRefusedSchema>;

// -- steps (what this app can do) --------------------------------------------------

export const StepSchema = z.object({
  name: z.string(),
  description: z.string().default(""),
  signature: z.string(),
  output: z.string(),
  tools: z.array(z.string()).default([]),
});
export type Step = z.infer<typeof StepSchema>;

// -- running ------------------------------------------------------------------------

// One launch, settled before anything is created: which workflow, what this run is called, the
// typed value for every parameter it was given, and who decides the gates it reaches. Resolved by
// the setup dialog, so a cancelled or incomplete setup leaves the runner untouched.
export interface RunIntent {
  workflowId: string;
  runTitle: string;
  arguments: Record<string, JsonValue>;
  gateMode: GateMode;
}

export const StepEventSchema = z.object({
  seq: z.number(),
  node: z.string(),
  at: z.number(),
  finished: z.boolean(),
  failed: z.boolean(),
});
export type StepEvent = z.infer<typeof StepEventSchema>;

export const RunStateSchema = z.object({
  running: z.boolean(),
  workflow: z.string().nullable(),
  result: z.string().nullable(),
  active: z.array(z.string()).default([]),
  done: z.array(z.string()).default([]),
  failed: z.array(z.string()).default([]),
  waiting_at: z.string().nullable(),
  events: z.array(StepEventSchema).default([]),
  seq: z.number(),
  // The live run's own automatic-approval flag, the same one `/api/run/automode` flips.
  auto_approve: z.boolean(),
  // Who decides a gate this run reaches, read live off the session -- `auto_approve` above is
  // derived from this, not the other way round.
  gate_mode: GateModeSchema,
});
export type RunState = z.infer<typeof RunStateSchema>;

export const RunStartedSchema = z.object({ ok: z.literal(true), running: z.string() });
export type RunStarted = z.infer<typeof RunStartedSchema>;

// One money ceiling this deployment holds a run to, counted over its own window. `usd` crosses as
// a decimal string, the way every other money figure here does, so no figure is rounded on the way.
export const MoneyBrakeSchema = z.object({
  window: z.enum(["run", "conversation", "day", "month", "total"]),
  usd: z.string(),
  warn_at: z.number().nullish(),
});
export type MoneyBrake = z.infer<typeof MoneyBrakeSchema>;

// What a run started here is held to, read before anybody starts one. An empty `brakes` is a
// deployment that configured no ceiling at all, which a reader states as none configured rather
// than as a ceiling of zero. `gate_modes` is what this deployment can actually be asked for.
export const RunLimitsSchema = z.object({
  brakes: z.array(MoneyBrakeSchema),
  gate_modes: z.array(GateModeSchema),
  default_gate_mode: GateModeSchema,
});
export type RunLimits = z.infer<typeof RunLimitsSchema>;

// What each node of a session's run produced, rendered server-side for
// reading. Live while the run goes, still answering after it ends.
export const RunValuesSchema = z.object({
  workflow: z.string().nullable(),
  values: z.record(z.string(), z.string()),
});
export type RunValues = z.infer<typeof RunValuesSchema>;

export const PendingGateSchema = z.object({
  gate_id: z.string(),
  workflow: z.string(),
  // The rendered value under review — the actual object, stringified by the
  // backend's own renderer, never a summary somebody wrote about it.
  reviewing: z.string(),
  // The node id a rejection reruns from, so the choice is informed.
  rejection_reruns: z.string(),
});
export type PendingGate = z.infer<typeof PendingGateSchema>;

export const PendingSchema = z.object({ pending: PendingGateSchema.nullable() });
export type Pending = z.infer<typeof PendingSchema>;

export const DecidedSchema = z.object({ ok: z.literal(true), approved: z.boolean() });
export type Decided = z.infer<typeof DecidedSchema>;

export const OkSchema = z.object({ ok: z.boolean() });

// The live run's automatic-approval flag, flipped from the control strip while a run is
// in flight -- gates it reaches from here on pass themselves, never counted as a human
// approval.
export const AutomodeSchema = z.object({ ok: z.boolean(), auto_approve: z.boolean() });
export type Automode = z.infer<typeof AutomodeSchema>;

// -- run history --------------------------------------------------------------------

// One response's own priced usage -- input, output, and cache tokens, read off the provider's
// own accounting for that call.
const SpendUsageSchema = z.object({
  input_tokens: z.number(),
  output_tokens: z.number(),
  cache_read_tokens: z.number(),
  cache_write_tokens: z.number(),
});

// A run's own priced cost: the total, and the same reading folded per step, keyed by step name.
// A step the run never priced (nothing ran it) carries no key here at all.
export const SpendViewSchema = z.object({
  cost_usd: z.string(),
  // Responses no source could name a figure for. Above zero, `cost_usd` is a floor rather than the
  // cost, and a screen says so rather than showing a number that reads as the whole of it.
  unpriced: z.number().default(0),
  usage: SpendUsageSchema,
  steps: z.record(
    z.string(),
    z.object({ cost_usd: z.string(), unpriced: z.number().default(0), usage: SpendUsageSchema }),
  ),
});
export type SpendView = z.infer<typeof SpendViewSchema>;

// The address of one immutable value a run fixed, and the digest that says which value it is.
// There is no URL here and never will be: a holder asks the run's own resolve route with this
// reference and gets exactly those bytes back, or a refusal.
export const ArtifactRefSchema = z.object({
  id: z.string(),
  label: z.string(),
  run_id: z.string(),
  node_id: z.string(),
  turn: z.string().nullable().default(null),
  value_digest: z.string(),
  media_type: z.enum(["text/markdown", "application/json", "text/plain"]),
});
export type ArtifactRef = z.infer<typeof ArtifactRefSchema>;

// One resolved reference: the address that was asked for, and the value it addresses. The host has
// already checked the value against its own reference, so a document that arrives is one whose
// digest, media type and size are agreed.
export const ArtifactDocumentSchema = z.object({
  ref: ArtifactRefSchema,
  value: z.unknown(),
});
export type ArtifactDocument = z.infer<typeof ArtifactDocumentSchema>;

// One decision a person recorded about what a run produced, over the exact references they read.
// It is an event, not a status: the host appends it and never rewrites one, so a run carries
// every review it was ever given, in the order they were given. `actor` is the name whoever
// decided typed, under the local-owner model this deployment runs on.
export const OwnerReviewSchema = z.object({
  review_id: z.string(),
  actor: z.string(),
  outcome: z.enum(["accepted", "rejected"]),
  reason: z.string(),
  artifact_refs: z.array(ArtifactRefSchema),
  // Which route recorded it, assigned by the host and never by a caller.
  source: z.string(),
  at: z.string().datetime({ offset: true }),
});
export type OwnerReview = z.infer<typeof OwnerReviewSchema>;

// The answer to one recorded review: the event the request now names, and whether this call was
// the one that minted it. `created: false` is an identical resubmission meeting the event it
// already wrote, which is what makes a retry after a lost answer safe.
export const OwnerReviewResultSchema = z.object({
  review: OwnerReviewSchema,
  created: z.boolean(),
});
export type OwnerReviewResult = z.infer<typeof OwnerReviewResultSchema>;

// What a person's decision carries to the host: the references exactly as the run minted them,
// the name of whoever decided, their verdict and their reason. `review_id` is the caller's own
// idempotency key: the same id with the same content is the same event, and the same id over
// different content is refused.
export interface OwnerReviewSubmission {
  review_id: string;
  actor: string;
  outcome: OwnerReview["outcome"];
  reason: string;
  artifact_refs: ArtifactRef[];
}

// One required acceptance, as the host checked it. Carried whole so a screen that needs to say
// which gate was never answered has it, without re-deriving anything from decisions.
export const OutcomeCheckSchema = z.object({
  id: z.string(),
  passed: z.boolean().nullable(),
  observed_source: z.enum(["judge", "person", "orchestrator", "auto", "timeout", "unknown"]),
  detail: z.string().default(""),
});
export type OutcomeCheck = z.infer<typeof OutcomeCheckSchema>;

// What a run's own facts say it did, checked by the host and never inferred from a produced value.
// `succeeded` means every piece of the primary work succeeded, which is a statement about
// producers and about nothing else. `unknown` is what nothing established, and is never a guess.
export const OutcomeSchema = z.object({
  status: z.enum(["succeeded", "partial", "noop", "failed", "unknown"]),
  summary: z.string().default(""),
  attempted: z.number().default(0),
  succeeded: z.number().default(0),
  skipped: z.number().default(0),
  failed: z.number().default(0),
  retry_attempts: z.number().default(0),
  primary_input: z.number().nullable().default(null),
  generation: z.number().nullable().default(null),
  checks: z.array(OutcomeCheckSchema).default([]),
  artifacts: z.array(ArtifactRefSchema).default([]),
});
export type Outcome = z.infer<typeof OutcomeSchema>;

export const RunEnvelopeSchema = z.object({
  run_id: z.string(),
  workflow_id: z.string(),
  // What this run itself is called, as whoever started it named it -- never the name of the session
  // that happens to host it. Empty for a run nobody named, which is what lets a reader fall back to
  // the workflow's own card.
  run_title: z.string(),
  outcome: z.enum(["completed", "failed", "stopped", "live"]),
  failure: z.string().default(""),
  started_at: z.string(),
  ended_at: z.string().default(""),
  runner: z.string(),
  session_id: z.string(),
  // Each declared parameter as the JSON value the run was actually given: an array stays an array
  // and a number a number, which is what lets a rerun reopen the same values in the same controls.
  params: z.record(z.string(), JsonValueSchema).default({}),
  gates_approved: z.array(z.string()).default([]),
  gates_rejected: z.record(z.string(), z.number()).default({}),
  gates_auto: z.array(z.string()).default([]),
  // Gates this run's own orchestrator decided -- an authored orchestrator gate, or a person gate
  // converted under gate_mode orchestrate. Counted apart from `gates_approved` (a person's own
  // yes) and `gates_auto` (nobody asked), the same honesty those two already keep.
  gates_orchestrated: z.array(z.string()),
  pinned: z.boolean().default(false),
  // This run's own priced cost, `null` when no ledger exists for it, as for a run still too fresh
  // to have priced a single response.
  spend: SpendViewSchema.nullable(),
  // What the run actually did, checked against its own facts -- a different question from
  // `outcome`, which says only how the execution ended.
  result: OutcomeSchema,
});
export type RunEnvelope = z.infer<typeof RunEnvelopeSchema>;

export const RunHistoryRecordSchema = RunEnvelopeSchema.extend({
  spec: z.unknown(),
  values: z.record(z.string(), z.string()).default({}),
  // Each worker step's own messages at the moment the run ended, keyed by step name --
  // pydantic-ai's `ModelMessage` grammar, dumped to JSON. Loose on purpose: rendering reads
  // only `kind` and each part's `part_kind`/`content`, tolerating whatever else a message
  // carries.
  conversations: z.record(z.string(), z.array(z.unknown())),
  events: z.array(StepEventSchema).default([]),
  graph: WorkflowGraphSchema.nullable(),
  // Every review a person recorded against this run, oldest first. Empty is a run nobody has
  // reviewed: owner acceptance unknown, never accepted and never rejected.
  owner_reviews: z.array(OwnerReviewSchema),
});
export type RunHistoryRecord = z.infer<typeof RunHistoryRecordSchema>;
