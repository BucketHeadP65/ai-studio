import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HarnessArtifact } from "@/api/harness-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { HarnessKindSection } from "./kind-section";

afterEach(cleanup);

const BASE = "http://studio.test";

function artifact(overrides: Partial<HarnessArtifact> = {}): HarnessArtifact {
  return {
    name: "summarize_day",
    status: "disabled",
    descriptor: "SummarizeDay",
    last_error: null,
    promoted: null,
    next_step: "activate",
    ...overrides,
  };
}

function section(onDiscuss = vi.fn()) {
  return (
    <HarnessKindSection
      kind="capability"
      title="Capabilities"
      blurb="What the agent wrote for itself."
      empty="Nothing custom yet."
      onDiscuss={onDiscuss}
    />
  );
}

describe("<HarnessKindSection />", () => {
  it("deletes a disabled artifact behind a confirm, then re-reads the shelf", async () => {
    let deleted = false;
    server.use(
      http.get(`${BASE}/api/harness/capability`, () =>
        HttpResponse.json({ kind: "capability", artifacts: deleted ? [] : [artifact()] }),
      ),
      http.delete(`${BASE}/api/harness/capability/summarize_day`, () => {
        deleted = true;
        return HttpResponse.json({ kind: "capability", name: "summarize_day", deleted: true });
      }),
    );
    const user = userEvent.setup();

    render(section());

    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Delete it" }));

    await waitFor(() => expect(screen.getByText("Nothing custom yet.")).toBeInTheDocument());
  });

  it("offers no Delete on an active artifact, the composition is not tidied by accident", async () => {
    server.use(
      http.get(`${BASE}/api/harness/capability`, () =>
        HttpResponse.json({
          kind: "capability",
          artifacts: [artifact({ status: "active", next_step: null })],
        }),
      ),
    );

    render(section());

    await screen.findByText("summarize_day");
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("hands the artifact to onDiscuss so the drawer opens scoped to it", async () => {
    const onDiscuss = vi.fn();
    server.use(
      http.get(`${BASE}/api/harness/capability`, () =>
        HttpResponse.json({ kind: "capability", artifacts: [artifact()] }),
      ),
    );
    const user = userEvent.setup();

    render(section(onDiscuss));

    await user.click(await screen.findByRole("button", { name: "Discuss" }));

    expect(onDiscuss).toHaveBeenCalledWith(
      expect.objectContaining({ name: "summarize_day", descriptor: "SummarizeDay" }),
    );
  });
});
