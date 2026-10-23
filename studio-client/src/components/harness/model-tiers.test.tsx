import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import type { ModelEntry, TierView } from "@/api/harness-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { ModelTiers } from "./model-tiers";

afterEach(cleanup);

const BASE = "http://studio.test";

const GLM: ModelEntry = {
  id: "openrouter/glm-5.3-flash",
  name: "GLM 5.3 Flash",
  route: "openrouter",
  context_length: 1_048_576,
  price_in_per_m: "0.075",
  price_out_per_m: "0.3",
  reasoning: "effort",
  pinned: true,
};

const DEEPSEEK: ModelEntry = {
  id: "gateway/deepseek-v4",
  name: "DeepSeek V4",
  route: "gateway",
  context_length: 131_072,
  price_in_per_m: null,
  price_out_per_m: null,
  reasoning: "toggle",
  pinned: false,
};

function defaultTiers(): TierView[] {
  return [
    {
      tier: "orchestrator",
      role: "design judgment, multi-file changes, hard debugging",
      model: GLM,
      thinking: "high",
      default: true,
    },
    {
      tier: "worker",
      role: "ordinary implementation, review with a clear brief",
      model: GLM,
      thinking: "medium",
      default: true,
    },
    {
      tier: "helper",
      role: "mechanical edits, lookups, single-file work",
      model: DEEPSEEK,
      thinking: "low",
      default: false,
    },
  ];
}

// Wires GET /api/harness/tiers off a mutable array, GET /api/harness/models over a
// small catalogue, and captures every PUT/DELETE so a test can assert on it, while
// keeping the store in sync so a refetch after a mutation reflects it (the same
// contract the real backend gives: PUT/DELETE answer the full listing).
function installHandlers(tiers: TierView[]) {
  const putCalls: { tier: string; body: unknown }[] = [];
  const deleteCalls: string[] = [];
  server.use(
    http.get(`${BASE}/api/harness/tiers`, () => HttpResponse.json({ tiers })),
    http.get(`${BASE}/api/harness/models`, () => HttpResponse.json({ models: [GLM, DEEPSEEK] })),
    http.put(`${BASE}/api/harness/tiers/:tier`, async ({ params, request }) => {
      const body = (await request.json()) as { model: string; thinking: TierView["thinking"] };
      putCalls.push({ tier: params.tier as string, body });
      const index = tiers.findIndex((view) => view.tier === params.tier);
      const model = body.model === GLM.id ? GLM : DEEPSEEK;
      tiers[index] = { ...tiers[index], model, thinking: body.thinking, default: false };
      return HttpResponse.json({ tiers });
    }),
    http.delete(`${BASE}/api/harness/tiers/:tier`, ({ params }) => {
      deleteCalls.push(params.tier as string);
      const index = tiers.findIndex((view) => view.tier === params.tier);
      tiers[index] = { ...tiers[index], model: GLM, thinking: "medium", default: true };
      return HttpResponse.json({ tiers });
    }),
  );
  return { putCalls, deleteCalls };
}

