import { createApiClient } from "client-core/api/client";

import {
  type HarnessAction,
  HarnessActionSchema,
  type HarnessComposition,
  HarnessCompositionSchema,
  type HarnessDeletion,
  HarnessDeletionSchema,
  type HarnessKind,
  type HarnessListing,
  HarnessListingSchema,
  type ModelsListing,
  ModelsListingSchema,
  type PluginState,
  PluginStateSchema,
  type PluginsListing,
  PluginsListingSchema,
  type PromotionAction,
  PromotionActionSchema,
  type Selection,
  SelectionSchema,
  type SpendAnswer,
  SpendAnswerSchema,
  type Thinking,
  type Tier,
  type TiersListing,
  TiersListingSchema,
} from "./harness-types";
import { STUDIO_API_URL } from "./studio-config";

// The harness station's typed API surface, over the studio agent backend
// (`studio.agent`, :8770) — the same origin the workflow studio talks to.
// Refusals arrive as FastAPI's `{detail}` shape; views unwrap them with
// `studioErrorMessage` from ./studio, which already reads that shape.

const { request } = createApiClient({ baseUrl: STUDIO_API_URL });

export const harnessApi = {
  composition: (): Promise<HarnessComposition> =>
    request<HarnessComposition>("/api/harness/composition", HarnessCompositionSchema),

  tiers: (): Promise<TiersListing> =>
    request<TiersListing>("/api/harness/tiers", TiersListingSchema),

  setTier: (tier: Tier, body: { model: string; thinking: Thinking }): Promise<TiersListing> =>
    request<TiersListing>(`/api/harness/tiers/${tier}`, TiersListingSchema, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  resetTier: (tier: Tier): Promise<TiersListing> =>
    request<TiersListing>(`/api/harness/tiers/${tier}`, TiersListingSchema, { method: "DELETE" }),

  models: (q: string): Promise<ModelsListing> =>
    request<ModelsListing>(`/api/harness/models?q=${encodeURIComponent(q)}`, ModelsListingSchema),

  selection: (): Promise<Selection> =>
    request<Selection>("/api/harness/selection", SelectionSchema),

  select: (name: string | null): Promise<Selection> =>
    request<Selection>("/api/harness/selection", SelectionSchema, {
      method: "PUT",
      body: JSON.stringify({ name }),
    }),

  plugins: (): Promise<PluginsListing> =>
    request<PluginsListing>("/api/harness/plugins", PluginsListingSchema),

  setPlugin: (name: string, enabled: boolean): Promise<PluginState> =>
    request<PluginState>(
      `/api/harness/plugins/${encodeURIComponent(name)}/${enabled ? "enable" : "disable"}`,
      PluginStateSchema,
      { method: "POST" },
    ),

  list: (kind: HarnessKind): Promise<HarnessListing> =>
    request<HarnessListing>(`/api/harness/${kind}`, HarnessListingSchema),

  remove: (kind: HarnessKind, name: string): Promise<HarnessDeletion> =>
    request<HarnessDeletion>(
      `/api/harness/${kind}/${encodeURIComponent(name)}`,
      HarnessDeletionSchema,
      { method: "DELETE" },
    ),

  activate: (kind: HarnessKind, name: string): Promise<HarnessAction> =>
    request<HarnessAction>(
      `/api/harness/${kind}/${encodeURIComponent(name)}/activate`,
      HarnessActionSchema,
      { method: "POST" },
    ),

  disable: (kind: HarnessKind, name: string): Promise<HarnessAction> =>
    request<HarnessAction>(
      `/api/harness/${kind}/${encodeURIComponent(name)}/disable`,
      HarnessActionSchema,
      { method: "POST" },
    ),

  promote: (name: string): Promise<PromotionAction> =>
    request<PromotionAction>(
      `/api/harness/capability/${encodeURIComponent(name)}/promote`,
      PromotionActionSchema,
      { method: "POST" },
    ),

  demote: (name: string): Promise<PromotionAction> =>
    request<PromotionAction>(
      `/api/harness/capability/${encodeURIComponent(name)}/demote`,
      PromotionActionSchema,
      { method: "POST" },
    ),

  spend: (scope: string, subject: string, sinceHours = 24): Promise<SpendAnswer> =>
    request<SpendAnswer>(
      `/api/spend?scope=${encodeURIComponent(scope)}&subject=${encodeURIComponent(subject)}&since_hours=${sinceHours}`,
      SpendAnswerSchema,
    ),
};
