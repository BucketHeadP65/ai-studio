import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { ActiveProjectProvider, useActiveProject } from "./active-project";
import { ProjectDropdown } from "./project-dropdown";

afterEach(cleanup);

// ActiveProjectProvider persists to localStorage, which jsdom keeps across
// tests within one file -- clear it so each test starts with no active project.
beforeEach(() => {
  window.localStorage.clear();
});

const BASE = "http://studio.test";

const PROJECTS = [
  {
    id: "proj-1",
    name: "engram",
    root_path: "/home/user/engram",
    missing: false,
    running_sessions: 2,
  },
  { id: "proj-2", name: "gone", root_path: "/tmp/gone", missing: true, running_sessions: 0 },
  {
    id: "proj-3",
    name: "harness",
    root_path: "/home/user/harness",
    missing: false,
    running_sessions: 0,
  },
];

function ActiveProjectProbe() {
  const { activeProjectId } = useActiveProject();
  return <span data-testid="active">{activeProjectId ?? "none"}</span>;
}

function setup() {
  server.use(http.get(`${BASE}/api/projects`, () => HttpResponse.json({ projects: PROJECTS })));
  return render(
    <ActiveProjectProvider>
      <ProjectDropdown />
      <ActiveProjectProbe />
    </ActiveProjectProvider>,
  );
}

describe("<ProjectDropdown />", () => {
  it("renders every project, with a running badge where running_sessions > 0", async () => {
    setup();

    await userEvent.click(await screen.findByRole("button", { name: /select a project/i }));

    expect(await screen.findByRole("menuitemradio", { name: /engram/ })).toHaveTextContent(
      "2 running",
    );
    expect(screen.getByRole("menuitemradio", { name: /harness/ })).not.toHaveTextContent("running");
  });

  it("a missing project renders dimmed and disabled", async () => {
    setup();

    await userEvent.click(await screen.findByRole("button", { name: /select a project/i }));

    const gone = await screen.findByRole("menuitemradio", { name: /gone/ });
    expect(gone).toBeDisabled();
    expect(gone).toHaveTextContent(/missing/i);
  });

  it("choosing a project fires the active-project context and closes the menu", async () => {
    setup();

    await userEvent.click(await screen.findByRole("button", { name: /select a project/i }));
    await userEvent.click(await screen.findByRole("menuitemradio", { name: /engram/ }));

    expect(screen.getByTestId("active")).toHaveTextContent("proj-1");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /engram/ })).toBeInTheDocument();
  });

  it("clicking a missing project does not fire the context", async () => {
    setup();

    await userEvent.click(await screen.findByRole("button", { name: /select a project/i }));
    await userEvent.click(await screen.findByRole("menuitemradio", { name: /gone/ }));

    expect(screen.getByTestId("active")).toHaveTextContent("none");
  });

  it("offers an Open folder entry that opens the dialog and closes the menu", async () => {
    setup();

    await userEvent.click(await screen.findByRole("button", { name: /select a project/i }));
    await userEvent.click(screen.getByRole("menuitem", { name: /open folder/i }));

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: /open folder/i })).toBeInTheDocument();
  });

  it("registering a project through the dialog selects it and refreshes the listing", async () => {
    setup();
    server.use(
      http.post(`${BASE}/api/projects`, () =>
        HttpResponse.json({
          project: { id: "proj-4", name: "new-one", root_path: "/home/user/new-one" },
        }),
      ),
    );

    await userEvent.click(await screen.findByRole("button", { name: /select a project/i }));
    await userEvent.click(screen.getByRole("menuitem", { name: /open folder/i }));
    await userEvent.type(screen.getByLabelText("Absolute path"), "/home/user/new-one");
    await userEvent.click(screen.getByRole("button", { name: "Open" }));

    await waitFor(() => expect(screen.getByTestId("active")).toHaveTextContent("proj-4"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the backend's refusal when the listing fails to load", async () => {
    server.use(
      http.get(`${BASE}/api/projects`, () =>
        HttpResponse.json({ detail: "database unavailable" }, { status: 500 }),
      ),
    );

    render(
      <ActiveProjectProvider>
        <ProjectDropdown />
      </ActiveProjectProvider>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
  });
});
