import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { PluginsCard } from "./plugins-card";

afterEach(cleanup);

const BASE = "http://studio.test";

const PLUGINS = {
  plugins: [
    {
      name: "research",
      description: "The knowledge bases' read plugin.",
      enabled: true,
      fixed: false,
    },
    {
      name: "sql",
      description: "The SQL plugin over the same knowledge bases.",
      enabled: false,
      fixed: false,
    },
    { name: "spend-limits", description: "The money brake.", enabled: true, fixed: true },
  ],
};

describe("<PluginsCard />", () => {
  it("lists every plugin: toggleable ones as switches, always-on as read only", async () => {
    server.use(
      http.get(`${BASE}/api/harness/plugins`, () => HttpResponse.json(PLUGINS)),
      http.get(`${BASE}/api/harness/selection`, () => HttpResponse.json({ name: null })),
    );

    render(<PluginsCard />);

    expect(await screen.findByRole("switch", { name: /research/ })).toBeChecked();
    expect(screen.getByRole("switch", { name: /sql/ })).not.toBeChecked();
    expect(screen.queryByRole("switch", { name: /spend-limits/ })).not.toBeInTheDocument();
    expect(screen.getByText("spend-limits")).toBeInTheDocument();
    expect(screen.getByText("always on")).toBeInTheDocument();
  });

  it("a flip posts the new state and re-reads the listing", async () => {
    let flipped = false;
    server.use(
      http.get(`${BASE}/api/harness/plugins`, () =>
        HttpResponse.json(
          flipped
            ? { plugins: [{ ...PLUGINS.plugins[1], enabled: true }] }
            : { plugins: [PLUGINS.plugins[1]] },
        ),
      ),
      http.post(`${BASE}/api/harness/plugins/sql/enable`, () => {
        flipped = true;
        return HttpResponse.json({ ...PLUGINS.plugins[1], enabled: true });
      }),
      http.get(`${BASE}/api/harness/selection`, () => HttpResponse.json({ name: null })),
    );
    const user = userEvent.setup();

    render(<PluginsCard />);

    await user.click(await screen.findByRole("switch", { name: /sql/ }));
    await waitFor(() => expect(screen.getByRole("switch", { name: /sql/ })).toBeChecked());
  });
  it("the switches stand down while a profile governs the plugins", async () => {
    server.use(
      http.get(`${BASE}/api/harness/plugins`, () => HttpResponse.json(PLUGINS)),
      http.get(`${BASE}/api/harness/selection`, () => HttpResponse.json({ name: "researcher" })),
    );

    render(<PluginsCard />);

    expect(await screen.findByRole("switch", { name: /research/ })).toBeDisabled();
    expect(screen.getByText(/follow the profile researcher/)).toBeInTheDocument();
  });
});
