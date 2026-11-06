import { screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import type { MaintenanceFlag } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";
import { maintenanceFlag } from "@/test-support/factories/flags";

import { FlagCard } from "./flag-card";

// One maintenance flag as Home shows it: its kind in words, the workflow and step it is about,
// what it asks, its evidence and proposed change folded away until asked for, and the one act on
// it, resolving it through the shared modal with a typed name and a reason.

const BASE = "http://studio.test";
const RESOLVE = `${BASE}/api/library/flags/:flagId/resolve`;

function inList(flag: MaintenanceFlag, onResolved: () => void = vi.fn()): ReactElement {
  return (
    <ul>
      <FlagCard flag={flag} onResolved={onResolved} />
    </ul>
  );
}

describe("FlagCard reads one maintenance flag", () => {
  it("shows its kind in words, its workflow, its step and its summary", () => {
    const flag = maintenanceFlag();
    render(inList(flag));

    expect(screen.getByText("Diverged copy")).toBeInTheDocument();
    expect(screen.queryByText("diverged_copy")).not.toBeInTheDocument();
    expect(screen.getByText("wf.weekly-sweep.v2")).toBeInTheDocument();
    expect(screen.getByText("compose")).toBeInTheDocument();
    expect(screen.getByText(flag.summary)).toBeInTheDocument();
  });

  it("names no workflow for a flag about a step", () => {
    render(
      inList(
        maintenanceFlag({
          id: "mf.change.compose",
          kind: "repository_change",
          workflow: "",
          title: "Repository change to compose",
          summary: "Decide whether compose gains a length parameter its consumers set.",
        }),
      ),
    );

    expect(screen.getByText("Repository change")).toBeInTheDocument();
    expect(screen.getByText("compose")).toBeInTheDocument();
    expect(screen.queryByText(/workflow/i)).not.toBeInTheDocument();
  });

  it("shows its evidence only when asked", async () => {
    render(inList(maintenanceFlag()));

    const evidence = screen.getByRole("button", { name: "Evidence" });
    expect(evidence).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/holds a copy of compose/)).not.toBeInTheDocument();

    await userEvent.click(evidence);

    expect(evidence).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/holds a copy of compose/)).toHaveTextContent("+Write five lines.");
  });

  it("shows its proposed change only when asked", async () => {
    render(inList(maintenanceFlag({ proposed_change: "Add a length parameter to compose." })));

    const proposed = screen.getByRole("button", { name: "Proposed change" });
    expect(screen.queryByText("Add a length parameter to compose.")).not.toBeInTheDocument();

    await userEvent.click(proposed);

    expect(proposed).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Add a length parameter to compose.")).toBeInTheDocument();
  });

  it("offers no proposed change when it has none", () => {
    render(inList(maintenanceFlag({ proposed_change: "" })));

    expect(screen.getByRole("button", { name: "Evidence" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Proposed change" })).not.toBeInTheDocument();
    expect(screen.queryByText("Proposed change")).not.toBeInTheDocument();
  });
});

describe("FlagCard resolves a flag with a name and a reason", () => {
  it("opens the shared modal and refuses an empty reason", async () => {
    let posts = 0;
    server.use(
      http.post(RESOLVE, () => {
        posts += 1;
        return HttpResponse.json({ ok: true, flag: maintenanceFlag({ status: "resolved" }) });
      }),
    );
    render(inList(maintenanceFlag()));

    await userEvent.click(screen.getByRole("button", { name: "Resolve" }));
    const dialog = await screen.findByRole("dialog", { name: "Resolve this flag" });
    const resolve = within(dialog).getByRole("button", { name: "Resolve" });

    await userEvent.type(within(dialog).getByLabelText("Your name"), "Alex");
    expect(resolve).toBeDisabled();

    // Spaces are no reason: the harness refuses a blank one, so the modal never sends it.
    await userEvent.type(within(dialog).getByLabelText("Why"), "   ");
    expect(resolve).toBeDisabled();
    await userEvent.click(resolve);

    expect(posts).toBe(0);
    expect(screen.getByRole("dialog", { name: "Resolve this flag" })).toBeInTheDocument();
  });

  it("refuses a reason without a name, as approving a workflow does", async () => {
    render(inList(maintenanceFlag()));

    await userEvent.click(screen.getByRole("button", { name: "Resolve" }));
    const dialog = await screen.findByRole("dialog", { name: "Resolve this flag" });
    await userEvent.type(within(dialog).getByLabelText("Why"), "The copy is meant to differ.");

    expect(within(dialog).getByRole("button", { name: "Resolve" })).toBeDisabled();
  });

  it("posts the reason with the name typed, then closes and says the flag resolved", async () => {
    const flag = maintenanceFlag();
    const posted: { flagId: string; body: unknown }[] = [];
    server.use(
      http.post(RESOLVE, async ({ params, request }) => {
        posted.push({ flagId: String(params.flagId), body: await request.json() });
        return HttpResponse.json({ ok: true, flag: { ...flag, status: "resolved" } });
      }),
    );
    const onResolved = vi.fn();
    render(inList(flag, onResolved));

    await userEvent.click(screen.getByRole("button", { name: "Resolve" }));
    const dialog = await screen.findByRole("dialog", { name: "Resolve this flag" });
    await userEvent.type(within(dialog).getByLabelText("Your name"), "  Alex ");
    await userEvent.type(
      within(dialog).getByLabelText("Why"),
      "The weekly sweep needs its own wording. ",
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "Resolve" }));

    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
    expect(posted).toEqual([
      {
        flagId: "mf.diverged.weekly-sweep.compose",
        body: { by: "Alex", reason: "The weekly sweep needs its own wording." },
      },
    ]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps the modal open with the harness's own words when it refuses", async () => {
    server.use(
      http.post(RESOLVE, () =>
        HttpResponse.json(
          { ok: false, message: "No maintenance flag mf.diverged.weekly-sweep.compose." },
          { status: 404 },
        ),
      ),
    );
    const onResolved = vi.fn();
    render(inList(maintenanceFlag(), onResolved));

    await userEvent.click(screen.getByRole("button", { name: "Resolve" }));
    const dialog = await screen.findByRole("dialog", { name: "Resolve this flag" });
    await userEvent.type(within(dialog).getByLabelText("Your name"), "Alex");
    await userEvent.type(within(dialog).getByLabelText("Why"), "Settled already.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Resolve" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "No maintenance flag mf.diverged.weekly-sweep.compose.",
    );
    expect(onResolved).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Resolve this flag" })).toBeInTheDocument();
  });
});