describe("<ModelTiers />", () => {
  it("renders one card per tier, in listing order", async () => {
    installHandlers(defaultTiers());

    render(<ModelTiers />);

    await screen.findByRole("heading", { name: "Orchestrator" });
    expect(screen.getByRole("heading", { name: "Worker" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Helper" })).toBeInTheDocument();
  });

  it("picking a model PUTs it to the right tier, with the tier's current thinking", async () => {
    const { putCalls } = installHandlers(defaultTiers());
    const user = userEvent.setup();

    render(<ModelTiers />);

    await screen.findByRole("heading", { name: "Orchestrator" });
    const box = screen.getByRole("combobox", { name: "Orchestrator model" });
    await user.click(box);
    await waitFor(() => expect(screen.getAllByRole("option").length).toBeGreaterThan(0));
    await user.click(screen.getByRole("option", { name: /DeepSeek V4/ }));

    await waitFor(() => expect(putCalls).toHaveLength(1));
    expect(putCalls[0]).toEqual({
      tier: "orchestrator",
      body: { model: DEEPSEEK.id, thinking: "high" },
    });
  });

  it("disables the picker while a PUT is in flight, so a second pick cannot fire another request", async () => {
    const tiers = defaultTiers();
    const putCalls: { tier: string; body: unknown }[] = [];
    let releaseFirstPut: (() => void) | undefined;
    server.use(
      http.get(`${BASE}/api/harness/tiers`, () => HttpResponse.json({ tiers })),
      http.get(`${BASE}/api/harness/models`, () => HttpResponse.json({ models: [GLM, DEEPSEEK] })),
      http.put(`${BASE}/api/harness/tiers/:tier`, async ({ params, request }) => {
        const body = (await request.json()) as { model: string; thinking: TierView["thinking"] };
        putCalls.push({ tier: params.tier as string, body });
        // Holds the response open until the test releases it, so the mutation's
        // isPending window is observable rather than resolved before assertions run.
        await new Promise<void>((resolve) => {
          releaseFirstPut = resolve;
        });
        const index = tiers.findIndex((view) => view.tier === params.tier);
        tiers[index] = {
          ...tiers[index],
          model: DEEPSEEK,
          thinking: body.thinking,
          default: false,
        };
        return HttpResponse.json({ tiers });
      }),
    );
    const user = userEvent.setup();

    render(<ModelTiers />);

    await screen.findByRole("heading", { name: "Orchestrator" });
    const box = screen.getByRole("combobox", { name: "Orchestrator model" });
    await user.click(box);
    await waitFor(() => expect(screen.getAllByRole("option").length).toBeGreaterThan(0));
    await user.click(screen.getByRole("option", { name: /DeepSeek V4/ }));

    await waitFor(() => expect(putCalls).toHaveLength(1));
    await waitFor(() => expect(box).toBeDisabled());
    expect(box).toHaveAttribute("aria-expanded", "false");

    // The input is disabled, so a second interaction cannot open the list or pick again.
    await user.click(box);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(putCalls).toHaveLength(1);

    releaseFirstPut?.();
    await waitFor(() => expect(box).not.toBeDisabled());
    expect(putCalls).toHaveLength(1);
  });

  it("changing thinking PUTs the current model with the new level", async () => {
    const { putCalls } = installHandlers(defaultTiers());
    const user = userEvent.setup();

    render(<ModelTiers />);

    await screen.findByRole("heading", { name: "Helper" });
    const helperCard = screen
      .getByRole("heading", { name: "Helper" })
      .closest("div") as HTMLElement;
    await user.click(within(helperCard).getByRole("radio", { name: "High" }));

    await waitFor(() => expect(putCalls).toHaveLength(1));
    expect(putCalls[0]).toEqual({ tier: "helper", body: { model: DEEPSEEK.id, thinking: "high" } });
  });

  it("reset DELETEs the tier", async () => {
    const { deleteCalls } = installHandlers(defaultTiers());
    const user = userEvent.setup();

    render(<ModelTiers />);

    await screen.findByRole("heading", { name: "Helper" });
    const helperCard = screen
      .getByRole("heading", { name: "Helper" })
      .closest("div") as HTMLElement;
    const resetButton = within(helperCard).getByRole("button", { name: "Reset to default" });
    expect(resetButton).not.toBeDisabled();
    await user.click(resetButton);

    await waitFor(() => expect(deleteCalls).toEqual(["helper"]));
  });

  it("disables reset once a tier is already at its default", async () => {
    installHandlers(defaultTiers());

    render(<ModelTiers />);

    await screen.findByRole("heading", { name: "Orchestrator" });
    const orchestratorCard = screen
      .getByRole("heading", { name: "Orchestrator" })
      .closest("div") as HTMLElement;
    expect(
      within(orchestratorCard).getByRole("button", { name: "Reset to default" }),
    ).toBeDisabled();
  });

  it('shows "In effect on the next turn." after a change', async () => {
    installHandlers(defaultTiers());
    const user = userEvent.setup();

    render(<ModelTiers />);

    await screen.findByRole("heading", { name: "Helper" });
    const helperCard = screen
      .getByRole("heading", { name: "Helper" })
      .closest("div") as HTMLElement;
    await user.click(within(helperCard).getByRole("radio", { name: "High" }));

    await waitFor(() =>
      expect(within(helperCard).getByText("In effect on the next turn.")).toBeInTheDocument(),
    );
  });

  it("shows the backend's refusal in the danger banner", async () => {
    const tiers = defaultTiers();
    server.use(
      http.get(`${BASE}/api/harness/tiers`, () => HttpResponse.json({ tiers })),
      http.get(`${BASE}/api/harness/models`, () => HttpResponse.json({ models: [GLM, DEEPSEEK] })),
      http.put(`${BASE}/api/harness/tiers/:tier`, () =>
        HttpResponse.json({ detail: "Unknown model 'ghost-model'." }, { status: 400 }),
      ),
    );
    const user = userEvent.setup();

    render(<ModelTiers />);

    await screen.findByRole("heading", { name: "Orchestrator" });
    const box = screen.getByRole("combobox", { name: "Orchestrator model" });
    await user.click(box);
    await waitFor(() => expect(screen.getAllByRole("option").length).toBeGreaterThan(0));
    await user.click(screen.getByRole("option", { name: /DeepSeek V4/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Unknown model 'ghost-model'.");
  });
});
