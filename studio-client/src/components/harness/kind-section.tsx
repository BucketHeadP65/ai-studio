"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PackageOpen } from "lucide-react";
import { useState } from "react";

import { harnessApi } from "@/api/harness";
import type {
  HarnessAction,
  HarnessArtifact,
  HarnessDeletion,
  HarnessKind,
  PromotionAction,
} from "@/api/harness-types";
import { studioErrorMessage } from "@/api/studio";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import { Card } from "@/shared/ui/card";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { EmptyState } from "@/shared/ui/empty-state";

interface Props {
  kind: HarnessKind;
  title: string;
  blurb: string;
  empty: string;
  emptyHint?: string;
  filter?: string;
  onDiscuss: (artifact: HarnessArtifact) => void;
}

// One custom kind's shelf: every artifact of that kind, with the owner's acts inline.
// Each section owns its own query, the three kinds are three independent listings on
// the backend, so one failing (or empty) kind never blanks the others. `next_step`
// from the listing decides which button renders as the primary act; disable/demote
// stay available as secondary acts regardless. Delete exists only on a disabled row --
// the composition never changes as a side effect of tidying the shelf -- and Discuss
// hands the row to the page's agent drawer, scoped to this artifact.
// The word a person sees for each kind, so the delete dialog never shows the raw enum value.
const KIND_WORD: Record<HarnessKind, string> = { capability: "capability", step: "script", agent: "agent", profile: "profile" };

export function HarnessKindSection({
  kind,
  title,
  blurb,
  empty,
  emptyHint,
  filter = "",
  onDiscuss,
}: Props) {
  const queryClient = useQueryClient();
  const listKey = ["harness", kind] as const;
  const [actError, setActError] = useState<string | null>(null);
  const [condemned, setCondemned] = useState<string | null>(null);

  const listing = useQuery({ queryKey: listKey, queryFn: () => harnessApi.list(kind) });
  const needle = filter.trim().toLowerCase();
  const artifacts = (listing.data?.artifacts ?? []).filter(
    (artifact) =>
      needle === "" ||
      artifact.name.toLowerCase().includes(needle) ||
      artifact.descriptor.toLowerCase().includes(needle),
  );

  const act = useMutation({
    mutationFn: ({
      verb,
      name,
    }: {
      verb: "activate" | "disable" | "promote" | "demote";
      name: string;
    }): Promise<HarnessAction | PromotionAction> => {
      if (verb === "promote") {
        return harnessApi.promote(name);
      }
      if (verb === "demote") {
        return harnessApi.demote(name);
      }
      return harnessApi[verb](kind, name);
    },
    onMutate: () => setActError(null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: listKey });
      queryClient.invalidateQueries({ queryKey: ["harness", "composition"] });
    },
    onError: (error) => setActError(studioErrorMessage(error)),
  });

  const remove = useMutation({
    mutationFn: (name: string): Promise<HarnessDeletion> => harnessApi.remove(kind, name),
    onMutate: () => setActError(null),
    onSuccess: () => {
      setCondemned(null);
      queryClient.invalidateQueries({ queryKey: listKey });
      queryClient.invalidateQueries({ queryKey: ["harness", "composition"] });
    },
    onError: (error) => {
      setCondemned(null);
      setActError(studioErrorMessage(error));
    },
  });

  return (
    <Card className="px-4 py-3">
      <h2 className="font-medium text-label-1 text-sm">
        {title}
        {listing.data && listing.data.artifacts.length > 0
          ? `, ${listing.data.artifacts.length}`
          : ""}
      </h2>
      <p className="mt-0.5 text-caption text-label-3">{blurb}</p>

      {listing.isPending ? <p className="mt-2 text-caption text-label-3">Loading...</p> : null}
      {listing.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {studioErrorMessage(listing.error)}
        </p>
      ) : null}
      {actError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {actError}
        </p>
      ) : null}

      {listing.data ? (
        listing.data.artifacts.length === 0 ? (
          <EmptyState
            className="py-5"
            icon={<PackageOpen size={18} strokeWidth={1.5} />}
            title={empty}
            hint={emptyHint}
          />
        ) : artifacts.length === 0 ? (
          <p className="mt-2 text-caption text-label-3">No {kind} matches the filter.</p>
        ) : (
          <ul className="mt-2 divide-y divide-line">
            {artifacts.map((artifact) => (
              <ArtifactRow
                key={artifact.name}
                artifact={artifact}
                busy={act.isPending || remove.isPending}
                onAct={(verb) => act.mutate({ verb, name: artifact.name })}
                onDelete={() => setCondemned(artifact.name)}
                onDiscuss={() => onDiscuss(artifact)}
              />
            ))}
          </ul>
        )
      ) : null}

      <ConfirmDialog
        open={condemned !== null}
        title={`Delete ${KIND_WORD[kind]} "${condemned ?? ""}"?`}
        message="Its source file and listing are removed for good, and the act is recorded as evidence. The agent can always write a new one."
        confirmLabel="Delete it"
        destructive
        confirming={remove.isPending}
        onConfirm={() => condemned !== null && remove.mutate(condemned)}
        onCancel={() => setCondemned(null)}
      />
    </Card>
  );
}

