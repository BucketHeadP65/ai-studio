import { ApiError, createApiClient } from "client-core/api/client";
import { z } from "zod";

import { KNOWLEDGE_BASE_API_URL } from "./config";
import {
  DecisionResultSchema,
  DecisionsSchema,
  GraphResultSchema,
  InitReportSchema,
  JobSchema,
  JobStartedSchema,
  KnowledgeBaseDetailSchema,
  KnowledgeBaseListResultSchema,
  KnowledgeBaseSchemaSchema,
  NotePreviewSchema,
  RefreshRunsResultSchema,
  RegistrationReportSchema,
  RemoveWatchEntryResultSchema,
  RetainedNoteSchema,
  ScheduleReportSchema,
  SourceQueueEntrySchema,
  TemplatesResultSchema,
  WatchEntrySchema,
} from "./types";
import type {
  DecisionResult,
  Decisions,
  GraphResult,
  InitReport,
  Job,
  JobStarted,
  KnowledgeBaseDetail,
  KnowledgeBaseListResult,
  KnowledgeBaseSchema,
  NoteDraft,
  NotePreview,
  RefreshRunsResult,
  RegistrationReport,
  RemoveWatchEntryResult,
  RetainedNote,
  Schedule,
  ScheduleReport,
  SourceQueueEntry,
  TemplatesResult,
  WatchEntry,
} from "./types";
import type { ArtifactRef } from "./studio-types";

// The `{error, detail}` shape studio/knowledge_base/http/api.py's `_error_response` answers a
// refusal with — parsed (not merely cast) so a body that doesn't actually match falls through to
// the raw message below rather than reading fields that were never really there.
const ErrorBodySchema = z.object({ detail: z.string().optional(), error: z.string().optional() });

// The 409 the refresh route answers when a run for this knowledge base is already queued or
// running: the uniform `{error, detail}` plus the job to watch. A different 409 (a broken
// registry row) carries no `job_id` and so fails this parse, which is what keeps it an error.
const RefreshConflictSchema = z.object({
  error: z.string(),
  detail: z.string(),
  job_id: z.number(),
});

// What one press of "Refresh now" produced: the job to watch, and whether it was already going.
export interface RefreshStart {
  jobId: number;
  alreadyRunning: boolean;
}

// The job named by an in-progress-run 409, or null when the error is anything else.
function refreshConflictJobId(error: unknown): number | null {
  if (!(error instanceof ApiError) || error.status !== 409) {
    return null;
  }
  try {
    return RefreshConflictSchema.parse(JSON.parse(error.message)).job_id;
  } catch {
    return null;
  }
}

// The knowledge base API client — each knowledge base's link graph (rendered on the
// Knowledge-bases detail page's Graph tab), plus the Knowledge-bases station's registry
// surface (list, create, read one schema). The generic
// request/parse/error plumbing (request<T>, ApiError, the 401/204 boundary) lives in
// client-core; this module injects the one app-local concern — the base URL — and
// builds the typed `knowledgeBaseApi` object on top. The surface is UNAUTHENTICATED: no
// getToken, no onUnauthorized; the client is local-only.
//
// Every response is zod-parsed via ./types, so a backend contract drift surfaces
// as a ZodError at the boundary rather than downstream in a view.

export { ApiError };

const { request } = createApiClient({ baseUrl: KNOWLEDGE_BASE_API_URL });

// The backend answers a refusal as `{error, detail}` JSON (studio/knowledge_base/http/api.py's
// `_error_response`) — a human sentence in `detail`, a machine tag in `error`. This is the
// one place that unwraps it, so a view shows the sentence, never the raw body.
export function knowledgeBaseErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    try {
      const body = ErrorBodySchema.parse(JSON.parse(error.message));
      return body.detail ?? body.error ?? error.message;
    } catch {
      return error.message;
    }
  }
  return error instanceof Error ? error.message : String(error);
}

