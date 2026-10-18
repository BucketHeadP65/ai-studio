"use client";

import { useQuery } from "@tanstack/react-query";
import { Button } from "client-core/ui/button";
import { Input } from "client-core/ui/input";
import { Modal, ModalTitle } from "client-core/ui/modal";
import { Textarea } from "client-core/ui/textarea";
import { useEffect, useId, useState } from "react";
import { z } from "zod";

import { knowledgeBaseApi, knowledgeBaseErrorMessage } from "@/api/knowledge-base";

// The "Minimal" starting point: a PARTIAL KnowledgeBaseSchema — only what a fresh knowledge base
// needs beyond the backend's own defaults (engram.kb.domain.schema.KnowledgeBaseSchema field defaults
// fill in the rest, e.g. retrieval.profile, section_summaries). `slug` and `retrieval.collection`
// are deliberately absent here: the registry name a person types IS the knowledge base's
// collection namespace, so the dialog stamps both from the `name` field at submit time, never
// hand-typed twice, never silently mismatched (the backend refuses a request where they disagree).
const MINIMAL_TEMPLATE = {
  purpose: "",
  page_types: [{ page_type: "note", id_prefix: "note" }],
};

type Mode = "template" | "minimal" | "paste";

// The shape a schema must have to even reach the backend's own invariant checks
// (KnowledgeBaseSchema._check_invariants needs a non-empty page_types, each with a page_type).
// Anything beyond that, frontmatter fields, edge fields, claims, is the backend's validation to
// do; this schema only catches the two mistakes a person can make before the network
// round-trip, and `.passthrough()` leaves everything else untouched.
const KnowledgeBaseSchemaDraftSchema = z.looseObject({
  slug: z.string().min(1, "Name a knowledge base before creating it."),
  page_types: z
    .array(z.looseObject({ page_type: z.string().min(1) }))
    .min(1, "A schema needs at least one page type."),
});

export type KnowledgeBaseSchemaDraft = z.infer<typeof KnowledgeBaseSchemaDraftSchema>;

// Exactly one of `schema`/`template` is set: `schema` for the Minimal and Paste starting points,
// `template` (a `GET /api/templates` slug) for a template picked from the catalog.
export interface CreateKnowledgeBasePayload {
  name: string;
  path: string;
  schema?: KnowledgeBaseSchemaDraft;
  template?: string;
}

interface Props {
  open: boolean;
  creating: boolean;
  error?: string | null;
  onCreate: (payload: CreateKnowledgeBasePayload) => void;
  onCancel: () => void;
  // Builds the same payload `onCreate` would and hands it here instead: the caller creates
  // the knowledge base first, then, only on success, opens the terminal pane at its
  // now-existing, now-registered path, with the engram-init interview skill a `claude` prompt
  // away. A path that doesn't exist yet can never pass the terminal's own root-containment gate,
  // so "design it with AI" has to create before it can open a pane there. Disabled only for want
  // of a path to fill in; the terminal itself decides whether that path is reachable (the
  // client never enforces that policy).
  onDesignWithAI: (payload: CreateKnowledgeBasePayload) => void;
}

