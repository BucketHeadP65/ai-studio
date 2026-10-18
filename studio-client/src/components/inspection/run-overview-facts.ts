import type { Finding, Job, Lead, Question, RunOverview } from "@/api/inspection";
import { spaced, spanStep } from "@/api/inspection-words";

// What the run overview derives from the inspection's rows: which steps carry a finding or a dismissed lead,
// how a lead was settled, which finding a question decided, and the count phrases over each table.

export type StepMark = "finding" | "dismissed" | "unresolved";

const FINISHED_STATES: ReadonlySet<string> = new Set(["done", "abandoned"]);

export type Settled =
  | { kind: "finding"; finding: Finding }
  | { kind: "dismissed" | "undecided" | "invalid"; refusal: string }
  | { kind: "open" };

export function settledOf(lead: Lead, findings: Finding[]): Settled {
  const finding = findings.find((candidate) => candidate.lead_id === lead.lead_id);
  if (finding !== undefined) return { kind: "finding", finding };
  if (lead.resolution !== "open") return { kind: lead.resolution, refusal: lead.refusal ?? "" };
  if (lead.attempted_at !== null) return { kind: "undecided", refusal: lead.refusal ?? "" };
  return { kind: "open" };
}

export function markedSteps(overview: RunOverview): Map<number, StepMark> {
  const marks = new Map<number, StepMark>();
  for (const lead of overview.leads) {
    const settled = settledOf(lead, overview.findings);
    if (settled.kind === "open") continue;
    for (const span of lead.spans) {
      const step = spanStep(span);
      const mark = settled.kind === "undecided" || settled.kind === "invalid" ? "unresolved" : settled.kind;
      const previous = marks.get(step);
      if (mark === "finding" || (previous !== "finding" && (mark === "unresolved" || previous !== "unresolved"))) {
        marks.set(step, mark);
      }
    }
  }
  return marks;
}

export function findingOfQuestion(question: Question, findings: Finding[]): Finding | null {
  return findings.find((finding) => finding.question_id === question.question_id) ?? null;
}

// Only a finding or an explicit dismissal settles a lead. Failed or uncertain attribution remains unresolved.
export function leadsPhrase(leads: Lead[], findings: Finding[]): string {
  const total = leads.length;
  if (total === 0) return "0";
  const settled = leads.filter((lead) => ["finding", "dismissed"].includes(settledOf(lead, findings).kind)).length;
  if (total === 1) return settled === 1 ? "1, settled" : "1, not yet settled";
  if (settled === 0) return `${total}, none settled`;
  if (settled === total) return total === 2 ? "2, both settled" : `${total}, all settled`;
  return `${total}, ${settled} settled`;
}

export function questionsPhrase(questions: Question[]): string {
  if (questions.length === 0) return "0";
  const tiers = new Set(questions.map((question) => question.tier));
  if (tiers.size === 1) {
    const [tier] = tiers;
    return `${questions.length}, all on the ${tier} tier`;
  }
  return `${questions.length}`;
}

export function jobsPhrase(jobs: Job[]): string {
  if (jobs.length === 0) return "0";
  const unfinished = jobs.filter((job) => !FINISHED_STATES.has(job.state)).length;
  if (unfinished === 0) return `${jobs.length}, all finished`;
  return `${jobs.length}, ${unfinished} still running`;
}

export function jobLabel(job: Job, leads: Lead[]): { kind: string; detail: string } {
  if (job.kind !== "attribute_lead") return { kind: job.kind, detail: "" };
  const lead = leads.find((candidate) => String(candidate.lead_id) === job.key);
  return { kind: job.kind, detail: lead === undefined ? "" : spaced(lead.symptom) };
}
