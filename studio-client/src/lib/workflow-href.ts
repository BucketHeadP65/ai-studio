// The two workflow routes, built in one place. They were three builders under two names before
// this, with one name meaning a different route in each of two files, which is how a link lands on
// the wrong page.
//
// `libraryHref` opens what a workflow is: its description, its parameters, its steps in execution
// order. `runHref` opens what a run did: the outcome, the artifacts, the graph. Given a run id it
// opens that run in particular, which is what a board row wants so a failure is one click from its
// own record rather than from whatever the workflow is doing now.

export function libraryHref(workflowId: string): string {
  return `/workflows/library/${encodeURIComponent(workflowId)}`;
}

export function runHref(workflowId: string, runId?: string): string {
  const base = `/workflows/${encodeURIComponent(workflowId)}`;
  return runId === undefined ? base : `${base}?run=${encodeURIComponent(runId)}`;
}

// The element id an artifact is reachable at on a run's page, so a link from elsewhere lands on
// the artifact rather than at the top of the page. Derived from the artifact id alone, by both
// sides, so a link and its target cannot drift apart.
export function artifactAnchorId(artifactId: string): string {
  return `artifact-${encodeURIComponent(artifactId)}`;
}

export function artifactHref(workflowId: string, runId: string, artifactId: string): string {
  return `${runHref(workflowId, runId)}#${artifactAnchorId(artifactId)}`;
}