export function CreateKnowledgeBaseDialog({
  open,
  creating,
  error,
  onCreate,
  onCancel,
  onDesignWithAI,
}: Props) {
  const nameId = useId();
  const pathId = useId();
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [mode, setMode] = useState<Mode>("minimal");
  const [templateSlug, setTemplateSlug] = useState("");
  const [pastedJson, setPastedJson] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  // Only fetched while the dialog is actually open: the template catalog rarely changes within a
  // session, so there is nothing worth loading behind a dialog nobody has opened yet.
  const templatesQuery = useQuery({
    queryKey: ["knowledge-base", "templates"] as const,
    queryFn: () => knowledgeBaseApi.templates(),
    enabled: open,
  });
  const templates = templatesQuery.data?.templates ?? [];

  // A fresh form every time the dialog opens: the component instance persists across
  // opens, so nothing typed for a discarded attempt would otherwise linger into the next
  // one. Minimal starts selected; a template from the catalog is one click away.
  useEffect(() => {
    if (!open) return;
    setName("");
    setPath("");
    setMode("minimal");
    setTemplateSlug("");
    setPastedJson("");
    setFormError(null);
  }, [open]);

  // Shared by the "Create" and "Design it with AI" buttons: both need the same validated
  // payload, they only differ in what happens to it next (see the two thin callers below).
  function buildPayload(): CreateKnowledgeBasePayload | null {
    const trimmedName = name.trim();
    const trimmedPath = path.trim();
    if (!trimmedName || !trimmedPath) {
      setFormError("Name and path are both required.");
      return null;
    }

    if (mode === "template") {
      setFormError(null);
      return { name: trimmedName, path: trimmedPath, template: templateSlug };
    }

    let raw: unknown;
    if (mode === "paste") {
      try {
        raw = JSON.parse(pastedJson);
      } catch (parseError) {
        setFormError(
          `Invalid JSON: ${parseError instanceof Error ? parseError.message : String(parseError)}`,
        );
        return null;
      }
    } else {
      raw = { ...MINIMAL_TEMPLATE, slug: trimmedName, retrieval: { collection: trimmedName } };
    }

    const parsed = KnowledgeBaseSchemaDraftSchema.safeParse(raw);
    if (!parsed.success) {
      setFormError(
        parsed.error.issues[0]?.message ?? "That schema is not shaped like a knowledge base schema.",
      );
      return null;
    }

    setFormError(null);
    return { name: trimmedName, path: trimmedPath, schema: parsed.data };
  }

  function submit() {
    const payload = buildPayload();
    if (payload) onCreate(payload);
  }

  function submitAndDesign() {
    const payload = buildPayload();
    if (payload) onDesignWithAI(payload);
  }

  return (
    <Modal
      open={open}
      onClose={onCancel}
      className="flex max-h-[85vh] max-w-lg flex-col gap-3 overflow-y-auto"
    >
      <ModalTitle>New knowledge base</ModalTitle>

      <label htmlFor={nameId} className="flex flex-col gap-1 text-caption text-label-2">
        Name
        <Input
          id={nameId}
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="e.g. inbox"
        />
      </label>

      <label htmlFor={pathId} className="flex flex-col gap-1 text-caption text-label-2">
        Path
        <Input
          id={pathId}
          value={path}
          onChange={(event) => setPath(event.target.value)}
          placeholder="e.g. /Users/you/knowledge-bases/inbox"
        />
      </label>

      <fieldset className="flex flex-col gap-1.5">
        <legend className="text-caption text-label-2">Template</legend>
        {templates.map((template) => (
          <label key={template.slug} className="flex items-center gap-2 text-body text-label-2">
            <input
              type="radio"
              name="template"
              checked={mode === "template" && templateSlug === template.slug}
              onChange={() => {
                setMode("template");
                setTemplateSlug(template.slug);
              }}
            />
            {template.title}
          </label>
        ))}
        <label className="flex items-center gap-2 text-body text-label-2">
          <input
            type="radio"
            name="template"
            checked={mode === "minimal"}
            onChange={() => setMode("minimal")}
          />
          Minimal
        </label>
        <label className="flex items-center gap-2 text-body text-label-2">
          <input
            type="radio"
            name="template"
            checked={mode === "paste"}
            onChange={() => setMode("paste")}
          />
          Paste a knowledge base schema JSON
        </label>
      </fieldset>

      {templatesQuery.isError ? (
        <p className="text-caption text-label-3">
          Could not load templates: {knowledgeBaseErrorMessage(templatesQuery.error)}
        </p>
      ) : null}

      {mode === "paste" ? (
        <Textarea
          aria-label="Knowledge base schema JSON"
          value={pastedJson}
          onChange={(event) => setPastedJson(event.target.value)}
          rows={8}
          placeholder='{"slug": "...", "page_types": [{"page_type": "..."}]}'
          className="font-mono text-caption"
        />
      ) : null}

      <div>
        <Button
          type="button"
          variant="secondary"
          disabled={path.trim().length === 0}
          title={path.trim().length === 0 ? "Enter a path first" : undefined}
          onClick={submitAndDesign}
        >
          Design it with AI
        </Button>
      </div>

      {(formError ?? error) ? (
        <p role="alert" className="text-caption text-danger-ink">
          {formError ?? error}
        </p>
      ) : null}

      <div className="mt-1 flex justify-end gap-2">
        <Button variant="secondary" onClick={onCancel} disabled={creating}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={creating}>
          {creating ? "Creating..." : "Create"}
        </Button>
      </div>
    </Modal>
  );
}
