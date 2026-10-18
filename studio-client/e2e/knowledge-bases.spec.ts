import { type Page, type Route, expect, test } from "@playwright/test";

// The Knowledge-bases station's happy path: create a knowledge base, watch it appear in the list.
// Same convention as e2e/smoke.spec.ts — every endpoint the page fetches is
// page.route-mocked, hermetic, no backend involved. `POST /api/knowledge-bases` mutates the
// mock's own state so the follow-up `GET /api/knowledge-bases` (React Query's
// invalidate-and-refetch) answers with the knowledge base the form just created — proving the
// round trip, not just the click.

interface CreatedKnowledgeBase {
  name: string;
  path: string;
}

function json(page: Route, body: unknown) {
  return page.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockKnowledgeBasesApi(page: Page) {
  let created: CreatedKnowledgeBase | null = null;

  // The dashboard opens one EventSource per backend on mount. Answered with a stream that says
  // nothing and asks for a long retry, so no spec spends its budget on reconnects.
  await page.route("**/api/events**", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "retry: 100000\n\n" }),
  );
  await page.route("**/api/knowledge-bases", async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() as CreatedKnowledgeBase;
      created = { name: body.name, path: body.path };
      await json(route, {
        written: ["knowledge-base-schema.json"],
        skipped: [],
        merged: [],
        forced: [],
      });
      return;
    }
    const knowledgeBases = created
      ? [
          {
            name: created.name,
            registered: true,
            indexed: false,
            broken: false,
            path: created.path,
            description: "",
            pages: null,
            page_types: [],
            last_indexed: null,
          },
        ]
      : [];
    await json(route, { knowledge_bases: knowledgeBases });
  });
}

test("creating a knowledge base from the minimal template lands it in the list", async ({ page }) => {
  await mockKnowledgeBasesApi(page);

  await page.goto("/knowledge-bases");
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge bases" })).toBeVisible();
  await expect(page.getByText("Nothing here yet.")).toBeVisible();

  await page.getByRole("button", { name: "New base" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();

  await dialog.getByLabel("Name").fill("scratch");
  await dialog.getByLabel("Path").fill("/tmp/scratch");
  await dialog.getByRole("radio", { name: "Minimal" }).check();
  await dialog.getByRole("button", { name: "Create" }).click();

  await expect(dialog).toBeHidden();
  await expect(page.getByText("scratch", { exact: true })).toBeVisible();
  await expect(page.getByText("Not indexed yet.")).toBeVisible();
});
