import { describe, expect, it } from "vitest";

import {
  editChanges,
  formatToolElapsed,
  pluralVerb,
  presentTool,
  summarizeResult,
} from "./tool-presentation";

describe("editChanges", () => {
  it("treats a CRLF file as the same lines as its LF equivalent, with no phantom rows", () => {
    const crlf = editChanges("a\r\nb\r\nc\r\n", "a\r\nB\r\nc\r\n");
    const lf = editChanges("a\nb\nc\n", "a\nB\nc\n");
    expect(crlf).toEqual(lf);
  });

  it("carries no carriage return in a changed line's own text", () => {
    const changes = editChanges("a\r\nb\r\n", "a\r\nB\r\n");
    for (const change of changes) {
      expect(change.value).not.toContain("\r");
    }
  });
});

describe("formatToolElapsed", () => {
  it("shows one decimal under ten seconds", () => {
    expect(formatToolElapsed(700)).toBe("0.7s");
    expect(formatToolElapsed(1234)).toBe("1.2s");
  });

  it("falls back to formatElapsedMs at ten seconds and beyond", () => {
    expect(formatToolElapsed(10_000)).toBe("10s");
    expect(formatToolElapsed(65_000)).toBe("1m 5s");
  });
});

describe("presentTool", () => {
  it("reads read_file as a read with its path", () => {
    expect(presentTool("read_file", { path: "docs/plan.md" })).toEqual({
      kind: "read",
      verb: "Read",
      subject: "docs/plan.md",
    });
  });

  it("reads write_file as an edit with its path", () => {
    expect(presentTool("write_file", { path: "docs/plan.md", content: "hi" })).toEqual({
      kind: "edit",
      verb: "Wrote",
      subject: "docs/plan.md",
    });
  });

  it("reads edit_file as an edit with its path", () => {
    expect(
      presentTool("edit_file", { path: "docs/plan.md", old_text: "a", new_text: "b" }),
    ).toEqual({
      kind: "edit",
      verb: "Edited",
      subject: "docs/plan.md",
    });
  });

  it("reads list_directory as a search with its path", () => {
    expect(presentTool("list_directory", { path: "scripts/" })).toEqual({
      kind: "search",
      verb: "Listed",
      subject: "scripts/",
    });
  });

  it("reads find_files as a search with its quoted pattern", () => {
    expect(presentTool("find_files", { pattern: "*.py", path: "src" })).toEqual({
      kind: "search",
      verb: "Found files",
      subject: '"*.py"',
    });
  });

  it("reads file_info as a read of its path", () => {
    expect(presentTool("file_info", { path: "docs/plan.md" })).toEqual({
      kind: "read",
      verb: "Inspected",
      subject: "docs/plan.md",
    });
  });

  it("reads create_directory as another kind of change to its path", () => {
    expect(presentTool("create_directory", { path: "docs/notes" })).toEqual({
      kind: "other",
      verb: "Created directory",
      subject: "docs/notes",
    });
  });

  it("reads search_files as a search with its quoted pattern", () => {
    expect(presentTool("search_files", { pattern: "plan", path: "docs" })).toEqual({
      kind: "search",
      verb: "Searched",
      subject: '"plan"',
    });
  });

  it("reads run_command as an execute with its command", () => {
    expect(presentTool("run_command", { command: "ls -la" })).toEqual({
      kind: "execute",
      verb: "Ran",
      subject: "ls -la",
    });
  });

  it("truncates a long run_command command to 60 chars", () => {
    const command = "a".repeat(80);
    const result = presentTool("run_command", { command });
    expect(result.subject).toBe(`${"a".repeat(60)}…`);
  });

  it("reads fetch with a url as a fetch of the hostname", () => {
    expect(presentTool("fetch", { url: "https://example.test/page" })).toEqual({
      kind: "fetch",
      verb: "Fetched",
      subject: "example.test",
    });
  });

  it("falls back to the raw string when fetch's url does not parse", () => {
    expect(presentTool("fetch", { url: "not a url" })).toEqual({
      kind: "fetch",
      verb: "Fetched",
      subject: "not a url",
    });
  });

  it("reads fetch with an id as a retrieve of the id", () => {
    expect(presentTool("fetch", { id: "note-1" })).toEqual({
      kind: "retrieve",
      verb: "Fetched",
      subject: "note-1",
    });
  });

  it("reads the studio search tool as a retrieve of its query", () => {
    expect(presentTool("search", { q: "compile guards" })).toEqual({
      kind: "retrieve",
      verb: "Retrieved",
      subject: '"compile guards"',
    });
  });

  it("reads the studio search tool's query field when q is absent", () => {
    expect(presentTool("search", { query: "compile guards" })).toEqual({
      kind: "retrieve",
      verb: "Retrieved",
      subject: '"compile guards"',
    });
  });

  it("reads graph_expand's singular id", () => {
    expect(presentTool("graph_expand", { id: "n1" })).toEqual({
      kind: "retrieve",
      verb: "Expanded",
      subject: "n1",
    });
  });

  it("reads graph_expand's plural ids", () => {
    expect(presentTool("graph_expand", { ids: ["n1", "n2"] })).toEqual({
      kind: "retrieve",
      verb: "Expanded",
      subject: "n1, n2",
    });
  });

  it("reads find_paths as a fixed retrieve", () => {
    expect(presentTool("find_paths", {})).toEqual({
      kind: "retrieve",
      verb: "Found paths",
      subject: null,
    });
  });

  it("reads run_sql as a fixed retrieve", () => {
    expect(presentTool("run_sql", { sql: "select 1" })).toEqual({
      kind: "retrieve",
      verb: "Queried the knowledge base",
      subject: null,
    });
  });

  it("reads describe_schema as a fixed retrieve", () => {
    expect(presentTool("describe_schema", {})).toEqual({
      kind: "retrieve",
      verb: "Described the schema",
      subject: null,
    });
  });

  it("reads run_code as a fixed execute", () => {
    expect(presentTool("run_code", { code: "1 + 1" })).toEqual({
      kind: "execute",
      verb: "Ran code",
      subject: null,
    });
  });

  it("falls back to the raw name for an unknown tool", () => {
    expect(presentTool("frobnicate", { x: 1 })).toEqual({
      kind: "other",
      verb: "frobnicate",
      subject: null,
    });
  });

  it("degrades to the verb alone while the call's input is still streaming", () => {
    expect(presentTool("read_file", undefined)).toEqual({
      kind: "read",
      verb: "Read",
      subject: null,
    });
  });
});

