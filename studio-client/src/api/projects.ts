import { ApiError, VoidSchema, createApiClient } from "client-core/api/client";
import { z } from "zod";

import { STUDIO_API_URL } from "./studio-config";

// The project workspace's typed API surface, over the studio agent backend
// (`studio.agent`, :8770) -- the same origin harnessApi and studioApi talk to.
// The backend does not exist yet: these zod schemas and this client are the
// PROPOSED contract, one module ahead of the routes. Every path string lives
// in `endpoints` below, so an adopted contract's path change is a one-file
// edit. Refusals arrive as FastAPI's `{detail}` shape; views unwrap them with
// `studioErrorMessage` from ./studio, which already reads that shape.
//
// Session/chat endpoints (`/api/sessions/{id}/...`) are owned elsewhere and
// are not part of this client.

export { ApiError };

export const ProjectSchema = z.object({
  id: z.string(),
  name: z.string(),
  root_path: z.string(),
});
export type Project = z.infer<typeof ProjectSchema>;

// GET /api/projects adds the two fields the dropdown needs and a plain
// ProjectView never carries: whether the root_path still exists on disk, and
// how many sessions are live in it right now.
export const ProjectListingEntrySchema = ProjectSchema.extend({
  missing: z.boolean(),
  running_sessions: z.number(),
});
export type ProjectListingEntry = z.infer<typeof ProjectListingEntrySchema>;

const ProjectsListingSchema = z.object({ projects: z.array(ProjectListingEntrySchema) });
const ProjectResultSchema = z.object({ project: ProjectSchema });

export const FileEntrySchema = z.object({
  path: z.string(),
  kind: z.enum(["dir", "file"]),
  size: z.number(),
});
export type FileEntry = z.infer<typeof FileEntrySchema>;

const FileListingSchema = z.object({ entries: z.array(FileEntrySchema) });

export const FileContentSchema = z.object({
  content: z.string(),
  hash: z.string(),
  obsidian: z.boolean(),
});
export type FileContent = z.infer<typeof FileContentSchema>;

const FileWriteResultSchema = z.object({ hash: z.string() });

export const NoteAnchorSchema = z.object({
  exact: z.string(),
  prefix: z.string(),
  suffix: z.string(),
});
export type NoteAnchor = z.infer<typeof NoteAnchorSchema>;

export const NoteReplySchema = z.object({
  author: z.enum(["owner", "agent"]),
  text: z.string(),
  at: z.string().datetime({ offset: true }),
});
export type NoteReply = z.infer<typeof NoteReplySchema>;

export const NoteStatusSchema = z.enum(["open", "resolved"]);
export type NoteStatus = z.infer<typeof NoteStatusSchema>;

export const NoteSchema = z.object({
  id: z.string(),
  anchor: NoteAnchorSchema,
  text: z.string(),
  created_at: z.string().datetime({ offset: true }),
  archived: z.boolean(),
  replies: z.array(NoteReplySchema),
  status: NoteStatusSchema,
});
export type Note = z.infer<typeof NoteSchema>;

// GET .../notes adds the fields only a listing needs: which file it's on,
// whether its anchor still resolves against that file's current text, the
// 1-based line it resolves to, and the character offsets of its exact span in
// the file's raw source -- all null for an orphaned note, whose anchor has no
// location any more.
export const NoteListingEntrySchema = NoteSchema.extend({
  path: z.string(),
  orphaned: z.boolean(),
  line: z.number().nullable(),
  start: z.number().nullable(),
  end: z.number().nullable(),
});
export type NoteListingEntry = z.infer<typeof NoteListingEntrySchema>;

const NotesListingSchema = z.object({ notes: z.array(NoteListingEntrySchema) });
const NoteResultSchema = z.object({ note: NoteSchema });

// One annotated file's note aggregate, as the tree's per-file status dot reads it: whether it
// has any notes at all, and the most recent agent reply across them -- `null` when it has none.
export const FileNoteSummarySchema = z.object({
  path: z.string(),
  open_notes: z.number(),
  resolved_notes: z.number(),
  latest_agent_reply_at: z.string().datetime({ offset: true }).nullable(),
});
export type FileNoteSummary = z.infer<typeof FileNoteSummarySchema>;

const NotesSummarySchema = z.object({ files: z.array(FileNoteSummarySchema) });

export const SkillBodySchema = z.object({
  content: z.string(),
  hash: z.string(),
  editable: z.boolean(),
});
export type SkillBody = z.infer<typeof SkillBodySchema>;

const SkillBodyWriteResultSchema = z.object({ hash: z.string() });

export const SkillCardSchema = z.object({
  name: z.string(),
  description: z.string(),
  library: z.string(),
  path: z.string(),
  valid: z.boolean(),
  error: z.string().nullable(),
});
export type SkillCard = z.infer<typeof SkillCardSchema>;

