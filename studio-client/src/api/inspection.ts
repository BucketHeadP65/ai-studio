import { createApiClient } from "client-core/api/client";

import { z } from "zod";

import { type MessagePayload, MessagePayloadSchema } from "./sessions-types";
import { STUDIO_API_URL } from "./studio-config";

// The inspection surface over the harness backend (:8770): what the detectors found, the evidence behind one
// finding, the conversation the inspector answered in, everything one run cost the inspection, and one more
// question to the inspector that made a finding. Same shape as `studioApi`: client-core supplies
// request/parse/error, this module injects the base URL and builds the endpoint object.

const { request } = createApiClient({ baseUrl: STUDIO_API_URL });

export const BlameSchema = z.object({
  root_cause: z.string(),
  root_cause_text: z.string(),
  surface_kind: z.string(),
  surface_name: z.string(),
  why: z.string(),
});
export type Blame = z.infer<typeof BlameSchema>;

export const EvidenceSchema = z.object({
  span: z.string(),
  label: z.string(),
  kind: z.string(),
  text: z.string(),
});
export type Evidence = z.infer<typeof EvidenceSchema>;

export const LeadSchema = z.object({
  lead_id: z.number(),
  source: z.string(),
  watch: z.string(),
  symptom: z.string(),
  spans: z.array(z.string()),
  found_by: z.string(),
  seen_at: z.string().datetime({ offset: true }),
  attempted_at: z.string().datetime({ offset: true }).nullable(),
  refusal: z.string().nullable(),
  resolution: z.enum(["open", "undecided", "invalid", "dismissed"]).default("open"),
});
export type Lead = z.infer<typeof LeadSchema>;

export const FindingSchema = z.object({
  finding_id: z.number(),
  lead_id: z.number().nullable(),
  question_id: z.number().nullable(),
  source: z.string(),
  symptom: z.string(),
  symptom_text: z.string(),
  explanation: z.string(),
  found_by: z.string(),
  model: z.string().nullable(),
  decided_at: z.string().datetime({ offset: true }),
  blames: z.array(BlameSchema),
  lead_found_by: z.string().nullable(),
});
export type Finding = z.infer<typeof FindingSchema>;

export const FindingDetailSchema = z.object({
  finding: FindingSchema,
  lead: LeadSchema.nullable(),
  evidence: z.array(EvidenceSchema),
});
export type FindingDetail = z.infer<typeof FindingDetailSchema>;

export const QuestionSchema = z.object({
  question_id: z.number(),
  conversation_id: z.string(),
  kind: z.string(),
  source: z.string(),
  watch: z.string(),
  lead_id: z.number().nullable(),
  tier: z.string(),
  model: z.string(),
  asked_at: z.string().datetime({ offset: true }),
  answer: z.string().nullable(),
});
export type Question = z.infer<typeof QuestionSchema>;

// `payload` is pydantic-ai's own ModelMessage, JSON-shaped. Held as unknown here and parsed once through
// `conversationPayloads`, which drops a message this app cannot read rather than failing the whole page.
export const ConversationMessageSchema = z.object({
  run_id: z.string(),
  idx: z.number(),
  kind: z.string(),
  payload: z.unknown(),
  text: z.string(),
});
export type ConversationMessage = z.infer<typeof ConversationMessageSchema>;

export const QuestionPageSchema = z.object({
  question: QuestionSchema,
  messages: z.array(ConversationMessageSchema),
});
export type QuestionPage = z.infer<typeof QuestionPageSchema>;

// One step of the run under investigation: its own messages in the same five-field shape the
// question route answers, which of them hold a cited part -- indices into `messages` -- and
// `first`, the index in the step's own full history of `messages[0]` (a turn can window down to
// the exchange that matters, so this is not always 0).
export const TurnEntrySchema = z.object({
  step: z.number(),
  messages: z.array(ConversationMessageSchema),
  cited: z.array(z.number()),
  first: z.number(),
});
export type TurnEntry = z.infer<typeof TurnEntrySchema>;

