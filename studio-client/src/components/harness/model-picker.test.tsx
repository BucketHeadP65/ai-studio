import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ModelEntry } from "@/api/harness-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { ModelPicker } from "./model-picker";

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

const CATALOGUE = [GLM, DEEPSEEK];

function installModelsHandler() {
  server.use(
    http.get(`${BASE}/api/harness/models`, ({ request }) => {
      const q = new URL(request.url).searchParams.get("q") ?? "";
      const matches =
        q === ""
          ? CATALOGUE
          : CATALOGUE.filter((entry) => entry.name.toLowerCase().includes(q.toLowerCase()));
      return HttpResponse.json({ models: matches });
    }),
  );
}

describe("<ModelPicker />", () => {
  it("narrows the list as the query narrows", async () => {
    installModelsHandler();
    const user = userEvent.setup();

    render(<ModelPicker value={GLM} onPick={vi.fn()} label="Worker model" />);

    const box = screen.getByRole("combobox", { name: "Worker model" });
    await user.type(box, "DeepSeek");

    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(1));
    expect(screen.getByRole("option", { name: /DeepSeek V4/ })).toBeInTheDocument();
  });

  it("walks the options with the arrow keys and picks with Enter", async () => {
    installModelsHandler();
    const onPick = vi.fn();
    const user = userEvent.setup();

    render(<ModelPicker value={GLM} onPick={onPick} label="Worker model" />);

    const box = screen.getByRole("combobox", { name: "Worker model" });
    await user.click(box);
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(2));

    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");

    expect(onPick).toHaveBeenCalledWith(DEEPSEEK);
  });

  it("closes on Escape without picking", async () => {
    installModelsHandler();
    const onPick = vi.fn();
    const user = userEvent.setup();

    render(<ModelPicker value={GLM} onPick={onPick} label="Worker model" />);

    const box = screen.getByRole("combobox", { name: "Worker model" });
    await user.click(box);
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(2));

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(onPick).not.toHaveBeenCalled();
  });

  it("carries each option's id, for aria-activedescendant to point at", async () => {
    installModelsHandler();
    const user = userEvent.setup();

    render(<ModelPicker value={GLM} onPick={vi.fn()} label="Worker model" />);

    const box = screen.getByRole("combobox", { name: "Worker model" });
    await user.click(box);

    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(2));
    expect(document.getElementById(`model-option-${GLM.id}`)).toBeInTheDocument();
    expect(document.getElementById(`model-option-${DEEPSEEK.id}`)).toBeInTheDocument();

    await user.keyboard("{ArrowDown}");
    expect(box).toHaveAttribute("aria-activedescendant", `model-option-${GLM.id}`);
  });
});
