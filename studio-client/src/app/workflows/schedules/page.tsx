import type { ReactElement } from "react";

import { SchedulesPage } from "@/components/workflows/schedules-page";

// The management home for every standing, cadence-driven schedule, off `/workflows/schedules`.

export default function WorkflowSchedulesPage(): ReactElement {
  return <SchedulesPage />;
}
