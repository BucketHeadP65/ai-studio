// The station vocabulary the server renders as prose, and the ids that name what a person is
// looking at within a station. Adding a page to the app means adding a row here; entries are
// matched in order, so a more specific route (`/workflows/library`) must sit above the family it
// would otherwise fall into (`/workflows/{id}`).

type RouteMatch = [test: (pathname: string) => boolean, page: string];

function exact(path: string): (pathname: string) => boolean {
  return (pathname) => pathname === path;
}

function under(prefix: string): (pathname: string) => boolean {
  return (pathname) => pathname.startsWith(prefix);
}

const LIBRARY_PREFIX = "/workflows/library/";
const WORKFLOWS_PREFIX = "/workflows/";
const KNOWLEDGE_BASES_PREFIX = "/knowledge-bases/";
const PROJECTS_PREFIX = "/projects/";

const ROUTES: RouteMatch[] = [
  // The app's landing route, which redirects to the chat station, so it names the same page.
  [exact("/"), "chat"],
  [exact("/chat"), "chat"],
  [exact("/workflows"), "fleet"],
  [exact("/workflows/draft"), "draft"],
  [exact("/workflows/library"), "library"],
  [under(LIBRARY_PREFIX), "workflow-about"],
  [exact("/workflows/schedules"), "schedules"],
  [under(WORKFLOWS_PREFIX), "run-inspector"],
  [exact("/projects"), "projects"],
  [under(PROJECTS_PREFIX), "project"],
  [exact("/knowledge-bases"), "knowledge-bases"],
  [under(KNOWLEDGE_BASES_PREFIX), "knowledge-base-detail"],
  [exact("/harness"), "harness"],
  [exact("/settings"), "settings"],
];

export function pageOf(pathname: string): string {
  for (const [test, page] of ROUTES) {
    if (test(pathname)) return page;
  }
  return "unknown";
}

export function idsOf(pathname: string, search: URLSearchParams): Record<string, string> {
  switch (pageOf(pathname)) {
    case "chat": {
      const session = search.get("session");
      return session === null ? {} : { session };
    }
    case "workflow-about":
      return { workflow: pathname.slice(LIBRARY_PREFIX.length) };
    case "run-inspector": {
      const workflow = pathname.slice(WORKFLOWS_PREFIX.length);
      const run = search.get("run");
      return run === null ? { workflow } : { workflow, run };
    }
    case "knowledge-base-detail":
      return { knowledge_base: pathname.slice(KNOWLEDGE_BASES_PREFIX.length) };
    case "project":
      return { project: pathname.slice(PROJECTS_PREFIX.length) };
    default:
      return {};
  }
}
