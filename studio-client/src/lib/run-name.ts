// What a run is called on a board, in one place, because it is a rule rather than a detail: a run
// carries its own title, and a run nobody named falls back to the workflow's card title and then to
// the workflow id. Never the title of the session it happens to run in. One session can host
// several runs, so that name could never have named any of them.
//
// `titleOf` is the caller's own lookup, so a screen holding a library listing and one holding
// per-workflow details each keep their own map and still read the fallback the same way.

export function runName(
  runTitle: string,
  workflowId: string,
  titleOf: (workflowId: string) => string | undefined,
): string {
  if (runTitle !== "") return runTitle;
  const title = titleOf(workflowId) ?? "";
  return title !== "" ? title : workflowId;
}
