import { z } from "zod";

// The self-authoring harness's response boundary, mirroring the agent backend's
// /api/harness routes (studio/agent/features/selfauthoring/routes.py) field-for-field.
// Everything the agent authors lands pending in one of three kinds; the dashboard is
// the owner's review-and-activate surface over them, same acts as `studio harness`.

export const HarnessKindSchema = z.enum(["capability", "step", "agent", "profile"]);
export type HarnessKind = z.infer<typeof HarnessKindSchema>;

// One custom artifact's listing row. `promoted` is meaningful only for
// kind=capability (null elsewhere); `next_step` names the one forward act the row is
// ready for — a broken artifact (last_error set) has none, it needs re-authoring.
export const HarnessArtifactSchema = z.object({
  name: z.string(),
  status: z.enum(["active", "disabled"]),
  descriptor: z.string(),
  last_error: z.string().nullable(),
  promoted: z.boolean().nullable(),
  next_step: z.enum(["activate", "promote"]).nullable(),
});
export type HarnessArtifact = z.infer<typeof HarnessArtifactSchema>;

export const HarnessListingSchema = z.object({
  kind: HarnessKindSchema,
  artifacts: z.array(HarnessArtifactSchema),
});
export type HarnessListing = z.infer<typeof HarnessListingSchema>;

export const HarnessActionSchema = z.object({
  kind: HarnessKindSchema,
  name: z.string(),
  status: z.enum(["active", "disabled"]),
});
export type HarnessAction = z.infer<typeof HarnessActionSchema>;

export const HarnessDeletionSchema = z.object({
  kind: HarnessKindSchema,
  name: z.string(),
  deleted: z.literal(true),
});
export type HarnessDeletion = z.infer<typeof HarnessDeletionSchema>;

// One built-in plugin: what it is, whether it is on, and whether it can move at all.
// Always-on plugins (fixed) are never off; the toggleable ones flip with next-turn effect.
export const PluginStateSchema = z.object({
  name: z.string(),
  description: z.string(),
  enabled: z.boolean(),
  fixed: z.boolean(),
});
export type PluginState = z.infer<typeof PluginStateSchema>;

export const PluginsListingSchema = z.object({
  plugins: z.array(PluginStateSchema),
});
export type PluginsListing = z.infer<typeof PluginsListingSchema>;

// The profile in effect, or null when the hand-set plugins govern. GET and PUT
// /api/harness/selection share this one shape; switching is refused (409)
// mid-conversation, with the fix named in `detail`.
export const SelectionSchema = z.object({
  name: z.string().nullable(),
});
export type Selection = z.infer<typeof SelectionSchema>;

export const PromotionActionSchema = z.object({
  name: z.string(),
  promoted: z.boolean(),
});
export type PromotionAction = z.infer<typeof PromotionActionSchema>;

// The three tiers the harness routes work onto, each an owner-set model plus a
// thinking level. Mirrors harness.adapters.model_tiers.Tier.
export const TierSchema = z.enum(["orchestrator", "worker", "helper"]);
export type Tier = z.infer<typeof TierSchema>;

export const ThinkingSchema = z.enum(["low", "medium", "high", "xhigh"]);
export type Thinking = z.infer<typeof ThinkingSchema>;

// One entry on the model menu: what it costs, how big its context window is, and
// whether it takes a reasoning effort level, a toggle, or neither. Mirrors
// harness.adapters.model_list.ModelEntry field-for-field. The price fields ride the
// wire as decimal strings (FastAPI serializes a pydantic `Decimal` that way), never
// floats, so a fraction of a cent is never lost to floating-point rounding.
export const ModelEntrySchema = z.object({
  id: z.string(),
  name: z.string(),
  route: z.enum(["openrouter", "gateway", "azure"]),
  context_length: z.number().nullable(),
  price_in_per_m: z.string().nullable(),
  price_out_per_m: z.string().nullable(),
  reasoning: z.enum(["effort", "toggle", "none"]),
  pinned: z.boolean(),
});
export type ModelEntry = z.infer<typeof ModelEntrySchema>;

// One tier as the owner sees it now: the job it does (`role`), the model behind it,
// its thinking level, and whether that model is still the deployment default or an
// owner override. Mirrors GET/PUT/DELETE /api/harness/tiers(/{tier}).
export const TierViewSchema = z.object({
  tier: TierSchema,
  role: z.string(),
  model: ModelEntrySchema,
  thinking: ThinkingSchema,
  default: z.boolean(),
});
export type TierView = z.infer<typeof TierViewSchema>;

export const TiersListingSchema = z.object({ tiers: z.array(TierViewSchema) });
export type TiersListing = z.infer<typeof TiersListingSchema>;

export const ModelsListingSchema = z.object({ models: z.array(ModelEntrySchema) });
export type ModelsListing = z.infer<typeof ModelsListingSchema>;

// The live setup the custom artifacts join once activated: the host agent's model and
// run engine, the steps this process can execute, the promoted set, and
// how many custom artifacts are currently active per kind.
export const HarnessCompositionSchema = z.object({
  model: z.string(),
  run_engine: z.enum(["in_process", "temporal"]),
  steps_registered: z.array(z.string()),
  promoted: z.array(z.string()),
  active: z.object({
    capability: z.number(),
    step: z.number(),
    agent: z.number(),
    profile: z.number(),
  }),
});
export type HarnessComposition = z.infer<typeof HarnessCompositionSchema>;

// One scope's spend report, read off the telemetry: what a `key` (a run, a step, a model, ...)
// cost over the window, the calls behind it, and the tokens by kind. `cost_usd` rides the wire as
// a decimal string, never a float, so nothing is lost between the ledger and the reader.
export const SpendLineSchema = z.object({
  key: z.string(),
  calls: z.number(),
  cost_usd: z.string(),
  input_tokens: z.number(),
  output_tokens: z.number(),
  cache_read_tokens: z.number(),
  cache_write_tokens: z.number(),
});
export type SpendLine = z.infer<typeof SpendLineSchema>;

// `available: true` carries the report; `available: false` carries why the telemetry could not
// answer and where a narrower answer still lives.
export const SpendAnswerSchema = z.discriminatedUnion("available", [
  z.object({
    available: z.literal(true),
    scope: z.string(),
    subject: z.string(),
    since_hours: z.number(),
    lines: z.array(SpendLineSchema),
    total: SpendLineSchema,
    source: z.literal("telemetry"),
  }),
  z.object({
    available: z.literal(false),
    scope: z.string(),
    subject: z.string(),
    since_hours: z.number(),
    reason: z.string(),
    fallback: z.string(),
  }),
]);
export type SpendAnswer = z.infer<typeof SpendAnswerSchema>;
