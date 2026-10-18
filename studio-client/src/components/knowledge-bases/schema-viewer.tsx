"use client";

import { useQuery } from "@tanstack/react-query";
import { Button } from "client-core/ui/button";
import { Modal, ModalTitle } from "client-core/ui/modal";

import { knowledgeBaseApi, knowledgeBaseErrorMessage } from "@/api/knowledge-base";
import { knowledgeBaseKeys } from "@/api/query-keys";

interface Props {
  name: string | null;
  onClose: () => void;
}

// A read-only look at one registered knowledge base's parsed schema, the schema `POST
// /api/knowledge-bases` scaffolded it from. Fetches on demand (only while a name is selected)
// rather than joining onto the list query, since most rows in the list are never opened.
export function KnowledgeBaseSchemaViewer({ name, onClose }: Props) {
  const schema = useQuery({
    queryKey: knowledgeBaseKeys.schema(name),
    queryFn: () => knowledgeBaseApi.knowledgeBaseSchema(name ?? ""),
    enabled: name !== null,
  });

  return (
    <Modal
      open={name !== null}
      onClose={onClose}
      className="flex max-h-[80vh] max-w-2xl flex-col gap-3"
    >
      <div className="flex items-center justify-between gap-3">
        <ModalTitle>{name}</ModalTitle>
        <Button variant="secondary" className="h-7 px-2.5 text-caption" onClick={onClose}>
          Close
        </Button>
      </div>

      {schema.isPending ? <p className="text-body text-label-3">Loading schema...</p> : null}
      {schema.isError ? (
        <p role="alert" className="text-caption text-danger-ink">
          {knowledgeBaseErrorMessage(schema.error)}
        </p>
      ) : null}
      {schema.data ? (
        <pre className="min-h-0 flex-1 overflow-auto rounded-md bg-well p-3 text-caption text-label-2">
          {JSON.stringify(schema.data, null, 2)}
        </pre>
      ) : null}
    </Modal>
  );
}
