import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { DecisionsCard } from "./decisions";

afterEach(cleanup);

const row = {
  id: "flag.try-planning-reminders",
  page_type: "flag",
  title: "Try planning reminders",
  summary: "A live task list re-injected every turn.",
  status: "pending",
  links: { about: ["talk.agents-that-plan"] },
};

const outcomesByPageType = {
  flag: [
    { status: "accepted", link: null },
    { status: "dismissed", link: "resolved_by" },
  ],
};

function mockDecisions(rows: unknown[] = [row], skipped: string[] = []) {
  server.use(
    http.get("*/api/knowledge-bases/harness/decisions", () =>
      HttpResponse.json({ rows, skipped, outcomes_by_page_type: outcomesByPageType }),
    ),
  );
}

describe("DecisionsCard", () => {
  it("lists a row with its page type and status as text", async () => {
    mockDecisions();
    render(<DecisionsCard name="harness" />);
    expect(await screen.findByText("Try planning reminders")).toBeInTheDocument();
    expect(screen.getByText("flag")).toBeInTheDocument();
    expect(screen.getByText("pending")).toBeInTheDocument();
  });

  it("decides a row through an outcome with no link", async () => {
    mockDecisions();
    let sentBody: unknown;
    server.use(
      http.patch(
        "*/api/knowledge-bases/harness/pages/flag.try-planning-reminders",
        async ({ request }) => {
          sentBody = await request.json();
          return HttpResponse.json({ ...row, status: "accepted", job_id: 1, index: "queued" });
        },
      ),
    );
    render(<DecisionsCard name="harness" />);
    await screen.findByText("Try planning reminders");

    await userEvent.click(screen.getByRole("button", { name: "Accepted" }));

    await waitFor(() => expect(sentBody).toEqual({ status: "accepted" }));
  });

  it("shows the page-id field for an outcome with a link, and sends its value", async () => {
    mockDecisions();
    let sentBody: unknown;
    server.use(
      http.patch(
        "*/api/knowledge-bases/harness/pages/flag.try-planning-reminders",
        async ({ request }) => {
          sentBody = await request.json();
          return HttpResponse.json({ ...row, status: "dismissed", job_id: 1, index: "queued" });
        },
      ),
    );
    render(<DecisionsCard name="harness" />);
    await screen.findByText("Try planning reminders");

    const input = screen.getByLabelText("Try planning reminders page id");
    await userEvent.type(input, "flag.duplicate-of");
    await userEvent.click(screen.getByRole("button", { name: "Dismissed" }));

    await waitFor(() =>
      expect(sentBody).toEqual({
        status: "dismissed",
        links: { resolved_by: ["flag.duplicate-of"] },
      }),
    );
  });

  it("says when nothing awaits a decision", async () => {
    mockDecisions([]);
    render(<DecisionsCard name="harness" />);
    expect(await screen.findByText("Nothing awaits a decision.")).toBeInTheDocument();
  });

  it("names the pages that failed to parse", async () => {
    mockDecisions([], ["a.md", "b.md"]);
    render(<DecisionsCard name="harness" />);
    expect(await screen.findByText("2 pages could not be read (a.md, b.md).")).toBeInTheDocument();
  });
});
