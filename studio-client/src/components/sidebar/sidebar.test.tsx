import { fireEvent, screen } from "@testing-library/dom";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunView } from "@/api/sessions-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

let pathnameValue = "/chat";

vi.mock("next/navigation", () => ({
  usePathname: () => pathnameValue,
}));

// Imported after the mock so the component sees the mocked module.
import { NAV_GROUPS } from "./nav-groups";
import { Sidebar } from "./sidebar";

const PLAIN_PUNCTUATION = /^[A-Za-z0-9 ,.?()]*$/;
const BASE = "http://studio.test";

function runViewFixture(overrides: Partial<RunView> = {}): RunView {
  return {
    session_id: "sess-1",
    workflow_id: "wf.demo.v1",
    run_title: "",
    state: "waiting",
    active: [],
    done: [],
    failed: [],
    waiting_at: null,
    gate: null,
    started_at: "",
    ...overrides,
  };
}

function mockRuns(runs: RunView[]): void {
  server.use(http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs })));
}

beforeEach(() => {
  window.localStorage.clear();
  // Most of this suite reads the nav's content, so it runs against the pinned rail; the
  // hidden-by-default behavior has its own tests below.
  window.localStorage.setItem("studio:sidebar:pinned", "1");
  pathnameValue = "/chat";
  mockRuns([]);
});

afterEach(() => {
  window.localStorage.clear();
});

