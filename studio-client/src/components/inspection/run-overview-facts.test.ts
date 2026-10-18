import { describe, expect, it } from "vitest";

import type { Finding, Job, Lead, Question, RunOverview } from "@/api/inspection";

import {
  findingOfQuestion,
  jobLabel,
  jobsPhrase,
  leadsPhrase,
  markedSteps,
  questionsPhrase,
  settledOf,
} from "./run-overview-facts";

const SOURCE = "record:harness-author-d01d1796";

const SETTLED_LEAD: Lead = {
  lead_id: 1,
  source: SOURCE,
  watch: "every-run",
  symptom: "duplicate_call",
  spans: ["6.2.0", "7.2.0"],
  found_by: "duplicate_call",
  seen_at: "2026-09-10T12:06:00+00:00",
  attempted_at: null,
  refusal: null,
  resolution: "open",
};

const DISMISSED_LEAD: Lead = {
  lead_id: 2,
  source: SOURCE,
  watch: "every-run",
  symptom: "retried_after_error",
  spans: ["3.4.0", "3.6.0"],
  found_by: "retried_after_error",
  seen_at: "2026-09-10T12:07:00+00:00",
  attempted_at: "2026-09-10T12:07:30+00:00",
  refusal: "the retry at 3.6.0 changed the arguments after the error at 3.4.0",
  resolution: "dismissed",
};

const OPEN_LEAD: Lead = {
  lead_id: 3,
  source: SOURCE,
  watch: "every-run",
  symptom: "missing_tool",
  spans: ["9.1.0"],
  found_by: "missing_tool",
  seen_at: "2026-09-10T12:20:00+00:00",
  attempted_at: null,
  refusal: null,
  resolution: "open",
};

const LEADS = [SETTLED_LEAD, DISMISSED_LEAD];

const FINDING_FROM_LEAD: Finding = {
  finding_id: 1,
  lead_id: 1,
  question_id: null,
  source: SOURCE,
  symptom: "duplicate_call",
  symptom_text: "the agent called the same tool twice with the same arguments",
  explanation: "The agent repeated a call the first answer already covered.",
  found_by: "rule",
  model: null,
  decided_at: "2026-09-10T12:09:00+00:00",
  blames: [],
  lead_found_by: "duplicate_call",
};

const FINDING_FROM_QUESTION: Finding = {
  finding_id: 2,
  lead_id: null,
  question_id: 7,
  source: SOURCE,
  symptom: "warning_ignored",
  symptom_text: "the transcript shows a warning the agent never acted on",
  explanation: "The agent read a stale warning as a clean confirmation and ran the workflow anyway.",
  found_by: "inspector",
  model: "z-ai/glm-5.3-flash",
  decided_at: "2026-09-10T12:17:00+00:00",
  blames: [],
  lead_found_by: null,
};

const FINDINGS = [FINDING_FROM_LEAD, FINDING_FROM_QUESTION];

const QUESTIONS: Question[] = [
  {
    question_id: 7,
    conversation_id: "q-1",
    kind: "inspect",
    source: SOURCE,
    watch: "every-run",
    lead_id: null,
    tier: "helper",
    model: "z-ai/glm-5.3-flash",
    asked_at: "2026-09-10T12:16:00+00:00",
    answer: "Warning ignored, two blames.",
  },
  {
    question_id: 8,
    conversation_id: "q-2",
    kind: "attribute",
    source: SOURCE,
    watch: "every-run",
    lead_id: 2,
    tier: "helper",
    model: "z-ai/glm-5.3-flash",
    asked_at: "2026-09-10T12:07:00+00:00",
    answer: "Lead dismissed, the retry changed the arguments.",
  },
];

const JOBS: Job[] = [
  {
    id: 1,
    kind: "scan_run",
    key: `every-run:${SOURCE}`,
    state: "done",
    attempts: 1,
    result: "6 rules ran, 2 leads left",
    last_error: null,
    created_at: "2026-09-10T12:06:40+00:00",
    finished_at: "2026-09-10T12:06:41+00:00",
  },
  {
    id: 2,
    kind: "attribute_lead",
    key: "2",
    state: "done",
    attempts: 2,
    result: "attempt 2 dismissed the lead",
    last_error: null,
    created_at: "2026-09-10T12:07:02+00:00",
    finished_at: "2026-09-10T12:07:40+00:00",
  },
  {
    id: 3,
    kind: "inspect_run",
    key: `every-run:${SOURCE}`,
    state: "queued",
    attempts: 0,
    result: null,
    last_error: null,
    created_at: "2026-09-10T12:16:10+00:00",
    finished_at: null,
  },
];

const OVERVIEW: RunOverview = { source: SOURCE, leads: LEADS, findings: FINDINGS, questions: QUESTIONS, jobs: JOBS };

describe("settledOf", () => {
  it("keeps insufficient evidence unresolved rather than dismissing it", () => {
    const uncertain = { ...DISMISSED_LEAD, resolution: "undecided" as const, refusal: "Instructions were not recorded" };
    expect(settledOf(uncertain, [])).toEqual({ kind: "undecided", refusal: uncertain.refusal });
    expect(leadsPhrase([uncertain], [])).toBe("1, not yet settled");
  });

  it("keeps invalid model answers distinct from a dismissal", () => {
    const invalid = { ...DISMISSED_LEAD, resolution: "invalid" as const, refusal: "Unknown root cause twice" };
    expect(settledOf(invalid, [])).toEqual({ kind: "invalid", refusal: invalid.refusal });
    expect(leadsPhrase([invalid], [])).toBe("1, not yet settled");
  });
  it("finds the finding a lead was settled into", () => {
    expect(settledOf(SETTLED_LEAD, FINDINGS)).toEqual({ kind: "finding", finding: FINDING_FROM_LEAD });
  });

  it("reads a dismissal off a lead the inspector attempted and refused", () => {
    expect(settledOf(DISMISSED_LEAD, FINDINGS)).toEqual({ kind: "dismissed", refusal: DISMISSED_LEAD.refusal });
  });

  it("leaves a never-attempted lead open", () => {
    expect(settledOf(OPEN_LEAD, FINDINGS)).toEqual({ kind: "open" });
  });
});

