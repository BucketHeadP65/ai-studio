// The query keys the stations share, grouped by the change topic that invalidates each one.
//
// The push channel turns a topic into a key prefix: "run.wf.report.v1" becomes
// ["run", "wf.report.v1"], "sessions" becomes ["sessions"]. React Query matches by prefix, so a
// query only refetches on a topic when its key STARTS with that topic's key. Every key here
// therefore begins with the root of the topic that must reach it.
//
// A workflow id always carries dots (wf.<name>.v<n>), so the two collection keys under "run"
// ("live" and "recent") can never collide with a workflow's own key. "steps" (below, in
// `stepKeys`) is the same kind of collection key and cannot collide either.

export const runKeys = {
  /** Every live run, the fleet pool behind GET /api/runs. */
  live: ["run", "live"] as const,
  /** The newest archived runs, GET /api/runs/recent. */
  recent: ["run", "recent"] as const,
  /**
   * The newest archived runs at one caller's own size, GET /api/runs/recent?limit=.
   *
   * The size is part of the key, so two screens asking for different amounts never read each
   * other's answer. It still sits under `recent`, so anything invalidating that reaches it.
   */
  recentAtMost: (limit: number) => ["run", "recent", limit] as const,
  /** One workflow's own run history. */
  history: (workflowId: string) => ["run", workflowId, "history"] as const,
  /** One run's step state, as the panels read it. */
  state: (workflowId: string, sessionId: string | null) =>
    ["run", workflowId, "state", sessionId] as const,
  /** The same state read by the inspector's timeline, kept apart so the two never share a fetch. */
  timeline: (workflowId: string, sessionId: string | null) =>
    ["run", workflowId, "timeline", sessionId] as const,
  /** What each node of one run produced. */
  values: (workflowId: string, sessionId: string | null) =>
    ["run", workflowId, "values", sessionId] as const,
  /** One artifact of one archived run, keyed by the digest so two versions never share a fetch. */
  artifact: (runId: string, artifactId: string, digest: string) =>
    ["run", "artifact", runId, artifactId, digest] as const,
};

// What this deployment answers about itself rather than about one run. Nothing publishes a
// "studio" topic, so these keys refetch on their own invalidation alone, which is what a fact that
// only changes when the deployment is recomposed needs.
export const studioKeys = {
  /** Every money ceiling a run started here is held to, and the gate modes it may ask for. */
  runLimits: ["studio", "run-limits"] as const,
};

// A run's own root ("run") gets a push frame on every node start and finish, so a key that must
// NOT refetch on every node transition sits under its own root instead, out of that root's reach.
export const spendKeys = {
  /** One run's spend report from the telemetry. */
  run: (runId: string) => ["spend", "run", runId] as const,
};

export const sessionKeys = {
  /** The session list, filtered by archived and a search string. */
  list: (archived: boolean, query: string) => ["sessions", "list", archived, query] as const,
  /** Every project the sessions API knows. */
  projects: ["sessions", "projects"] as const,
  /** One session's own header facts. */
  detail: (sessionId: string | null) => ["session", sessionId, "detail"] as const,
  /** One session's transcript. */
  transcript: (sessionId: string) => ["session", sessionId, "transcript"] as const,
  /** One session's conversation panel. */
  conversation: (sessionId: string | null) => ["session", sessionId, "conversation"] as const,
};

export const stepKeys = {
  // Under the "run" root, not "session": a step line changes on the same push frames a run's
  // own progress does (`run.{workflow_id}` on every node start and finish), and that topic's
  // root invalidation reaches every key starting with "run" -- never a specific workflow id,
  // since a line is keyed by session, not workflow. Sitting under "session" instead left this
  // stranded: nothing publishes a `session.{id}` frame on a line transition.
  /** One session's open agentic-step lines, the step drawer's item picker and composer gate. */
  lines: (sessionId: string) => ["run", "steps", sessionId] as const,
};

export const scheduleKeys = {
  /** Every schedule. */
  all: ["schedules"] as const,
};

export const knowledgeBaseKeys = {
  /** Every knowledge base the studio knows. */
  all: ["knowledge-base"] as const,
  /** One base's vitals. */
  detail: (name: string) => ["knowledge-base", name] as const,
  /** One base's parsed schema. */
  schema: (name: string | null) => ["knowledge-base", name, "schema"] as const,
  /** One base's refresh history. */
  refreshRuns: (name: string) => ["knowledge-base", name, "refresh-runs"] as const,
};

// The inspection's own root. Nothing publishes an "inspection" topic yet, so these keys refetch on their own
// invalidation only, which is what the ask route does when it appends an answer.
export const inspectionKeys = {
  /** The findings list under one set of filters. */
  findings: (filters: Record<string, string | number | undefined>) =>
    ["inspection", "findings", filters] as const,
  /** One finding, its lead and its evidence. */
  finding: (id: number) => ["inspection", "finding", id] as const,
  /** One question and the conversation it was answered in. */
  question: (id: number) => ["inspection", "question", id] as const,
  /** The turn under investigation for one finding. */
  turn: (findingId: number) => ["inspection", "finding", findingId, "turn"] as const,
  /** Everything the inspection did to one run. */
  run: (runId: string) => ["inspection", "run", runId] as const,
  /** The run in one screen: its steps and the record's facts. */
  outline: (runId: string) => ["inspection", "run", runId, "outline"] as const,
  /** The messages one step of a run added. */
  step: (runId: string, step: number) => ["inspection", "run", runId, "step", step] as const,
};
