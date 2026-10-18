import type { ReactElement } from "react";

// The right pane with no project selected -- the rail and the header live in this route's
// layout, so there is nothing else to fetch or render here.
export default function ProjectsPage(): ReactElement {
  return <p className="text-body text-label-3">Pick a project.</p>;
}
