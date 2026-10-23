import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import { ApiError } from "@/shared/api/client";
import { server } from "@/shared/test-support/msw/server";

import { projectKeys, projectsApi } from "./projects";
import { studioErrorMessage } from "./studio";

// The project workspace client rides the same agent backend as harness/studio
// (:8770), faked here as http://studio.test (vitest.config env). Every method
// pins one endpoint's zod boundary against a payload mirroring the PROPOSED
// backend contract, so a shape drift (once the backend lands) fails HERE,
// named, rather than as an undefined deep in a view.

const BASE = "http://studio.test";

const PROJECT = { id: "proj-1", name: "engram", root_path: "/home/user/engram" };

const NOTE = {
  id: "note-1",
  anchor: { exact: "the anchored span", prefix: "before it, ", suffix: ", after it" },
  text: "worth a second look",
  created_at: "2026-08-11T09:00:00+00:00",
  archived: false,
  replies: [],
  status: "open",
};

describe("projectsApi", () => {
  it("parses the project listing, missing and running_sessions included", async () => {
    server.use(
      http.get(`${BASE}/api/projects`, () =>
        HttpResponse.json({
          projects: [
            { ...PROJECT, missing: false, running_sessions: 2 },
            {
              id: "proj-2",
              name: "gone",
              root_path: "/tmp/gone",
              missing: true,
              running_sessions: 0,
            },
          ],
        }),
      ),
    );

    const listing = await projectsApi.list();

    expect(listing).toHaveLength(2);
    expect(listing[0]).toMatchObject({ id: "proj-1", missing: false, running_sessions: 2 });
    expect(listing[1]).toMatchObject({ id: "proj-2", missing: true, running_sessions: 0 });
  });

  it("registers a project by root path", async () => {
    server.use(
      http.post(`${BASE}/api/projects`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ root_path: "/home/user/engram" });
        return HttpResponse.json({ project: PROJECT });
      }),
    );

    const project = await projectsApi.register("/home/user/engram");

    expect(project).toEqual(PROJECT);
  });

  it("surfaces a 400 (not a directory) through studioErrorMessage", async () => {
    server.use(
      http.post(`${BASE}/api/projects`, () =>
        HttpResponse.json({ detail: "'/nope' is not a directory." }, { status: 400 }),
      ),
    );

    const failure = await projectsApi.register("/nope").catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(studioErrorMessage(failure)).toBe("'/nope' is not a directory.");
  });

  it("renames a project", async () => {
    server.use(
      http.patch(`${BASE}/api/projects/proj-1`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ name: "engram-renamed" });
        return HttpResponse.json({ project: { ...PROJECT, name: "engram-renamed" } });
      }),
    );

    const project = await projectsApi.rename("proj-1", "engram-renamed");

    expect(project.name).toBe("engram-renamed");
  });

  it("unregisters a project", async () => {
    server.use(
      http.delete(`${BASE}/api/projects/proj-1`, () => new HttpResponse(null, { status: 204 })),
    );

    await expect(projectsApi.unregister("proj-1")).resolves.toBeUndefined();
  });

  it("surfaces a 409 (a session is running) through studioErrorMessage", async () => {
    server.use(
      http.delete(`${BASE}/api/projects/proj-1`, () =>
        HttpResponse.json({ detail: "A session is running in this project." }, { status: 409 }),
      ),
    );

    const failure = await projectsApi.unregister("proj-1").catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(studioErrorMessage(failure)).toBe("A session is running in this project.");
  });

  it("parses a file tree listing", async () => {
    server.use(
      http.get(`${BASE}/api/projects/proj-1/files`, () =>
        HttpResponse.json({
          entries: [
            { path: "notes", kind: "dir", size: 0 },
            { path: "notes/today.md", kind: "file", size: 128 },
          ],
        }),
      ),
    );

    const entries = await projectsApi.files("proj-1");

    expect(entries).toEqual([
      { path: "notes", kind: "dir", size: 0 },
      { path: "notes/today.md", kind: "file", size: 128 },
    ]);
  });

  it("reads a file by path, query-encoded", async () => {
    server.use(
      http.get(`${BASE}/api/projects/proj-1/file`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("path")).toBe("notes/today.md");
        return HttpResponse.json({ content: "# Today", hash: "abc123def456", obsidian: true });
      }),
    );

    const file = await projectsApi.readFile("proj-1", "notes/today.md");

    expect(file).toEqual({ content: "# Today", hash: "abc123def456", obsidian: true });
  });

  it("writes a file with an expected hash and returns the new hash", async () => {
    server.use(
      http.put(`${BASE}/api/projects/proj-1/file`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({
          path: "notes/today.md",
          content: "# Today\n\nmore",
          expected_hash: "abc123def456",
        });
        return HttpResponse.json({ hash: "def456abc123" });
      }),
    );

    const hash = await projectsApi.writeFile(
      "proj-1",
      "notes/today.md",
      "# Today\n\nmore",
      "abc123def456",
    );

    expect(hash).toBe("def456abc123");
  });

  it("surfaces a 409 (stale hash) through studioErrorMessage", async () => {
    server.use(
      http.put(`${BASE}/api/projects/proj-1/file`, () =>
        HttpResponse.json({ detail: "The file changed on disk." }, { status: 409 }),
      ),
    );

    const failure = await projectsApi
      .writeFile("proj-1", "notes/today.md", "stale", "abc123def456")
      .catch((error: unknown) => error);

    expect(studioErrorMessage(failure)).toBe("The file changed on disk.");
  });

  it("creates a file through the create endpoint, never the write endpoint", async () => {
    let writeCalled = false;
    server.use(
      http.put(`${BASE}/api/projects/proj-1/file`, () => {
        writeCalled = true;
        return HttpResponse.json({ hash: "wrong" });
      }),
      http.post(`${BASE}/api/projects/proj-1/file/create`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ path: "notes/new.md" });
        return HttpResponse.json({ hash: "abc123def456" });
      }),
    );

    const hash = await projectsApi.createFile("proj-1", "notes/new.md");

    expect(hash).toBe("abc123def456");
    expect(writeCalled).toBe(false);
  });

  it("surfaces a 409 (the file already exists) through studioErrorMessage", async () => {
    server.use(
      http.post(`${BASE}/api/projects/proj-1/file/create`, () =>
        HttpResponse.json({ detail: "'notes/new.md' already exists." }, { status: 409 }),
      ),
    );

    const failure = await projectsApi
      .createFile("proj-1", "notes/new.md")
      .catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(studioErrorMessage(failure)).toBe("'notes/new.md' already exists.");
  });

  it("renames a file", async () => {
    server.use(
      http.post(`${BASE}/api/projects/proj-1/file/rename`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ path: "notes/today.md", to: "notes/yesterday.md" });
        return new HttpResponse(null, { status: 204 });
      }),
    );

    await expect(
      projectsApi.renameFile("proj-1", "notes/today.md", "notes/yesterday.md"),
    ).resolves.toBeUndefined();
  });

  it("deletes a file by path, query-encoded", async () => {
    server.use(
      http.delete(`${BASE}/api/projects/proj-1/file`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("path")).toBe("notes/today.md");
        return new HttpResponse(null, { status: 204 });
      }),
    );

    await expect(projectsApi.deleteFile("proj-1", "notes/today.md")).resolves.toBeUndefined();
  });

  it("parses the notes listing, path/orphaned/line/start/end included", async () => {
    server.use(
      http.get(`${BASE}/api/projects/proj-1/notes`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("path")).toBe("notes/today.md");
        return HttpResponse.json({
          notes: [
            { ...NOTE, path: "notes/today.md", orphaned: false, line: 3, start: 40, end: 58 },
          ],
        });
      }),
    );

    const notes = await projectsApi.notes("proj-1", "notes/today.md");

    expect(notes).toEqual([
      { ...NOTE, path: "notes/today.md", orphaned: false, line: 3, start: 40, end: 58 },
    ]);
  });

  it("parses an orphaned note, whose line and offsets are null", async () => {
    server.use(
      http.get(`${BASE}/api/projects/proj-1/notes`, () =>
        HttpResponse.json({
          notes: [
            { ...NOTE, path: "notes/today.md", orphaned: true, line: null, start: null, end: null },
          ],
        }),
      ),
    );

    const notes = await projectsApi.notes("proj-1", "notes/today.md");

    expect(notes).toEqual([
      { ...NOTE, path: "notes/today.md", orphaned: true, line: null, start: null, end: null },
    ]);
  });

  it("lists every note across the project when no path is given", async () => {
    server.use(
      http.get(`${BASE}/api/projects/proj-1/notes`, ({ request }) => {
        expect(new URL(request.url).search).toBe("");
        return HttpResponse.json({ notes: [] });
      }),
    );

    await expect(projectsApi.notes("proj-1")).resolves.toEqual([]);
  });

  it("creates a note", async () => {
    server.use(
      http.post(`${BASE}/api/projects/proj-1/notes`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({
          path: "notes/today.md",
          anchor: NOTE.anchor,
          text: NOTE.text,
        });
        return HttpResponse.json({ note: NOTE });
      }),
    );

    const note = await projectsApi.createNote("proj-1", "notes/today.md", NOTE.anchor, NOTE.text);

    expect(note).toEqual(NOTE);
  });

  it("updates a note", async () => {
    server.use(
      http.patch(`${BASE}/api/projects/proj-1/notes/note-1`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ path: "notes/today.md", archived: true });
        return HttpResponse.json({ note: { ...NOTE, archived: true } });
      }),
    );

    const note = await projectsApi.updateNote("proj-1", "note-1", {
      path: "notes/today.md",
      archived: true,
    });

    expect(note.archived).toBe(true);
  });

  it("updates a reply, PATCHing the reply route with its index and new text", async () => {
    server.use(
      http.patch(`${BASE}/api/projects/proj-1/notes/note-1/reply`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ path: "notes/today.md", reply_index: 0, text: "revised" });
        return HttpResponse.json({
          note: { ...NOTE, replies: [{ author: "owner", text: "revised", at: NOTE.created_at }] },
        });
      }),
    );

    const note = await projectsApi.updateReply("proj-1", "note-1", {
      path: "notes/today.md",
      replyIndex: 0,
      text: "revised",
    });

    expect(note.replies).toEqual([{ author: "owner", text: "revised", at: NOTE.created_at }]);
  });

  it("deletes a note, path query-encoded", async () => {
    server.use(
      http.delete(`${BASE}/api/projects/proj-1/notes/note-1`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("path")).toBe("notes/today.md");
        return new HttpResponse(null, { status: 204 });
      }),
    );

    await expect(
      projectsApi.deleteNote("proj-1", "note-1", "notes/today.md"),
    ).resolves.toBeUndefined();
  });

  it("parses a note carrying replies from both authors", async () => {
    server.use(
      http.get(`${BASE}/api/projects/proj-1/notes`, () =>
        HttpResponse.json({
          notes: [
            {
              ...NOTE,
              path: "notes/today.md",
              orphaned: false,
              line: 3,
              start: 40,
              end: 58,
              status: "resolved",
              replies: [
                { author: "owner", text: "any update?", at: "2026-08-11T09:05:00+00:00" },
                { author: "agent", text: "fixed it", at: "2026-08-11T09:06:00+00:00" },
              ],
            },
          ],
        }),
      ),
    );

    const [note] = await projectsApi.notes("proj-1", "notes/today.md");

    expect(note.status).toBe("resolved");
    expect(note.replies).toEqual([
      { author: "owner", text: "any update?", at: "2026-08-11T09:05:00+00:00" },
      { author: "agent", text: "fixed it", at: "2026-08-11T09:06:00+00:00" },
    ]);
  });

  it("replies to a note", async () => {
    server.use(
      http.post(`${BASE}/api/projects/proj-1/notes/note-1/reply`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ path: "notes/today.md", text: "on it" });
        return HttpResponse.json({
          note: {
            ...NOTE,
            replies: [{ author: "owner", text: "on it", at: "2026-08-11T09:05:00+00:00" }],
          },
        });
      }),
    );

    const note = await projectsApi.replyToNote("proj-1", "note-1", "notes/today.md", "on it");

    expect(note.replies).toEqual([
      { author: "owner", text: "on it", at: "2026-08-11T09:05:00+00:00" },
    ]);
  });

  it("resolves a note, path query-encoded", async () => {
    server.use(
      http.post(`${BASE}/api/projects/proj-1/notes/note-1/resolve`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("path")).toBe("notes/today.md");
        return HttpResponse.json({ note: { ...NOTE, status: "resolved" } });
      }),
    );

    const note = await projectsApi.resolveNote("proj-1", "note-1", "notes/today.md");

    expect(note.status).toBe("resolved");
  });

  it("reopens a note, path query-encoded", async () => {
    server.use(
      http.post(`${BASE}/api/projects/proj-1/notes/note-1/reopen`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("path")).toBe("notes/today.md");
        return HttpResponse.json({ note: { ...NOTE, status: "open" } });
      }),
    );

    const note = await projectsApi.reopenNote("proj-1", "note-1", "notes/today.md");

    expect(note.status).toBe("open");
  });

  it("parses the skills listing, including a broken card", async () => {
    server.use(
      http.get(`${BASE}/api/projects/proj-1/skills`, () =>
        HttpResponse.json({
          skills: [
            {
              name: "recap",
              description: "Summarize the day.",
              library: "personal",
              path: "skills/recap.md",
              valid: true,
              error: null,
            },
            {
              name: "broken",
              description: "",
              library: "personal",
              path: "skills/broken.md",
              valid: false,
              error: "missing frontmatter",
            },
          ],
        }),
      ),
    );

    const skills = await projectsApi.skills("proj-1");

    expect(skills).toHaveLength(2);
    expect(skills[1]).toMatchObject({ valid: false, error: "missing frontmatter" });
  });

  it("installs a skill and returns its path", async () => {
    server.use(
      http.post(`${BASE}/api/projects/proj-1/skills`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ library: "personal", name: "recap" });
        return HttpResponse.json({ path: "skills/recap.md" });
      }),
    );

    const path = await projectsApi.installSkill("proj-1", "personal", "recap");

    expect(path).toBe("skills/recap.md");
  });

  it("reads a skill's full body by library and name, query-encoded", async () => {
    server.use(
      http.get(`${BASE}/api/projects/proj-1/skills/body`, ({ request }) => {
        const params = new URL(request.url).searchParams;
        expect(params.get("library")).toBe("builtin");
        expect(params.get("name")).toBe("core search");
        return HttpResponse.json({
          content: "---\ndescription: x\n---\n\nBody.\n",
          hash: "abc123",
          editable: false,
        });
      }),
    );

    const body = await projectsApi.skillBody("proj-1", "builtin", "core search");

    expect(body).toEqual({
      content: "---\ndescription: x\n---\n\nBody.\n",
      hash: "abc123",
      editable: false,
    });
  });

  it("writes a skill's body and returns the new hash", async () => {
    server.use(
      http.put(`${BASE}/api/projects/proj-1/skills/body`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({
          library: "personal",
          name: "recap",
          content: "new body",
          expected_hash: "abc123",
        });
        return HttpResponse.json({ hash: "def456" });
      }),
    );

    const hash = await projectsApi.writeSkillBody("proj-1", {
      library: "personal",
      name: "recap",
      content: "new body",
      expectedHash: "abc123",
    });

    expect(hash).toBe("def456");
  });

  it("surfaces a 403 (builtin is read-only) through studioErrorMessage", async () => {
    server.use(
      http.put(`${BASE}/api/projects/proj-1/skills/body`, () =>
        HttpResponse.json({ detail: "builtin skills are read-only" }, { status: 403 }),
      ),
    );

    const failure = await projectsApi
      .writeSkillBody("proj-1", { library: "builtin", name: "core-search", content: "x" })
      .catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(studioErrorMessage(failure)).toBe("builtin skills are read-only");
  });

  it("parses the per-file notes summary, a file with no agent reply included", async () => {
    server.use(
      http.get(`${BASE}/api/projects/proj-1/notes/summary`, () =>
        HttpResponse.json({
          files: [
            {
              path: "a.md",
              open_notes: 1,
              resolved_notes: 0,
              latest_agent_reply_at: "2026-08-11T09:06:00+00:00",
            },
            { path: "b.md", open_notes: 0, resolved_notes: 2, latest_agent_reply_at: null },
          ],
        }),
      ),
    );

    const summary = await projectsApi.notesSummary("proj-1");

    expect(summary).toEqual([
      {
        path: "a.md",
        open_notes: 1,
        resolved_notes: 0,
        latest_agent_reply_at: "2026-08-11T09:06:00+00:00",
      },
      { path: "b.md", open_notes: 0, resolved_notes: 2, latest_agent_reply_at: null },
    ]);
  });

  it("creates a directory", async () => {
    server.use(
      http.post(`${BASE}/api/projects/proj-1/dir/create`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ path: "notes/archive" });
        return new HttpResponse(null, { status: 204 });
      }),
    );

    await expect(projectsApi.createDir("proj-1", "notes/archive")).resolves.toBeUndefined();
  });

  it("rejects a drifted listing at the boundary", async () => {
    server.use(
      http.get(`${BASE}/api/projects`, () => HttpResponse.json({ projects: [{ id: "proj-1" }] })),
    );

    await expect(projectsApi.list()).rejects.toBeInstanceOf(ZodError);
  });
});