export const TurnPageSchema = z.object({
  turns: z.array(TurnEntrySchema),
});
export type TurnPage = z.infer<typeof TurnPageSchema>;

export const JobSchema = z.object({
  id: z.number(),
  kind: z.string(),
  key: z.string(),
  state: z.string(),
  attempts: z.number(),
  result: z.string().nullable(),
  last_error: z.string().nullable(),
  created_at: z.string().datetime({ offset: true }),
  finished_at: z.string().datetime({ offset: true }).nullable(),
});
export type Job = z.infer<typeof JobSchema>;

export const RunOverviewSchema = z.object({
  source: z.string(),
  leads: z.array(LeadSchema),
  findings: z.array(FindingSchema),
  questions: z.array(QuestionSchema),
  jobs: z.array(JobSchema),
});
export type RunOverview = z.infer<typeof RunOverviewSchema>;

export const StepOutlineSchema = z.object({ step: z.number(), messages: z.number(), summary: z.string() });
export type StepOutline = z.infer<typeof StepOutlineSchema>;

export const RunOutlineSchema = z.object({
  source: z.string(),
  agent: z.string().nullable(),
  started_at: z.string().datetime({ offset: true }).nullable(),
  ended_at: z.string().datetime({ offset: true }).nullable(),
  ended: z.string().nullable(),
  model: z.string().nullable(),
  tool_calls: z.number(),
  steps: z.array(StepOutlineSchema),
});
export type RunOutline = z.infer<typeof RunOutlineSchema>;

export const AnswerSchema = z.object({ answer: z.string(), conversation_id: z.string(), model: z.string() });
export type Answer = z.infer<typeof AnswerSchema>;

const FindingsListSchema = z.object({ findings: z.array(FindingSchema) });

export interface FindingFilters {
  symptom?: string;
  source?: string;
  found_by?: string;
  limit?: number;
}

function queryString(filters: FindingFilters): string {
  const search = new URLSearchParams();
  if (filters.symptom !== undefined) search.set("symptom", filters.symptom);
  if (filters.source !== undefined) search.set("source", filters.source);
  if (filters.found_by !== undefined) search.set("found_by", filters.found_by);
  if (filters.limit !== undefined) search.set("limit", String(filters.limit));
  const text = search.toString();
  return text === "" ? "" : `?${text}`;
}

// One recorded conversation as the chat components read it. A message whose payload this app cannot parse is
// dropped, the same discipline the run inspector's own archived conversations follow.
export function conversationPayloads(messages: ConversationMessage[]): MessagePayload[] {
  return messages.flatMap((message) => {
    const candidate = MessagePayloadSchema.safeParse(message.payload);
    return candidate.success ? [candidate.data] : [];
  });
}

export const inspectionApi = {
  findings: (filters: FindingFilters = {}): Promise<Finding[]> =>
    request<z.infer<typeof FindingsListSchema>>(
      `/api/inspection/findings${queryString(filters)}`,
      FindingsListSchema,
    ).then((result) => result.findings),

  finding: (id: number): Promise<FindingDetail> =>
    request(`/api/inspection/findings/${id}`, FindingDetailSchema),

  question: (id: number): Promise<QuestionPage> =>
    request(`/api/inspection/questions/${id}`, QuestionPageSchema),

  turn: (findingId: number): Promise<TurnPage> =>
    request(`/api/inspection/findings/${findingId}/turn`, TurnPageSchema),

  run: (runId: string): Promise<RunOverview> =>
    request(`/api/inspection/runs/${encodeURIComponent(runId)}`, RunOverviewSchema),

  outline: (runId: string): Promise<RunOutline> =>
    request(`/api/inspection/runs/${encodeURIComponent(runId)}/outline`, RunOutlineSchema),

  step: (runId: string, step: number): Promise<TurnEntry> =>
    request(`/api/inspection/runs/${encodeURIComponent(runId)}/steps/${step}`, TurnEntrySchema),

  ask: (questionId: number, text: string): Promise<Answer> =>
    request(`/api/inspection/questions/${questionId}/ask`, AnswerSchema, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
};
