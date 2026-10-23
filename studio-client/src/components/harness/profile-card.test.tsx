import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { ProfileCard } from "./profile-card";

afterEach(cleanup);

const BASE = "http://studio.test";

const PROFILES = {
  kind: "profile",
  artifacts: [
    {
      name: "researcher",
      status: "active",
      descriptor: "plugins: research, leveled-reads",
      last_error: null,
      promoted: null,
      next_step: null,
    },
    {
      name: "half_baked",
      status: "disabled",
      descriptor: "plugins: none",
      last_error: null,
      promoted: null,
      next_step: "activate",
    },
  ],
};

describe("<ProfileCard />", () => {
  it("offers the default plus every ACTIVE profile, never a pending one", async () => {
    server.use(
      http.get(`${BASE}/api/harness/selection`, () => HttpResponse.json({ name: null })),
      http.get(`${BASE}/api/harness/profile`, () => HttpResponse.json(PROFILES)),
    );

    render(<ProfileCard />);

    await screen.findByRole("option", { name: "researcher" });
    const picker = screen.getByRole("combobox", { name: "Profile" });
    const options = Array.from(picker.querySelectorAll("option")).map((option) => option.value);
    expect(options).toEqual(["", "researcher"]);
    expect(screen.queryByRole("option", { name: "half_baked" })).not.toBeInTheDocument();
  });

  it("selecting a profile puts it in effect and the card says so", async () => {
    let selected: string | null = null;
    server.use(
      http.get(`${BASE}/api/harness/selection`, () => HttpResponse.json({ name: selected })),
      http.get(`${BASE}/api/harness/profile`, () => HttpResponse.json(PROFILES)),
      http.put(`${BASE}/api/harness/selection`, async ({ request }) => {
        const body = (await request.json()) as { name: string | null };
        selected = body.name;
        return HttpResponse.json({ name: selected });
      }),
    );
    const user = userEvent.setup();

    render(<ProfileCard />);

    await screen.findByRole("option", { name: "researcher" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Profile" }), "researcher");

    await waitFor(() => expect(screen.getByText(/researcher governs/)).toBeInTheDocument());
  });

  it("a mid-conversation refusal is surfaced with the backend's sentence", async () => {
    server.use(
      http.get(`${BASE}/api/harness/selection`, () => HttpResponse.json({ name: null })),
      http.get(`${BASE}/api/harness/profile`, () => HttpResponse.json(PROFILES)),
      http.put(`${BASE}/api/harness/selection`, () =>
        HttpResponse.json(
          { detail: "A conversation is in progress. Forget it first, then switch the profile." },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();

    render(<ProfileCard />);

    await screen.findByRole("option", { name: "researcher" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Profile" }), "researcher");

    expect(await screen.findByRole("alert")).toHaveTextContent("Forget it first");
  });
});