describe("projectKeys", () => {
  it("nests every per-project key under the listing key", () => {
    expect(projectKeys.all).toEqual(["projects"]);
    expect(projectKeys.files("proj-1")).toEqual(["projects", "proj-1", "files"]);
    expect(projectKeys.file("proj-1", "a.md")).toEqual(["projects", "proj-1", "file", "a.md"]);
    expect(projectKeys.skills("proj-1")).toEqual(["projects", "proj-1", "skills"]);
  });

  it("makes the project-wide notes key a prefix of every per-file notes key", () => {
    const projectWide = projectKeys.notes("proj-1");
    const perFile = projectKeys.notes("proj-1", "a.md");

    expect(projectWide).toEqual(["projects", "proj-1", "notes"]);
    expect(perFile).toEqual(["projects", "proj-1", "notes", "a.md"]);
    expect(perFile.slice(0, projectWide.length)).toEqual([...projectWide]);
  });

  it("nests the summary key under the project-wide notes key too", () => {
    const projectWide = projectKeys.notes("proj-1");
    const summary = projectKeys.notesSummary("proj-1");

    expect(summary).toEqual(["projects", "proj-1", "notes", "summary"]);
    expect(summary.slice(0, projectWide.length)).toEqual([...projectWide]);
  });

  it("keys a skill body by project, library, and name", () => {
    expect(projectKeys.skillBody("proj-1", "builtin", "core-search")).toEqual([
      "projects",
      "proj-1",
      "skills",
      "body",
      "builtin",
      "core-search",
    ]);
  });
});
