import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { KnowledgeBaseListEntry } from "@/api/types";
import { render } from "@/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so the list sees the mocked module.
import { KnowledgeBasesList } from "./list";

afterEach(() => {
  routerPush.mockClear();
  cleanup();
});

function entry(overrides: Partial<KnowledgeBaseListEntry> = {}): KnowledgeBaseListEntry {
  return {
    name: "research",
    registered: true,
    indexed: true,
    broken: false,
    path: "/knowledge-bases/research",
    description: "",
    pages: 12,
    page_types: ["paper"],
    last_indexed: "2026-08-11T09:00:00+00:00",
    ...overrides,
  };
}

describe("KnowledgeBasesList", () => {
  it("shows a loading state before the fetch resolves", () => {
    render(
      <KnowledgeBasesList entries={[]} loading onViewSchema={vi.fn()} onOpenTerminal={vi.fn()} />,
    );

    expect(screen.getByText("Loading knowledge bases...")).toBeInTheDocument();
  });

  it("shows an empty state once loaded with nothing registered or indexed", () => {
    render(
      <KnowledgeBasesList
        entries={[]}
        loading={false}
        onViewSchema={vi.fn()}
        onOpenTerminal={vi.fn()}
      />,
    );

    expect(screen.getByText("Nothing here yet.")).toBeInTheDocument();
  });

  it("lists entries alphabetically with their documents and page type counts", () => {
    render(
      <KnowledgeBasesList
        entries={[
          entry({ name: "zeta", pages: 40, page_types: ["paper", "note"] }),
          entry({
            name: "alpha",
            registered: true,
            indexed: false,
            pages: null,
            page_types: [],
            last_indexed: null,
          }),
          entry({ name: "beta", registered: false, indexed: true, path: null }),
        ]}
        loading={false}
        onViewSchema={vi.fn()}
        onOpenTerminal={vi.fn()}
      />,
    );

    const rows = screen.getAllByRole("listitem");
    const names = rows.map((item) => item.textContent);
    expect(names[0]).toContain("alpha");
    expect(names[1]).toContain("beta");
    expect(names[2]).toContain("zeta");

    // zeta's own vitals: a page count and a page-type count, both in the row's own cells.
    expect(within(rows[2]).getByText("40")).toBeInTheDocument();
    expect(within(rows[2]).getByText("2")).toBeInTheDocument();
    // alpha has never indexed, its page count cell reads empty rather than "null".
    expect(within(rows[0]).queryByText("null")).not.toBeInTheDocument();
  });

  it("names every index state with an icon plus its own sentence", () => {
    render(
      <KnowledgeBasesList
        entries={[
          entry({
            name: "indexed-base",
            indexed: true,
            last_indexed: "2026-08-11T09:00:00+00:00",
          }),
          entry({
            name: "pending-base",
            registered: true,
            indexed: false,
            pages: null,
            page_types: [],
            last_indexed: null,
          }),
          entry({ name: "unregistered-base", registered: false, indexed: false, path: null }),
          entry({
            name: "broken-base",
            broken: true,
            indexed: false,
            path: "/knowledge-bases/gone",
          }),
        ]}
        loading={false}
        onViewSchema={vi.fn()}
        onOpenTerminal={vi.fn()}
      />,
    );

    expect(screen.getByText(/^Indexed .+\.$/)).toBeInTheDocument();
    expect(screen.getByText("Not indexed yet.")).toBeInTheDocument();
    expect(screen.getByText("Not registered.")).toBeInTheDocument();
    expect(screen.getByText("Broken.")).toBeInTheDocument();
  });

  it("surfaces a broken row (name plus raw path) instead of hiding it, with no actions", () => {
    render(
      <KnowledgeBasesList
        entries={[
          entry({ name: "moved-away", broken: true, indexed: false, path: "/knowledge-bases/gone" }),
        ]}
        loading={false}
        onViewSchema={vi.fn()}
        onOpenTerminal={vi.fn()}
      />,
    );

    expect(screen.getByText("moved-away")).toBeInTheDocument();
    expect(screen.getByText("/knowledge-bases/gone")).toBeInTheDocument();
    expect(screen.getByText("Broken.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View schema" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Terminal" })).not.toBeInTheDocument();
  });

  it("offers 'View schema' only for a registered knowledge base, wired to the name", async () => {
    const onViewSchema = vi.fn();
    render(
      <KnowledgeBasesList
        entries={[
          entry({ name: "research", registered: true }),
          entry({ name: "orphan", registered: false, path: null }),
        ]}
        loading={false}
        onViewSchema={onViewSchema}
        onOpenTerminal={vi.fn()}
      />,
    );

    expect(screen.getAllByRole("button", { name: "View schema" })).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "View schema" }));
    expect(onViewSchema).toHaveBeenCalledWith("research");
  });

  it("offers 'Terminal' only for a registered knowledge base, wired to its path", async () => {
    const onOpenTerminal = vi.fn();
    render(
      <KnowledgeBasesList
        entries={[
          entry({ name: "research", registered: true, path: "/knowledge-bases/research" }),
          entry({ name: "orphan", registered: false, path: null }),
          entry({ name: "moved-away", broken: true, path: "/knowledge-bases/gone" }),
        ]}
        loading={false}
        onViewSchema={vi.fn()}
        onOpenTerminal={onOpenTerminal}
      />,
    );

    expect(screen.getAllByRole("button", { name: "Terminal" })).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Terminal" }));
    expect(onOpenTerminal).toHaveBeenCalledWith("/knowledge-bases/research");
  });

  it("opens the base's own page on a whole row click, not just its name", async () => {
    render(
      <KnowledgeBasesList
        entries={[entry({ name: "research" })]}
        loading={false}
        onViewSchema={vi.fn()}
        onOpenTerminal={vi.fn()}
      />,
    );

    const row = screen.getByRole("listitem");
    // A click on the row's own documents cell, not the name link or an action button, still
    // navigates, proving the whole row is the click target.
    await userEvent.click(within(row).getByText("12"));
    expect(routerPush).toHaveBeenCalledWith("/knowledge-bases/research");
  });

  it("does not navigate when a row's own action button is clicked", async () => {
    const onViewSchema = vi.fn();
    render(
      <KnowledgeBasesList
        entries={[entry({ name: "research" })]}
        loading={false}
        onViewSchema={onViewSchema}
        onOpenTerminal={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "View schema" }));
    expect(onViewSchema).toHaveBeenCalledTimes(1);
    expect(routerPush).not.toHaveBeenCalled();
  });
});
