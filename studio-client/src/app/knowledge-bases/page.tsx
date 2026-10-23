"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { useRef, useState } from "react";

import { knowledgeBaseApi, knowledgeBaseErrorMessage } from "@/api/knowledge-base";
import { knowledgeBaseKeys } from "@/api/query-keys";
import type { KnowledgeBaseListEntry } from "@/api/types";
import { CreateKnowledgeBaseDialog } from "@/components/knowledge-bases/create-dialog";
import { KnowledgeBasesList } from "@/components/knowledge-bases/list";
import { KnowledgeBaseSchemaViewer } from "@/components/knowledge-bases/schema-viewer";
import { Button } from "@/shared/ui/button";

// Loaded client-side ONLY. xterm reads `self` at module evaluation, which does not
// exist in Node, a plain import made the server render of this route throw, so
// `GET /knowledge-bases` answered 500 and the page only appeared because the client
// re-rendered it. Same split as graph-canvas against sigma's WebGL mount.
const TerminalPane = dynamic(
  () => import("@/components/terminal/pane").then((module) => module.TerminalPane),
  {
    ssr: false,
  },
);

// The Knowledge-bases station: every knowledge base the studio knows about, a way to make a new
// one, and a look at any registered knowledge base's schema. The registry (what's declared) and the
// indexed DB (what actually exists) are two independent truths the list renders side by
// side, see KnowledgeBasesList.
//
// The header renders before the fetch resolves, so the station is identifiable while the
// list is still loading.

// "4 bases, 1 broken, 1 not indexed." Zero parts drop out entirely rather than reading
// "0 broken"; an empty registry reads "No bases yet." A broken base's index state is never
// counted as also not indexed, its own row already says "Broken." instead.
function summarySentence(entries: readonly KnowledgeBaseListEntry[]): string {
  if (entries.length === 0) return "No bases yet.";
  const broken = entries.filter((entry) => entry.broken).length;
  const notIndexed = entries.filter(
    (entry) => !entry.broken && entry.registered && !entry.indexed,
  ).length;
  const parts = [`${entries.length} ${entries.length === 1 ? "base" : "bases"}`];
  if (broken > 0) parts.push(`${broken} broken`);
  if (notIndexed > 0) parts.push(`${notIndexed} not indexed`);
  return `${parts.join(", ")}.`;
}

export default function KnowledgeBasesPage() {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [viewingSchema, setViewingSchema] = useState<string | null>(null);
  const [terminalCwd, setTerminalCwd] = useState<string | null>(null);

  const knowledgeBases = useQuery({
    queryKey: knowledgeBaseKeys.all,
    queryFn: () => knowledgeBaseApi.knowledgeBases(),
  });

  // Which button submitted the in-flight mutation: a plain ref (not state) so onSuccess always
  // reads the value as of the click that started this request, never a stale render's closure.
  const submitIntent = useRef<"create" | "design">("create");

  const create = useMutation({
    mutationFn: knowledgeBaseApi.createKnowledgeBase,
    onSuccess: (_report, variables) => {
      setDialogOpen(false);
      queryClient.invalidateQueries({ queryKey: knowledgeBaseKeys.all });
      // "Design it with AI" creates first: the terminal's own cwd gate only accepts an
      // existing, registered root, so the pane can't open until this request has succeeded.
      if (submitIntent.current === "design") {
        setTerminalCwd(variables.path);
      }
    },
  });

  const entries = knowledgeBases.data?.knowledge_bases ?? [];

  return (
    <div className="-m-6 flex flex-col">
      <div className="flex h-[70px] items-center gap-3 border-line border-b bg-surface px-7">
        <div className="min-w-0">
          <h1 className="font-bold text-label-1 text-title">Knowledge bases</h1>
          <p className="truncate text-caption text-label-3">{summarySentence(entries)}</p>
        </div>
        <div className="ml-auto flex items-center gap-2.5">
          <Button onClick={() => setDialogOpen(true)}>New base</Button>
        </div>
      </div>

      <div className="flex flex-col gap-4 px-7 pt-5 pb-7">
        {knowledgeBases.isError ? (
          <p
            role="alert"
            className="rounded-md bg-danger-soft px-3 py-1.5 text-caption text-danger-ink"
          >
            Is the knowledge base API running on :8100? {knowledgeBaseErrorMessage(knowledgeBases.error)}
          </p>
        ) : null}

        <KnowledgeBasesList
          entries={entries}
          loading={knowledgeBases.isPending}
          onViewSchema={setViewingSchema}
          onOpenTerminal={setTerminalCwd}
        />
      </div>

      <CreateKnowledgeBaseDialog
        open={dialogOpen}
        creating={create.isPending}
        error={create.isError ? knowledgeBaseErrorMessage(create.error) : null}
        onCreate={(payload) => {
          submitIntent.current = "create";
          create.mutate(payload);
        }}
        onCancel={() => setDialogOpen(false)}
        onDesignWithAI={(payload) => {
          submitIntent.current = "design";
          create.mutate(payload);
        }}
      />

      <KnowledgeBaseSchemaViewer name={viewingSchema} onClose={() => setViewingSchema(null)} />

      <TerminalPane cwd={terminalCwd} onClose={() => setTerminalCwd(null)} />
    </div>
  );
}
