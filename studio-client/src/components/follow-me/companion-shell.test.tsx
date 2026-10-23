import { screen } from "@testing-library/dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { render } from "@/shared/test-support/render";

import { CompanionShell } from "./companion-shell";
import type { Placement } from "./placement";
import { FollowMeProvider } from "./provider";

// Which slot of the shell the companion lands in, and the one station that never gets one.
// Unpinned throughout: the collapsed control is enough to place it, and it fires no requests.

let pathnameValue = "/projects";
let searchParamsValue = new URLSearchParams();

vi.mock("next/navigation", () => ({
  usePathname: () => pathnameValue,
  useSearchParams: () => searchParamsValue,
  useRouter: () => ({ push: vi.fn() }),
}));

function renderShell(placement?: Placement) {
  if (placement !== undefined) {
    window.localStorage.setItem("studio:followme:placement", placement);
  }
  return render(
    <FollowMeProvider>
      <CompanionShell>
        <p>page content</p>
      </CompanionShell>
    </FollowMeProvider>,
  );
}

function companion(): HTMLElement {
  return screen.getByRole("button", { name: "Companion" });
}

function page(): HTMLElement {
  return screen.getByText("page content");
}

function comesBefore(first: HTMLElement, second: HTMLElement): boolean {
  return (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

beforeEach(() => {
  window.localStorage.clear();
  pathnameValue = "/projects";
  searchParamsValue = new URLSearchParams();
});

afterEach(() => {
  window.localStorage.clear();
});

describe("CompanionShell", () => {
  it("keeps the companion off the chat station and its subpaths, page and all", () => {
    pathnameValue = "/chat";
    const { unmount } = renderShell();
    expect(screen.queryByRole("button", { name: "Companion" })).not.toBeInTheDocument();
    expect(page()).toBeInTheDocument();
    unmount();

    pathnameValue = "/chat/s-1";
    renderShell();
    expect(screen.queryByRole("button", { name: "Companion" })).not.toBeInTheDocument();
    expect(page()).toBeInTheDocument();
  });

  it("sits before the page column by default, where the nav leaves off", () => {
    renderShell();
    expect(comesBefore(companion(), page())).toBe(true);
    expect(screen.getByRole("main")).not.toContainElement(companion());
  });

  it("sits after the page column on the right", () => {
    renderShell("right");
    expect(comesBefore(page(), companion())).toBe(true);
    expect(screen.getByRole("main")).not.toContainElement(companion());
  });

  it("sits under the page inside its own column at the bottom", () => {
    renderShell("bottom");
    const main = screen.getByRole("main");
    expect(comesBefore(page(), companion())).toBe(true);
    expect(main.parentElement).toContainElement(companion());
    expect(main).not.toContainElement(companion());
  });

  it("floats over the page column, never over the nav", () => {
    renderShell("floating");
    const main = screen.getByRole("main");
    const column = main.parentElement;
    expect(column).toContainElement(companion());
    expect(column?.className).toContain("relative");
  });
});
