import type { ProvenanceActor } from "@/components/marks/provenance-chip";
import { provenance } from "@/components/marks/provenance-chip";

// The archive's own runner strings (`app:harness`, `app:chat`, `app:schedule:<id>`) name the
// route a run came through. Read here as who set it going, in the words and the icon every
// screen shares.

const SCHEDULE_PREFIX = "app:schedule:";

/** The short form for a facts cell: "chat", "a person at the controls", or "schedule <id>". */
export function runnerLabel(runner: string): string {
  if (runner.startsWith(SCHEDULE_PREFIX)) return `schedule ${runner.slice(SCHEDULE_PREFIX.length)}`;
  if (runner === "app:chat") return "chat";
  return "a person at the controls";
}

/** Which provenance actor, and so which icon, a runner string reads as. */
export function runnerActor(
  runner: string,
): Extract<ProvenanceActor, "schedule-started" | "person-started"> {
  return runner.startsWith(SCHEDULE_PREFIX) ? "schedule-started" : "person-started";
}

/** The full provenance sentence for a footer. */
export function startedBySentence(runner: string): string {
  if (runner.startsWith(SCHEDULE_PREFIX)) {
    return provenance.startedBySchedule(runner.slice(SCHEDULE_PREFIX.length));
  }
  if (runner === "app:chat") return provenance.startedFromChat();
  return provenance.startedByPerson();
}
