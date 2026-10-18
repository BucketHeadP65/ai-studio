import type { ReactElement } from "react";

import { WorkflowLibrary } from "@/components/workflows/workflow-library";

// The browsable home for every stored workflow artifact, off `/workflows/library`.

export default function WorkflowLibraryPage(): ReactElement {
  return <WorkflowLibrary />;
}