describe("markedSteps", () => {
  it("marks a settled lead's spans as finding and a dismissed lead's spans as dismissed", () => {
    const marks = markedSteps(OVERVIEW);
    expect(marks.get(6)).toBe("finding");
    expect(marks.get(7)).toBe("finding");
    expect(marks.get(3)).toBe("dismissed");
    expect(marks.has(9)).toBe(false);
  });

  it("lets a finding win when a settled lead and a dismissed lead land on the same step", () => {
    const tiedFinding: Finding = { ...FINDING_FROM_LEAD, lead_id: 10 };
    const tiedLeads: Lead[] = [
      { ...SETTLED_LEAD, lead_id: 10, spans: ["4.1.0"] },
      { ...DISMISSED_LEAD, lead_id: 11, spans: ["4.5.0"] },
    ];
    const overview: RunOverview = { source: SOURCE, leads: tiedLeads, findings: [tiedFinding], questions: [], jobs: [] };
    expect(markedSteps(overview).get(4)).toBe("finding");
  });

  it("returns an empty map when the run raised no leads", () => {
    const overview: RunOverview = { source: SOURCE, leads: [], findings: [], questions: [], jobs: [] };
    expect(markedSteps(overview).size).toBe(0);
  });
});

describe("findingOfQuestion", () => {
  it("finds the finding a question decided", () => {
    expect(findingOfQuestion(QUESTIONS[0], FINDINGS)).toBe(FINDING_FROM_QUESTION);
  });

  it("answers null when no finding names the question", () => {
    expect(findingOfQuestion(QUESTIONS[1], FINDINGS)).toBeNull();
  });
});

describe("leadsPhrase", () => {
  it("reads 0 with no leads", () => {
    expect(leadsPhrase([], [])).toBe("0");
  });

  it("reads one settled lead", () => {
    expect(leadsPhrase([SETTLED_LEAD], FINDINGS)).toBe("1, settled");
  });

  it("reads one lead not yet settled", () => {
    expect(leadsPhrase([OPEN_LEAD], [])).toBe("1, not yet settled");
  });

  it("counts a dismissal as settled too: one lead became a finding, the other was dismissed", () => {
    expect(leadsPhrase(LEADS, FINDINGS)).toBe("2, both settled");
  });

  it("reads both settled when two leads both became findings", () => {
    const secondFinding: Finding = { ...FINDING_FROM_LEAD, finding_id: 3, lead_id: 3 };
    expect(leadsPhrase([SETTLED_LEAD, OPEN_LEAD], [FINDING_FROM_LEAD, secondFinding])).toBe("2, both settled");
  });

  it("counts a dismissal apart from a lead still open", () => {
    expect(leadsPhrase([DISMISSED_LEAD, OPEN_LEAD], FINDINGS)).toBe("2, 1 settled");
  });

  it("reads none settled when every lead is still open", () => {
    const secondOpen: Lead = { ...OPEN_LEAD, lead_id: 4 };
    expect(leadsPhrase([OPEN_LEAD, secondOpen], [])).toBe("2, none settled");
  });

  it("reads a partial count over three leads", () => {
    expect(leadsPhrase([SETTLED_LEAD, DISMISSED_LEAD, OPEN_LEAD], FINDINGS)).toBe("3, 2 settled");
  });
});

describe("questionsPhrase", () => {
  it("reads 0 with no questions", () => {
    expect(questionsPhrase([])).toBe("0");
  });

  it("names the shared tier when every question sits on one", () => {
    expect(questionsPhrase(QUESTIONS)).toBe("2, all on the helper tier");
  });

  it("drops the tier phrase when the tiers differ", () => {
    const mixed: Question[] = [QUESTIONS[0], { ...QUESTIONS[1], tier: "flash" }];
    expect(questionsPhrase(mixed)).toBe("2");
  });
});

describe("jobsPhrase", () => {
  it("reads 0 with no jobs", () => {
    expect(jobsPhrase([])).toBe("0");
  });

  it("counts the jobs still short of a final state", () => {
    expect(jobsPhrase(JOBS)).toBe("3, 1 still running");
  });

  it("reads all finished once every job reaches a final state", () => {
    const finished = JOBS.map((job) => ({ ...job, state: "done" }));
    expect(jobsPhrase(finished)).toBe("3, all finished");
  });
});

describe("jobLabel", () => {
  it("names the lead an attribute_lead job asked about", () => {
    expect(jobLabel(JOBS[1], LEADS)).toEqual({ kind: "attribute_lead", detail: "retried after error" });
  });

  it("carries no detail for a job that is not attribute_lead", () => {
    expect(jobLabel(JOBS[0], LEADS)).toEqual({ kind: "scan_run", detail: "" });
  });

  it("carries no detail when no lead matches the job's key", () => {
    const orphan: Job = { ...JOBS[1], key: "999" };
    expect(jobLabel(orphan, LEADS)).toEqual({ kind: "attribute_lead", detail: "" });
  });
});