const SkillsListingSchema = z.object({ skills: z.array(SkillCardSchema) });
const SkillInstallResultSchema = z.object({ path: z.string() });

export interface NotePatch {
  path: string;
  text?: string;
  archived?: boolean;
}

export interface ReplyPatch {
  path: string;
  replyIndex: number;
  text: string;
}

export interface SkillBodyWrite {
  library: string;
  name: string;
  content: string;
  expectedHash?: string;
}

// Every path this client talks to, one place. The proposed contract's paths
// and field names are pending backend adoption -- a change there is a change
// to this map, not to the methods below.
export const endpoints = {
  projects: "/api/projects",
  project: (id: string) => `/api/projects/${encodeURIComponent(id)}`,
  files: (id: string) => `/api/projects/${encodeURIComponent(id)}/files`,
  file: (id: string) => `/api/projects/${encodeURIComponent(id)}/file`,
  create: (id: string) => `/api/projects/${encodeURIComponent(id)}/file/create`,
  fileRename: (id: string) => `/api/projects/${encodeURIComponent(id)}/file/rename`,
  dirCreate: (id: string) => `/api/projects/${encodeURIComponent(id)}/dir/create`,
  notes: (id: string) => `/api/projects/${encodeURIComponent(id)}/notes`,
  notesSummary: (id: string) => `/api/projects/${encodeURIComponent(id)}/notes/summary`,
  note: (id: string, noteId: string) =>
    `/api/projects/${encodeURIComponent(id)}/notes/${encodeURIComponent(noteId)}`,
  noteReply: (id: string, noteId: string) =>
    `/api/projects/${encodeURIComponent(id)}/notes/${encodeURIComponent(noteId)}/reply`,
  noteResolve: (id: string, noteId: string) =>
    `/api/projects/${encodeURIComponent(id)}/notes/${encodeURIComponent(noteId)}/resolve`,
  noteReopen: (id: string, noteId: string) =>
    `/api/projects/${encodeURIComponent(id)}/notes/${encodeURIComponent(noteId)}/reopen`,
  skills: (id: string) => `/api/projects/${encodeURIComponent(id)}/skills`,
  skillBody: (id: string) => `/api/projects/${encodeURIComponent(id)}/skills/body`,
} as const;

// Every react-query key this workspace mounts, one place. All of them nest
// under `all`, and each per-file key nests under its resource's project-wide
// key, so invalidating a prefix reaches every narrower query beneath it:
// `notes(id)` (the Notes panel's listing) is the prefix of `notes(id, path)`
// (one file's margin), and one invalidation refreshes both.
export const projectKeys = {
  all: ["projects"] as const,
  files: (id: string) => ["projects", id, "files"] as const,
  file: (id: string, path: string) => ["projects", id, "file", path] as const,
  notes: (id: string, path?: string) =>
    path === undefined
      ? (["projects", id, "notes"] as const)
      : (["projects", id, "notes", path] as const),
  notesSummary: (id: string) => ["projects", id, "notes", "summary"] as const,
  skills: (id: string) => ["projects", id, "skills"] as const,
  skillBody: (id: string, library: string, name: string) =>
    ["projects", id, "skills", "body", library, name] as const,
};

function withPathQuery(path: string, filePath: string | undefined): string {
  return filePath === undefined ? path : `${path}?path=${encodeURIComponent(filePath)}`;
}

const { request } = createApiClient({ baseUrl: STUDIO_API_URL });

