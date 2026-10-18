"use client";

import { useQuery } from "@tanstack/react-query";
import { type ReactElement, useState } from "react";

import { runKeys } from "@/api/query-keys";
import { studioApi, studioErrorMessage } from "@/api/studio";
import type { ArtifactRef } from "@/api/studio-types";
import { artifactAnchorId } from "@/lib/workflow-href";

// One value a run fixed, named up front and read only when somebody asks for it. The read goes
// through the run's own resolve route with the reference exactly as the run minted it: the
// reference says which run, which node and which bytes, and the host answers with those bytes or
// refuses in its own words. Nothing here ever holds a URL, and nothing is fetched on render -- an
// artifact can be a megabyte, and a board full of them would fetch every one.

export interface ArtifactCardProps {
  runId: string;
  artifact: ArtifactRef;
}

const TEXT_MEDIA = ["text/markdown", "text/plain"];

// The resolved value as text: its own string for a media type that promises one, its formatted
// JSON otherwise. The host has already checked the value against its reference, so a string here
// is the artifact's own bytes rather than something this app re-derived.
function readable(media: ArtifactRef["media_type"], value: unknown): string {
  if (TEXT_MEDIA.includes(media) && typeof value === "string") return value;
  return JSON.stringify(value, null, 2) ?? "";
}

export function ArtifactCard({ runId, artifact }: ArtifactCardProps): ReactElement {
  const [asked, setAsked] = useState(false);
  const document = useQuery({
    queryKey: runKeys.artifact(runId, artifact.id, artifact.value_digest),
    queryFn: () => studioApi.resolveArtifact(runId, artifact),
    enabled: asked,
    // An artifact is immutable by construction: the digest names the bytes, so a resolved
    // document can never go stale under the reference that asked for it.
    staleTime: Number.POSITIVE_INFINITY,
  });

  return (
    // The id a link from elsewhere lands on, derived from the artifact id by the same function the
    // link builds its fragment with, so the two cannot drift apart.
    <div
      id={artifactAnchorId(artifact.id)}
      className="scroll-mt-4 overflow-hidden rounded-lg border border-line bg-surface"
    >
      <div className="flex items-center gap-2 px-4 py-2.5">
        <span className="min-w-0 flex-1 truncate font-medium text-body text-label-1">
          {artifact.label}
        </span>
        <span className="shrink-0 font-mono text-label-3 text-micro">{artifact.media_type}</span>
        <button
          type="button"
          onClick={() => setAsked(true)}
          disabled={document.isFetching}
          className="shrink-0 rounded border border-line px-2.5 py-1 text-caption text-label-2 hover:bg-well disabled:opacity-50"
        >
          {document.isFetching ? "Reading" : "Show"}
        </button>
      </div>
      {document.isError ? (
        <p
          role="alert"
          className="border-line border-t bg-danger-soft px-4 py-2 text-caption text-danger-ink"
        >
          {studioErrorMessage(document.error)}
        </p>
      ) : null}
      {document.data ? (
        <pre className="max-h-96 overflow-auto border-line border-t bg-well px-4 py-3 text-caption text-label-1 whitespace-pre-wrap">
          {readable(artifact.media_type, document.data.value)}
        </pre>
      ) : null}
    </div>
  );
}