function ArtifactRow({
  artifact,
  busy,
  onAct,
  onDelete,
  onDiscuss,
}: {
  artifact: HarnessArtifact;
  busy: boolean;
  onAct: (verb: "activate" | "disable" | "promote" | "demote") => void;
  onDelete: () => void;
  onDiscuss: () => void;
}) {
  const active = artifact.status === "active";
  return (
    <li className="py-2">
      <div className="flex items-center gap-3">
        <span className="font-mono text-body text-label-1">{artifact.name}</span>
        <Badge tone={active ? "ok" : "neutral"} className="normal-case">
          {artifact.status}
        </Badge>
        {artifact.promoted === true ? (
          <Badge tone="ok" className="normal-case">
            promoted
          </Badge>
        ) : null}
        {artifact.last_error !== null ? (
          <Badge tone="danger" className="normal-case">
            broken
          </Badge>
        ) : null}
        <span
          className="min-w-0 flex-1 truncate text-caption text-label-3"
          title={artifact.descriptor}
        >
          {artifact.descriptor}
        </span>
        {active ? (
          <ActButton label="Disable" primary={false} busy={busy} onClick={() => onAct("disable")} />
        ) : (
          <ActButton
            label="Activate"
            primary={artifact.next_step === "activate"}
            busy={busy}
            disabled={artifact.last_error !== null}
            onClick={() => onAct("activate")}
          />
        )}
        {artifact.promoted === true ? (
          <ActButton label="Demote" primary={false} busy={busy} onClick={() => onAct("demote")} />
        ) : null}
        {artifact.next_step === "promote" ? (
          <ActButton label="Promote" primary busy={busy} onClick={() => onAct("promote")} />
        ) : null}
        <ActButton label="Discuss" primary={false} busy={busy} onClick={onDiscuss} />
        {!active ? (
          <Button
            variant="danger"
            className="h-7 shrink-0 px-2.5 text-caption"
            onClick={onDelete}
            disabled={busy}
          >
            Delete
          </Button>
        ) : null}
      </div>
      {artifact.last_error !== null ? (
        <p className="mt-1 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink">
          {artifact.last_error}
        </p>
      ) : null}
    </li>
  );
}

function ActButton({
  label,
  primary,
  busy,
  disabled = false,
  onClick,
}: {
  label: string;
  primary: boolean;
  busy: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <Button
      variant={primary ? undefined : "secondary"}
      className="h-7 shrink-0 px-2.5 text-caption"
      onClick={onClick}
      disabled={busy || disabled}
    >
      {label}
    </Button>
  );
}
