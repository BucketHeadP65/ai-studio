import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { SessionList } from "./session-list";

// The left panel's own network surface: two projects, four sessions, mocked
// end to end so an unmocked request fails loudly. The sessions handler holds
// mutable state so a rename/archive/delete round-trips into the next list
// fetch, the way the real backend would.

const BASE = "http://studio.test";

interface Session {
  id: string;
  project_id: string;
  title: string;
  started_at: string;
  last_activity_at: string;
  archived: boolean;
  answering: boolean;
  turn_count: number;
  usage: {
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
  };
  cost_usd: string;
  context: null;
}

function usage() {
  return { input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 };
}

const ALPHA: Session = {
  id: "s-alpha",
  project_id: "proj-engram",
  title: "Alpha",
  started_at: "2026-08-20T10:00:00Z",
  last_activity_at: "2026-08-20T10:00:00Z",
  archived: false,
  answering: false,
  turn_count: 3,
  usage: usage(),
  cost_usd: "0",
  context: null,
};

const BETA: Session = {
  id: "s-beta",
  project_id: "proj-engram",
  title: "Beta",
  started_at: "2026-08-22T09:00:00Z",
  last_activity_at: "2026-08-22T09:00:00Z",
  archived: false,
  answering: true,
  turn_count: 5,
  usage: usage(),
  cost_usd: "0",
  context: null,
};

const GAMMA: Session = {
  id: "s-gamma",
  project_id: "proj-harness",
  title: "Gamma",
  started_at: "2026-08-23T08:00:00Z",
  last_activity_at: "2026-08-23T08:00:00Z",
  archived: false,
  answering: false,
  turn_count: 1,
  usage: usage(),
  cost_usd: "0",
  context: null,
};

const DELTA: Session = {
  id: "s-delta",
  project_id: "proj-harness",
  title: "Delta",
  started_at: "2026-08-15T00:00:00Z",
  last_activity_at: "2026-08-15T00:00:00Z",
  archived: true,
  answering: false,
  turn_count: 8,
  usage: usage(),
  cost_usd: "0",
  context: null,
};

const PROJECTS = [
  { id: "proj-engram", name: "Engram", root_path: "/repo/engram" },
  { id: "proj-harness", name: "Harness", root_path: "/repo/harness" },
];

const PatchBodySchema = z.object({
  title: z.string().optional(),
  archived: z.boolean().optional(),
});

function paramId(value: string | readonly string[] | undefined): string {
  if (typeof value === "string") return value;
  return value?.[0] ?? "";
}

function mountApi(initial: Session[] = [ALPHA, BETA, GAMMA, DELTA]) {
  let sessions = [...initial];
  let lastListUrl = "";

  server.use(
    http.get(`${BASE}/api/projects`, () => HttpResponse.json({ projects: PROJECTS })),
    http.get(`${BASE}/api/sessions`, ({ request }) => {
      lastListUrl = request.url;
      const url = new URL(request.url);
      const archivedParam = url.searchParams.get("archived");
      const q = url.searchParams.get("q");
      let list = sessions;
      // archived=true means the WHOLE set, mirroring the backend; anything
      // else hides archived rows from the listing.
      if (archivedParam !== "true") {
        list = list.filter((session) => !session.archived);
      }
      if (q) {
        list = list.filter((session) => session.title.toLowerCase().includes(q.toLowerCase()));
      }
      return HttpResponse.json({ sessions: list });
    }),
    http.patch(`${BASE}/api/sessions/:id`, async ({ params, request }) => {
      const id = paramId(params.id);
      const body = PatchBodySchema.parse(await request.json());
      const index = sessions.findIndex((session) => session.id === id);
      if (index === -1) {
        return HttpResponse.json({ detail: "session not found" }, { status: 404 });
      }
      const updated = { ...sessions[index], ...body };
      sessions = sessions.map((session, i) => (i === index ? updated : session));
      return HttpResponse.json({ session: updated });
    }),
    http.delete(`${BASE}/api/sessions/:id`, ({ params }) => {
      const id = paramId(params.id);
      const target = sessions.find((session) => session.id === id);
      if (!target) {
        return HttpResponse.json({ detail: "session not found" }, { status: 404 });
      }
      if (target.answering) {
        return HttpResponse.json(
          { detail: "a turn is in flight in this session" },
          { status: 409 },
        );
      }
      sessions = sessions.filter((session) => session.id !== id);
      return new HttpResponse(null, { status: 204 });
    }),
  );

  return { getLastListUrl: () => lastListUrl };
}

