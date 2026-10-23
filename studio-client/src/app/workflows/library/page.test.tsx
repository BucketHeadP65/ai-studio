import { screen } from "@testing-library/dom";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { LibraryEntry } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

// Imported after the mock so the library sees the mocked module.
import WorkflowLibraryPage from "./page";

// The page is a thin mount of `WorkflowLibrary`; this confirms the wiring reaches a real
// screen -- the card grid's own behavior is covered in `workflow-library.test.tsx`.

const BASE = "http://studio.test";

const CARD: LibraryEntry = {
  id: "wf.sales.v1",
  title: "Fetch sales",
  summary: "Pull last quarter's sales into one report.",
  provisional: false,
  score: 0.9,
  trust: {
    level: "machine-confirmed",
    runs_with_evidence: 3,
    failed_runs: 0,
    approved_by: [],
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
  },
  needs: [],
  missing: [],
  brings: [],
  version: 1,
  archived_at: null,
  shape: { steps: 4, person_gates: 1, agent_gates: 1 },
  runs: { last: null },
};

describe("WorkflowLibraryPage", () => {
  it("renders the library heading, the new-workflow action, and a saved card", async () => {
    server.use(
      http.get(`${BASE}/api/library`, () => HttpResponse.json([CARD])),
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
    );
    render(<WorkflowLibraryPage />);

    expect(await screen.findByRole("heading", { level: 1, name: "Library" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Fetch sales" })).toHaveAttribute(
      "href",
      "/workflows/library/wf.sales.v1",
    );
    expect(screen.getByRole("link", { name: "New workflow" })).toHaveAttribute("href", "/chat");
  });
});