// read_file's real output: a header line naming the path, the line count, and a content hash,
// then the numbered content -- the fork's own `FileSystemToolset.read_file` shape.
function readFileOutput(path: string, lines: string[]): string {
  const numbered = lines.map((line, index) => `${index + 1}\t${line}`).join("\n");
  return `[${path} | ${lines.length} lines | hash:abc123]\n${numbered}\n`;
}

describe("summarizeResult", () => {
  it("summarizes a read_file result by the line count in its own header, never by counting", () => {
    expect(summarizeResult("read_file", {}, readFileOutput("a.md", ["one", "two", "three"]))).toBe(
      "3 lines",
    );
  });

  it("summarizes a single-line read_file result in the singular", () => {
    expect(summarizeResult("read_file", {}, readFileOutput("a.md", ["one"]))).toBe("1 line");
  });

  it("returns null for a read_file result carrying no header to parse", () => {
    expect(summarizeResult("read_file", {}, "one\ntwo\nthree")).toBeNull();
  });

  it("summarizes a list_directory result by its entry count", () => {
    expect(summarizeResult("list_directory", {}, "a.md  (10 bytes)\nb.md  (20 bytes)")).toBe(
      "2 entries",
    );
  });

  it("summarizes a single-entry list_directory result in the singular", () => {
    expect(summarizeResult("list_directory", {}, "a.md  (10 bytes)")).toBe("1 entry");
  });

  it("summarizes list_directory's empty-directory sentinel as empty", () => {
    expect(summarizeResult("list_directory", {}, "(empty directory)")).toBe("empty");
  });

  it("summarizes a write_file result by the input content's line count", () => {
    expect(summarizeResult("write_file", { content: "one\ntwo" }, "wrote it")).toBe("2 lines");
  });

  it("summarizes a single-line write_file result in the singular", () => {
    expect(summarizeResult("write_file", { content: "one" }, "wrote it")).toBe("1 line");
  });

  it("counts lines appended after a last line with no newline as pure additions", () => {
    const input = { path: "README.md", old_text: "a\nb", new_text: "a\nb\nc\nd\n" };
    expect(summarizeResult("edit_file", input, undefined)).toBe("+2 −0 lines");
  });

  it("summarizes an edit_file result by the diff of old_text and new_text", () => {
    const input = { old_text: "a\nb\nc\n", new_text: "a\nB\nc\nd\n" };
    expect(summarizeResult("edit_file", input, undefined)).toBe("+2 −1 lines");
  });

  it("returns null for an edit with no old_text or new_text yet", () => {
    expect(summarizeResult("edit_file", {}, undefined)).toBeNull();
  });

  it("summarizes a search_files result by its non-empty match count", () => {
    expect(summarizeResult("search_files", {}, "a.md:1:x\nb.md:2:y")).toBe("2 matches");
  });

  it("summarizes a single-match search_files result in the singular", () => {
    expect(summarizeResult("search_files", {}, "a.md:1:x")).toBe("1 match");
  });

  it("summarizes search_files's no-matches sentinel as no matches", () => {
    expect(summarizeResult("search_files", {}, "No matches found.")).toBe("no matches");
  });

  it("summarizes a run_command result with an exit code line", () => {
    expect(summarizeResult("run_command", {}, "line one\nline two\nexit code: 1")).toBe(
      "exit 1, 3 lines",
    );
  });

  it("summarizes a run_command result with no exit code line", () => {
    expect(summarizeResult("run_command", {}, "line one\nline two")).toBe("2 lines");
  });

  it("summarizes a single-line run_command result in the singular", () => {
    expect(summarizeResult("run_command", {}, "line one")).toBe("1 line");
  });

  it("summarizes a fetch result by its line count", () => {
    expect(summarizeResult("fetch", {}, "line one\nline two")).toBe("2 lines");
  });

  it("summarizes a single-line fetch result in the singular", () => {
    expect(summarizeResult("fetch", {}, "line one")).toBe("1 line");
  });

  it("summarizes a retrieve result over an array by its length", () => {
    expect(summarizeResult("search", {}, [1, 2, 3])).toBe("3 results");
  });

  it("summarizes a single-result retrieve result in the singular", () => {
    expect(summarizeResult("search", {}, [1])).toBe("1 result");
  });

  it("summarizes a retrieve result over an object's results array", () => {
    expect(summarizeResult("search", {}, { results: [1, 2] })).toBe("2 results");
  });

  it("summarizes a retrieve result over a string by its line count", () => {
    expect(summarizeResult("search", {}, "one\ntwo\nthree")).toBe("3 lines");
  });

  it("returns null for a retrieve result with nothing to count", () => {
    expect(summarizeResult("search", {}, { hits: 3 })).toBeNull();
  });

  it("summarizes an unknown tool's string result by its first non-empty line", () => {
    expect(summarizeResult("frobnicate", {}, "\n  first line  \nsecond line")).toBe("first line");
  });

  it("truncates an unknown tool's summary line to 80 chars", () => {
    const line = "x".repeat(120);
    expect(summarizeResult("frobnicate", {}, line)).toBe(`${"x".repeat(80)}…`);
  });

  it("summarizes an unknown tool's object result by its key count", () => {
    expect(summarizeResult("frobnicate", {}, { a: 1, b: 2 })).toBe("2 keys");
  });

  it("returns null for an unknown tool's result with nothing to summarize", () => {
    expect(summarizeResult("frobnicate", {}, 42)).toBeNull();
  });

  it("summarizes a find_files result by its match count, the same as search_files", () => {
    expect(summarizeResult("find_files", {}, "a.py\nb.py")).toBe("2 matches");
  });

  it("summarizes a single-match find_files result in the singular", () => {
    expect(summarizeResult("find_files", {}, "a.py")).toBe("1 match");
  });

  it("summarizes find_files's no-matches sentinel as no matches", () => {
    expect(summarizeResult("find_files", {}, "No matches found.")).toBe("no matches");
  });

  it("summarizes a file_info result by its first line", () => {
    expect(summarizeResult("file_info", {}, "path: docs/plan.md\ntype: file\nsize: 12 bytes")).toBe(
      "path: docs/plan.md",
    );
  });

  it("summarizes a create_directory result by its first line", () => {
    expect(summarizeResult("create_directory", {}, "Created directory: docs/notes")).toBe(
      "Created directory: docs/notes",
    );
  });
});

describe("pluralVerb", () => {
  it("names a run of commands", () => {
    expect(pluralVerb("execute", 3)).toBe("Ran 3 commands");
  });

  it("names a run of reads", () => {
    expect(pluralVerb("read", 4)).toBe("Read 4 files");
  });

  it("names a run of edits", () => {
    expect(pluralVerb("edit", 2)).toBe("Edited 2 files");
  });

  it("names a run of searches", () => {
    expect(pluralVerb("search", 3)).toBe("Searched 3 times");
  });

  it("names a run of fetches", () => {
    expect(pluralVerb("fetch", 2)).toBe("Fetched 2 pages");
  });

  it("names a run of retrievals", () => {
    expect(pluralVerb("retrieve", 3)).toBe("Retrieved 3 times");
  });

  it("names a run of unknown tool calls", () => {
    expect(pluralVerb("other", 3)).toBe("Called 3 tools");
  });
});