describe("SessionList", () => {
  it("groups sessions under their project, newest activity first", async () => {
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);

    const engramGroup = await screen.findByRole("group", { name: "Engram" });
    const titles = within(engramGroup)
      .getAllByText(/^(Alpha|Beta)$/)
      .map((el) => el.textContent);
    expect(titles).toEqual(["Beta", "Alpha"]);

    const harnessGroup = screen.getByRole("group", { name: "Harness" });
    expect(within(harnessGroup).getByText("Gamma")).toBeInTheDocument();
  });

  it("orders groups by newest activity across projects, not by raw list order", async () => {
    // The raw fixture order lists Engram's sessions (Alpha, Beta) before Harness's
    // (Gamma), but Gamma (2026-08-23) is more recent than Beta (2026-08-22) — the
    // newest session overall belongs to Harness, so Harness must render first.
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);
    await screen.findByText("Alpha");

    const headings = screen.getAllByRole("heading", { level: 3 }).map((el) => el.textContent);
    expect(headings).toEqual(["Harness", "Engram"]);
  });

  it("drives the q parameter from the search box", async () => {
    const api = mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);
    await screen.findByText("Alpha");

    await userEvent.click(screen.getByRole("button", { name: "Search sessions" }));
    const search = screen.getByRole("textbox", { name: "Search sessions" });
    await userEvent.type(search, "gamma");

    await waitFor(
      () => {
        const url = new URL(api.getLastListUrl());
        expect(url.searchParams.get("q")).toBe("gamma");
      },
      { timeout: 2000 },
    );
  });

  it("the archived toggle adds a separated Archived section beneath the active list", async () => {
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);

    await screen.findByText("Alpha");
    expect(screen.queryByText("Delta")).not.toBeInTheDocument();

    const toggle = screen.getByRole("button", { name: "Show archived" });
    await userEvent.click(toggle);

    const archivedSection = await screen.findByRole("group", { name: "Archived" });
    expect(within(archivedSection).getByText("Delta")).toBeInTheDocument();
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(within(archivedSection).queryByText("Alpha")).not.toBeInTheDocument();
  });

  it("a hidden row control is not clickable, the click opens the session instead", () => {
    // visibility: hidden removes hit testing, so the invisible rename/archive/
    // delete cluster can never swallow a click meant for the card.
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);

    return screen.findByText("Alpha").then((title) => {
      const row = title.closest("li");
      expect(row).not.toBeNull();
      const cluster = within(row as HTMLElement).getByRole("button", {
        name: "Session actions for Alpha",
      }).parentElement as HTMLElement;
      expect(cluster.className).toContain("invisible");
      expect(cluster.className).not.toContain("opacity-0");
    });
  });

  it("renames inline through PATCH and shows the new title", async () => {
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);
    await screen.findByText("Alpha");

    await userEvent.click(screen.getByRole("button", { name: "Session actions for Alpha" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const input = screen.getByRole("textbox", { name: "Rename session" });
    await userEvent.clear(input);
    await userEvent.type(input, "Alpha renamed{Enter}");

    expect(await screen.findByText("Alpha renamed")).toBeInTheDocument();
  });

  it("asks before delete and surfaces a 409 on the answering session", async () => {
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);
    await screen.findByText("Beta");

    await userEvent.click(screen.getByRole("button", { name: "Session actions for Beta" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));

    expect(await screen.findByText("a turn is in flight in this session")).toBeInTheDocument();
    expect(screen.getByText("Beta")).toBeInTheDocument();
  });

  it("multi-select shows the bulk bar and archives each selected id", async () => {
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);
    await screen.findByText("Alpha");

    await userEvent.click(screen.getByRole("checkbox", { name: "Select Alpha" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Gamma" }));

    expect(screen.getByText("2 selected")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Archive" }));

    await waitFor(() => {
      expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
      expect(screen.queryByText("Gamma")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Beta")).toBeInTheDocument();
  });

  it("ticking one session shows every other row's checkbox, so the next pick is one click", async () => {
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);
    await screen.findByText("Alpha");

    const gamma = screen.getByRole("checkbox", { name: "Select Gamma" });
    expect(gamma.className).toContain("invisible");

    await userEvent.click(screen.getByRole("checkbox", { name: "Select Alpha" }));

    expect(gamma.className).not.toContain("invisible");
    expect(gamma.className).toContain("visible");
  });

  it("Select all ticks every shown session, then Clear empties the selection", async () => {
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);
    await screen.findByText("Alpha");
    expect(screen.queryByRole("button", { name: "Select all" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("checkbox", { name: "Select Alpha" }));
    await userEvent.click(screen.getByRole("button", { name: "Select all" }));

    expect(screen.getByText("3 selected")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select Beta" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Select Gamma" })).toBeChecked();
    expect(screen.queryByRole("button", { name: "Select all" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Clear" }));

    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select Alpha" })).not.toBeChecked();
  });

  it("Select all covers the archived section when it is open", async () => {
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: "Show archived" }));
    await screen.findByText("Delta");

    await userEvent.click(screen.getByRole("checkbox", { name: "Select Alpha" }));
    await userEvent.click(screen.getByRole("button", { name: "Select all" }));

    expect(screen.getByText("4 selected")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select Delta" })).toBeChecked();
  });

  it("asks before a bulk delete, naming the count, then removes each selected id", async () => {
    mountApi();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={vi.fn()} />);
    await screen.findByText("Alpha");

    await userEvent.click(screen.getByRole("checkbox", { name: "Select Alpha" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Gamma" }));

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/Delete 2 sessions\?/)).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
      expect(screen.queryByText("Gamma")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Beta")).toBeInTheDocument();
  });

  it("the whole card views the session, not just its title", async () => {
    mountApi();
    const onView = vi.fn();
    render(<SessionList viewedId={null} onView={onView} onNewChat={vi.fn()} />);

    const title = await screen.findByText("Alpha");
    const row = title.closest("li") as HTMLElement;
    // The click lands on the row itself, not the title button inside it.
    fireEvent.click(row);

    expect(onView).toHaveBeenCalledWith("s-alpha");
  });

  it("a click on the row's own controls never counts as viewing", async () => {
    mountApi();
    const onView = vi.fn();
    render(<SessionList viewedId={null} onView={onView} onNewChat={vi.fn()} />);
    await screen.findByText("Alpha");

    await userEvent.click(screen.getByRole("button", { name: "Session actions for Alpha" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Rename" }));

    expect(onView).not.toHaveBeenCalled();
  });

  it("New chat calls onNewChat", async () => {
    mountApi();
    const onNewChat = vi.fn();
    render(<SessionList viewedId={null} onView={vi.fn()} onNewChat={onNewChat} />);
    await screen.findByText("Alpha");

    await userEvent.click(screen.getByRole("button", { name: "New chat" }));

    expect(onNewChat).toHaveBeenCalledTimes(1);
  });
});