// The `POST /api/knowledge-bases` body (KnowledgeBaseCreateRequest): a registry name, a target
// path, and either the knowledge base's schema or a template slug (exactly one of the two).
// `schema` is typed loosely by the caller (create-dialog.tsx's KnowledgeBaseSchemaDraftSchema) —
// this module has no opinion beyond the wire shape.
export interface CreateKnowledgeBaseRequest {
  name: string;
  path: string;
  schema?: Record<string, unknown>;
  template?: string;
  description?: string;
  force?: boolean;
}

// The PATCH .../pages/{page_id} body: the outcome's status, the link fields it asks for
// (each a single id, sent as the field's whole new value), and an optional reason.
export interface DecidePageBody {
  status: string;
  links?: Record<string, string[]>;
  reason?: string;
}

// Graph filters — page_type/rel, plus which knowledge base to read. No `collection`
// filter: the api.cards/api.edges surface exposes no collection column. `knowledge_base`
// names a registry entry; omitted, the backend answers the active knowledge base, and
// an unknown name answers 404.
export interface GraphFilters {
  page_type?: string;
  rel?: string;
  knowledge_base?: string;
}

function queryString(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

// What a keep sends: the request's own id, where the value came from, the draft exactly as the
// person confirmed it, and the two digests the preview answered with. The digests are carried back
// rather than recomputed here, since the whole point of them is that the host decides whether this
// is still the note that was read.
export interface RetainNoteBody {
  idempotency_key: string;
  origin: ArtifactRef;
  draft: NoteDraft;
  schema_digest: string;
  preview_digest: string;
}

export const knowledgeBaseApi = {
  graph: (filters: GraphFilters = {}): Promise<GraphResult> =>
    request<GraphResult>(`/api/graph${queryString({ ...filters })}`, GraphResultSchema),

  templates: (): Promise<TemplatesResult> => request<TemplatesResult>("/api/templates", TemplatesResultSchema),

  knowledgeBases: (): Promise<KnowledgeBaseListResult> =>
    request<KnowledgeBaseListResult>("/api/knowledge-bases", KnowledgeBaseListResultSchema),

  createKnowledgeBase: (payload: CreateKnowledgeBaseRequest): Promise<InitReport> =>
    request<InitReport>("/api/knowledge-bases", InitReportSchema, {
      method: "POST",
      body: JSON.stringify({
        name: payload.name,
        path: payload.path,
        ...(payload.schema !== undefined ? { schema: payload.schema } : {}),
        ...(payload.template !== undefined ? { template: payload.template } : {}),
        description: payload.description ?? "",
        force: payload.force ?? false,
      }),
    }),

  knowledgeBaseSchema: (name: string): Promise<KnowledgeBaseSchema> =>
    request<KnowledgeBaseSchema>(
      `/api/knowledge-bases/${encodeURIComponent(name)}/schema`,
      KnowledgeBaseSchemaSchema,
    ),

  knowledgeBaseDetail: (name: string): Promise<KnowledgeBaseDetail> =>
    request<KnowledgeBaseDetail>(
      `/api/knowledge-bases/${encodeURIComponent(name)}`,
      KnowledgeBaseDetailSchema,
    ),

  addSourceQueueEntry: (name: string, url: string): Promise<SourceQueueEntry> =>
    request<SourceQueueEntry>(
      `/api/knowledge-bases/${encodeURIComponent(name)}/source-queue`,
      SourceQueueEntrySchema,
      {
        method: "POST",
        body: JSON.stringify({ url }),
      },
    ),

  retrySourceQueueEntry: (name: string, key: string): Promise<SourceQueueEntry> =>
    request<SourceQueueEntry>(
      `/api/knowledge-bases/${encodeURIComponent(name)}/source-queue/${encodeURIComponent(key)}/retry`,
      SourceQueueEntrySchema,
      {
        method: "POST",
      },
    ),

  addWatchEntry: (name: string, scanner: WatchEntry["scanner"], ref: string): Promise<WatchEntry> =>
    request<WatchEntry>(`/api/knowledge-bases/${encodeURIComponent(name)}/watch-list`, WatchEntrySchema, {
      method: "POST",
      body: JSON.stringify({ scanner, ref }),
    }),

  removeWatchEntry: (name: string, scanner: WatchEntry["scanner"], ref: string): Promise<RemoveWatchEntryResult> =>
    request<RemoveWatchEntryResult>(
      `/api/knowledge-bases/${encodeURIComponent(name)}/watch-list${queryString({ scanner, ref })}`,
      RemoveWatchEntryResultSchema,
      { method: "DELETE" },
    ),

  decisions: (name: string): Promise<Decisions> =>
    request<Decisions>(`/api/knowledge-bases/${encodeURIComponent(name)}/decisions`, DecisionsSchema),

  decidePage: (name: string, pageId: string, body: DecidePageBody): Promise<DecisionResult> =>
    request<DecisionResult>(
      `/api/knowledge-bases/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}`,
      DecisionResultSchema,
      {
        method: "PATCH",
        body: JSON.stringify(body),
      },
    ),

  startRefresh: async (name: string): Promise<RefreshStart> => {
    try {
      const started = await request<JobStarted>(
        `/api/knowledge-bases/${encodeURIComponent(name)}/refresh`,
        JobStartedSchema,
        { method: "POST" },
      );
      return { jobId: started.job_id, alreadyRunning: false };
    } catch (error) {
      const running = refreshConflictJobId(error);
      if (running === null) {
        throw error;
      }
      return { jobId: running, alreadyRunning: true };
    }
  },

  readJob: (jobId: number): Promise<Job> => request<Job>(`/api/jobs/${jobId}`, JobSchema),

  refreshRuns: (name: string): Promise<RefreshRunsResult> =>
    request<RefreshRunsResult>(`/api/knowledge-bases/${encodeURIComponent(name)}/refresh-runs`, RefreshRunsResultSchema),

  setSchedule: (name: string, schedule: Schedule): Promise<ScheduleReport> =>
    request<ScheduleReport>(
      `/api/knowledge-bases/${encodeURIComponent(name)}/schedule`,
      ScheduleReportSchema,
      { method: "PUT", body: JSON.stringify(schedule) },
    ),

  clearSchedule: (name: string): Promise<ScheduleReport> =>
    request<ScheduleReport>(
      `/api/knowledge-bases/${encodeURIComponent(name)}/schedule`,
      ScheduleReportSchema,
      { method: "DELETE" },
    ),

  previewNote: (name: string, origin: ArtifactRef, editedDraft?: NoteDraft): Promise<NotePreview> =>
    request<NotePreview>(`/api/knowledge-bases/${encodeURIComponent(name)}/notes/preview`, NotePreviewSchema, {
      method: "POST",
      body: JSON.stringify({
        origin,
        ...(editedDraft !== undefined ? { edited_draft: editedDraft } : {}),
      }),
    }),

  retainNote: (name: string, body: RetainNoteBody): Promise<RetainedNote> =>
    request<RetainedNote>(`/api/knowledge-bases/${encodeURIComponent(name)}/notes`, RetainedNoteSchema, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  registerKnowledgeBase: (name: string, path: string, description = ""): Promise<RegistrationReport> =>
    request<RegistrationReport>(
      `/api/knowledge-bases/${encodeURIComponent(name)}/registration`,
      RegistrationReportSchema,
      { method: "PUT", body: JSON.stringify({ path, description }) },
    ),

  unregisterKnowledgeBase: (name: string): Promise<RegistrationReport> =>
    request<RegistrationReport>(
      `/api/knowledge-bases/${encodeURIComponent(name)}/registration`,
      RegistrationReportSchema,
      { method: "DELETE" },
    ),
};