// Every method below hands back the resource a caller actually wants, not the
// wire envelope: `{"entries": [...]}` becomes `FileEntry[]`, `{"project":
// ProjectView}` becomes `Project`, and so on. The envelope is still parsed in
// full (a drift in it still fails at the boundary) -- it's just never a shape
// a caller has to destructure.
export const projectsApi = {
  list: (): Promise<ProjectListingEntry[]> =>
    request<{ projects: ProjectListingEntry[] }>(endpoints.projects, ProjectsListingSchema).then(
      (result) => result.projects,
    ),

  register: (rootPath: string): Promise<Project> =>
    request<{ project: Project }>(endpoints.projects, ProjectResultSchema, {
      method: "POST",
      body: JSON.stringify({ root_path: rootPath }),
    }).then((result) => result.project),

  rename: (id: string, name: string): Promise<Project> =>
    request<{ project: Project }>(endpoints.project(id), ProjectResultSchema, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }).then((result) => result.project),

  unregister: (id: string): Promise<void> =>
    request<void>(endpoints.project(id), VoidSchema, { method: "DELETE" }),

  files: (id: string): Promise<FileEntry[]> =>
    request<{ entries: FileEntry[] }>(endpoints.files(id), FileListingSchema).then(
      (result) => result.entries,
    ),

  readFile: (id: string, path: string): Promise<FileContent> =>
    request<FileContent>(withPathQuery(endpoints.file(id), path), FileContentSchema),

  // Creates a file that must not exist yet: the backend refuses rather than
  // truncating, which a `writeFile` with empty content could not distinguish.
  createFile: (id: string, path: string): Promise<string> =>
    request<{ hash: string }>(endpoints.create(id), FileWriteResultSchema, {
      method: "POST",
      body: JSON.stringify({ path }),
    }).then((result) => result.hash),

  writeFile: (id: string, path: string, content: string, expectedHash?: string): Promise<string> =>
    request<{ hash: string }>(endpoints.file(id), FileWriteResultSchema, {
      method: "PUT",
      body: JSON.stringify({ path, content, expected_hash: expectedHash }),
    }).then((result) => result.hash),

  renameFile: (id: string, path: string, to: string): Promise<void> =>
    request<void>(endpoints.fileRename(id), VoidSchema, {
      method: "POST",
      body: JSON.stringify({ path, to }),
    }),

  createDir: (id: string, path: string): Promise<void> =>
    request<void>(endpoints.dirCreate(id), VoidSchema, {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  deleteFile: (id: string, path: string): Promise<void> =>
    request<void>(withPathQuery(endpoints.file(id), path), VoidSchema, { method: "DELETE" }),

  notes: (id: string, path?: string): Promise<NoteListingEntry[]> =>
    request<{ notes: NoteListingEntry[] }>(
      withPathQuery(endpoints.notes(id), path),
      NotesListingSchema,
    ).then((result) => result.notes),

  createNote: (id: string, path: string, anchor: NoteAnchor, text: string): Promise<Note> =>
    request<{ note: Note }>(endpoints.notes(id), NoteResultSchema, {
      method: "POST",
      body: JSON.stringify({ path, anchor, text }),
    }).then((result) => result.note),

  updateNote: (id: string, noteId: string, patch: NotePatch): Promise<Note> =>
    request<{ note: Note }>(endpoints.note(id, noteId), NoteResultSchema, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }).then((result) => result.note),

  deleteNote: (id: string, noteId: string, path: string): Promise<void> =>
    request<void>(withPathQuery(endpoints.note(id, noteId), path), VoidSchema, {
      method: "DELETE",
    }),

  replyToNote: (id: string, noteId: string, path: string, text: string): Promise<Note> =>
    request<{ note: Note }>(endpoints.noteReply(id, noteId), NoteResultSchema, {
      method: "POST",
      body: JSON.stringify({ path, text }),
    }).then((result) => result.note),

  // PATCH on the same route POST uses to create a reply -- editing an existing one by its
  // 0-based position in the thread. The backend refuses editing an agent-authored reply.
  updateReply: (id: string, noteId: string, patch: ReplyPatch): Promise<Note> =>
    request<{ note: Note }>(endpoints.noteReply(id, noteId), NoteResultSchema, {
      method: "PATCH",
      body: JSON.stringify({ path: patch.path, reply_index: patch.replyIndex, text: patch.text }),
    }).then((result) => result.note),

  resolveNote: (id: string, noteId: string, path: string): Promise<Note> =>
    request<{ note: Note }>(
      withPathQuery(endpoints.noteResolve(id, noteId), path),
      NoteResultSchema,
      { method: "POST" },
    ).then((result) => result.note),

  reopenNote: (id: string, noteId: string, path: string): Promise<Note> =>
    request<{ note: Note }>(
      withPathQuery(endpoints.noteReopen(id, noteId), path),
      NoteResultSchema,
      { method: "POST" },
    ).then((result) => result.note),

  skills: (id: string): Promise<SkillCard[]> =>
    request<{ skills: SkillCard[] }>(endpoints.skills(id), SkillsListingSchema).then(
      (result) => result.skills,
    ),

  installSkill: (id: string, library: string, name: string): Promise<string> =>
    request<{ path: string }>(endpoints.skills(id), SkillInstallResultSchema, {
      method: "POST",
      body: JSON.stringify({ library, name }),
    }).then((result) => result.path),

  skillBody: (id: string, library: string, name: string): Promise<SkillBody> =>
    request<SkillBody>(
      `${endpoints.skillBody(id)}?library=${encodeURIComponent(library)}&name=${encodeURIComponent(name)}`,
      SkillBodySchema,
    ),

  writeSkillBody: (id: string, write: SkillBodyWrite): Promise<string> =>
    request<{ hash: string }>(endpoints.skillBody(id), SkillBodyWriteResultSchema, {
      method: "PUT",
      body: JSON.stringify({
        library: write.library,
        name: write.name,
        content: write.content,
        expected_hash: write.expectedHash,
      }),
    }).then((result) => result.hash),

  notesSummary: (id: string): Promise<FileNoteSummary[]> =>
    request<{ files: FileNoteSummary[] }>(endpoints.notesSummary(id), NotesSummarySchema).then(
      (result) => result.files,
    ),
};