describe("Sidebar", () => {
  it("renders every group's entries with their hrefs", () => {
    render(<Sidebar />);
    for (const group of NAV_GROUPS) {
      for (const entry of group.entries) {
        expect(screen.getByRole("link", { name: entry.label })).toHaveAttribute("href", entry.href);
      }
    }
  });

  it("leads the main group with Home and keeps Chat beside it", () => {
    const main = NAV_GROUPS.find((group) => group.id === "main");
    expect(main?.entries.map((entry) => entry.label)).toEqual(["Home", "Chat"]);

    render(<Sidebar />);
    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "Chat" })).toHaveAttribute("href", "/chat");
  });

  it("marks Home as here on the root alone, never on a station under it", () => {
    pathnameValue = "/";
    const { unmount } = render(<Sidebar />);
    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Chat" })).not.toHaveAttribute("aria-current");
    unmount();

    pathnameValue = "/workflows";
    render(<Sidebar />);
    expect(screen.getByRole("link", { name: "Home" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Fleet" })).toHaveAttribute("aria-current", "page");
  });

  it("highlights the active route with aria-current, and only that route", () => {
    pathnameValue = "/workflows/library";
    render(<Sidebar />);
    expect(screen.getByRole("link", { name: "Library" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Fleet" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Chat" })).not.toHaveAttribute("aria-current");
  });

  it("keeps Library active on a workflow's own what it does page, not Fleet", () => {
    pathnameValue = "/workflows/library/wf.x.v1";
    render(<Sidebar />);
    expect(screen.getByRole("link", { name: "Library" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Fleet" })).not.toHaveAttribute("aria-current");
  });

  it("gives Fleet, not Schedules, the active state on the bare fleet route", () => {
    pathnameValue = "/workflows";
    render(<Sidebar />);
    expect(screen.getByRole("link", { name: "Fleet" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Schedules" })).not.toHaveAttribute("aria-current");
  });

  it("gives Schedules, not Fleet, the active state on the schedules route", () => {
    pathnameValue = "/workflows/schedules";
    render(<Sidebar />);
    expect(screen.getByRole("link", { name: "Schedules" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Fleet" })).not.toHaveAttribute("aria-current");
  });

  it("marks a run's inspector page as still inside Fleet, not Library", () => {
    pathnameValue = "/workflows/wf.some-run.v1";
    render(<Sidebar />);
    expect(screen.getByRole("link", { name: "Fleet" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Library" })).not.toHaveAttribute("aria-current");
  });

  it("gives Settings a route-aware entry in the System group after Harness", () => {
    const system = NAV_GROUPS.find((group) => group.id === "system");
    expect(system?.entries.map((entry) => entry.label)).toEqual([
      "Harness",
      "Inspection",
      "Settings",
    ]);

    pathnameValue = "/settings";
    render(<Sidebar />);
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
    expect(screen.getByRole("link", { name: "Harness" })).not.toHaveAttribute("aria-current");
  });

  it("collapses a group and persists the choice across a remount", () => {
    const { unmount } = render(<Sidebar />);
    fireEvent.click(screen.getByRole("button", { name: "Workflows" }));
    expect(screen.queryByRole("link", { name: "Fleet" })).not.toBeInTheDocument();
    expect(window.localStorage.getItem("studio:sidebar:group:workflows")).toBe("0");
    unmount();

    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: "Fleet" })).not.toBeInTheDocument();
    // A sibling group's own state is untouched.
    expect(screen.getByRole("link", { name: "Chat" })).toBeInTheDocument();
  });

  it("carries a visible open or closed mark on the group heading, not aria-expanded alone", () => {
    render(<Sidebar />);
    const heading = screen.getByRole("button", { name: "Workflows" });
    expect(heading.querySelector("svg")).not.toBeNull();

    fireEvent.click(heading);
    expect(heading).toHaveAttribute("aria-expanded", "false");
    expect(heading.querySelector("svg")).not.toBeNull();
  });

  it("hides itself on Hide sidebar, leaving only the edge grip, and remembers", () => {
    const { unmount } = render(<Sidebar />);
    fireEvent.click(screen.getByRole("button", { name: "Hide sidebar" }));

    expect(screen.queryByText("AI Factory Studio")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Chat" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open navigation" })).toBeInTheDocument();
    expect(window.localStorage.getItem("studio:sidebar:pinned")).toBe("0");
    unmount();

    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: "Chat" })).not.toBeInTheDocument();
  });

  it("summons the overlay from the grip, and a nav click puts it away", () => {
    window.localStorage.removeItem("studio:sidebar:pinned");
    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: "Chat" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    const chat = screen.getByRole("link", { name: "Chat" });
    expect(chat).toBeVisible();

    fireEvent.click(chat);
    expect(screen.queryByRole("link", { name: "Chat" })).not.toBeInTheDocument();
  });

  it("pins from the overlay into the standing rail", () => {
    window.localStorage.removeItem("studio:sidebar:pinned");
    render(<Sidebar />);
    fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    fireEvent.click(screen.getByRole("button", { name: "Pin sidebar" }));

    expect(window.localStorage.getItem("studio:sidebar:pinned")).toBe("1");
    expect(screen.getByRole("button", { name: "Hide sidebar" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Chat" })).toBeVisible();
  });

  it("keeps every visible or accessible string to plain punctuation, pinned and hidden", () => {
    const { container, rerender } = render(<Sidebar />);
    const collect = (): string[] => {
      const strings = new Set<string>();
      for (const el of container.querySelectorAll("*")) {
        const label = el.getAttribute("aria-label");
        const title = el.getAttribute("title");
        if (label) strings.add(label);
        if (title) strings.add(title);
        if (el.children.length === 0 && el.textContent) strings.add(el.textContent);
      }
      return [...strings];
    };

    for (const text of collect()) {
      expect(text).toMatch(PLAIN_PUNCTUATION);
    }

    fireEvent.click(screen.getByRole("button", { name: "Hide sidebar" }));
    rerender(<Sidebar />);
    for (const text of collect()) {
      expect(text).toMatch(PLAIN_PUNCTUATION);
    }
  });

  it("carries no badge and no banner when nothing waits on a person", async () => {
    mockRuns([runViewFixture({ state: "running" })]);
    render(<Sidebar />);

    await screen.findByRole("link", { name: "Fleet" });
    expect(screen.queryByText(/waiting on you/)).not.toBeInTheDocument();
    expect(screen.queryByText(/gate/)).not.toBeInTheDocument();
  });

  it("shows the Fleet badge and the footer banner when runs wait on a person", async () => {
    mockRuns([
      runViewFixture({ session_id: "s-1", state: "waiting" }),
      runViewFixture({ session_id: "s-2", state: "waiting" }),
      runViewFixture({ session_id: "s-3", state: "running" }),
    ]);
    render(<Sidebar />);

    // Waits for the runs query to resolve, which is when both marks appear.
    await screen.findByText("2 gates are waiting on you.");

    const fleetLink = screen
      .getAllByRole("link")
      .find((link) => link.getAttribute("href") === "/workflows");
    expect(fleetLink?.textContent).toContain("2");

    expect(screen.getByRole("link", { name: "2 gates are waiting on you." })).toHaveAttribute(
      "href",
      "/workflows",
    );
  });

  it("writes the singular sentence for exactly one waiting run", async () => {
    mockRuns([runViewFixture({ state: "waiting" })]);
    render(<Sidebar />);

    expect(await screen.findByText("1 gate is waiting on you.")).toBeInTheDocument();
  });

  it("names the waiting count in the Fleet entry's own accessible name even while expanded", async () => {
    mockRuns([runViewFixture({ state: "waiting" })]);
    render(<Sidebar />);

    expect(await screen.findByRole("link", { name: "Fleet, 1 waiting on you" })).toHaveAttribute(
      "href",
      "/workflows",
    );
  });

  it("carries the waiting count on the hidden grip's own accessible name", async () => {
    mockRuns([
      runViewFixture({ session_id: "s-1", state: "waiting" }),
      runViewFixture({ session_id: "s-2", state: "waiting" }),
      runViewFixture({ session_id: "s-3", state: "waiting" }),
    ]);
    render(<Sidebar />);
    await screen.findByText("3 gates are waiting on you.");

    fireEvent.click(screen.getByRole("button", { name: "Hide sidebar" }));

    const grip = await screen.findByRole("button", {
      name: "Open navigation, 3 gates are waiting on you.",
    });
    expect(grip).toBeInTheDocument();
    // The banner went with the rail: the grip is all that stands, and it carries the count.
    expect(screen.queryByRole("link", { name: "3 gates are waiting on you." })).toBeNull();
  });
});
