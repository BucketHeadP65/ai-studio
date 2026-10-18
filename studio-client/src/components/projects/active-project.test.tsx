import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ActiveProjectProvider, useActiveProject } from "./active-project";

afterEach(cleanup);

const STORAGE_KEY = "engram.active-project-id";

function Probe() {
  const { activeProjectId, setActiveProjectId } = useActiveProject();
  return (
    <div>
      <span data-testid="active">{activeProjectId ?? "none"}</span>
      <button type="button" onClick={() => setActiveProjectId("proj-1")}>
        choose proj-1
      </button>
      <button type="button" onClick={() => setActiveProjectId(null)}>
        clear
      </button>
    </div>
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

describe("ActiveProjectProvider / useActiveProject", () => {
  it("renders correctly with no stored value", async () => {
    render(
      <ActiveProjectProvider>
        <Probe />
      </ActiveProjectProvider>,
    );

    expect(await screen.findByTestId("active")).toHaveTextContent("none");
  });

  it("reads the persisted id on mount", async () => {
    window.localStorage.setItem(STORAGE_KEY, "proj-1");

    render(
      <ActiveProjectProvider>
        <Probe />
      </ActiveProjectProvider>,
    );

    expect(await screen.findByTestId("active")).toHaveTextContent("proj-1");
  });

  it("setting the active project persists it and updates every consumer", async () => {
    const user = userEvent.setup();
    render(
      <ActiveProjectProvider>
        <Probe />
      </ActiveProjectProvider>,
    );

    await user.click(screen.getByRole("button", { name: "choose proj-1" }));

    expect(screen.getByTestId("active")).toHaveTextContent("proj-1");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("proj-1");
  });

  it("clearing the active project removes the stored value", async () => {
    window.localStorage.setItem(STORAGE_KEY, "proj-1");
    const user = userEvent.setup();
    render(
      <ActiveProjectProvider>
        <Probe />
      </ActiveProjectProvider>,
    );
    expect(await screen.findByTestId("active")).toHaveTextContent("proj-1");

    await user.click(screen.getByRole("button", { name: "clear" }));

    expect(screen.getByTestId("active")).toHaveTextContent("none");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("degrades to in-memory state when localStorage throws", async () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const user = userEvent.setup();

    render(
      <ActiveProjectProvider>
        <Probe />
      </ActiveProjectProvider>,
    );
    expect(await screen.findByTestId("active")).toHaveTextContent("none");

    await user.click(screen.getByRole("button", { name: "choose proj-1" }));
    expect(screen.getByTestId("active")).toHaveTextContent("proj-1");

    getItem.mockRestore();
    setItem.mockRestore();
  });

  it("throws when useActiveProject is called outside the provider", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});

    expect(() => render(<Probe />)).toThrow(
      "useActiveProject must be used within an ActiveProjectProvider",
    );

    consoleError.mockRestore();
  });
});
