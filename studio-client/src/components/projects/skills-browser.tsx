"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { type SkillCard, projectKeys, projectsApi } from "@/api/projects";
import { studioErrorMessage } from "@/api/studio";
import { cn } from "@/shared/lib/cn";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import { EmptyState } from "@/shared/ui/empty-state";
import { Input } from "@/shared/ui/input";
import { Modal, ModalTitle } from "@/shared/ui/modal";
import { Select } from "@/shared/ui/select";

export interface SkillsBrowserProps {
  /** The project whose skills these are; the browser never reads any other project. */
  projectId: string;
  /** Fired with the row's own skill card, so the host can tell a builtin one from an editable one. */
  onOpen: (skill: SkillCard) => void;
}

const BUILTIN_LIBRARY = "builtin";

// The project's skills, one full-width row per SKILL.md the backend found. A row is exactly one
// of two states: valid or broken (a broken card still opens -- opening it is how the author fixes
// it). Every row opens, builtin included: a builtin skill's file lives outside the project root,
// so the host reads and shows its full body read-only rather than through the file-editing path.
export function SkillsBrowser({ projectId, onOpen }: SkillsBrowserProps) {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);

  const listKey = projectKeys.skills(projectId);
  const listing = useQuery({
    queryKey: listKey,
    queryFn: () => projectsApi.skills(projectId),
  });

  const skills = listing.data ?? [];
  const libraries = Array.from(
    new Set(
      skills.filter((skill) => skill.library !== BUILTIN_LIBRARY).map((skill) => skill.library),
    ),
  );

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="font-medium text-label-1 text-sm">Skills</h2>
        {libraries.length > 0 ? (
          <Button onClick={() => setDialogOpen(true)}>New Skill</Button>
        ) : null}
      </div>

      {listing.isError ? (
        <p
          role="alert"
          className="rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {studioErrorMessage(listing.error)}
        </p>
      ) : null}

      {listing.data && skills.length === 0 ? (
        <EmptyState
          icon={<Sparkles size={18} strokeWidth={1.5} />}
          title="No skill libraries"
          hint="This project has none configured yet. Add one in .aifactory/config.json, then its skills show up here."
        />
      ) : null}

      {skills.length > 0 ? (
        <div className="flex flex-col gap-2">
          {skills.map((skill) => (
            <SkillRow key={skill.path} skill={skill} onOpen={onOpen} />
          ))}
        </div>
      ) : null}

      <NewSkillDialog
        open={dialogOpen}
        libraries={libraries}
        projectId={projectId}
        onClose={() => setDialogOpen(false)}
        onCreated={(skill) => {
          queryClient.invalidateQueries({ queryKey: listKey });
          setDialogOpen(false);
          onOpen(skill);
        }}
      />
    </div>
  );
}

function SkillRow({
  skill,
  onOpen,
}: {
  skill: SkillCard;
  onOpen: (skill: SkillCard) => void;
}) {
  const builtin = skill.library === BUILTIN_LIBRARY;
  const state = builtin ? "builtin" : skill.valid ? "valid" : "broken";

  return (
    <button
      type="button"
      data-testid={`skill-card-${skill.name}`}
      data-skill-state={state}
      aria-label={`Open ${skill.name}`}
      onClick={() => onOpen(skill)}
      className={cn(
        "flex w-full flex-col gap-1 rounded-md border border-line p-3 text-left transition-colors hover:bg-well",
        state === "broken" && "opacity-70",
      )}
    >
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-medium text-label-1 text-sm">
          {skill.name}
        </span>
        <Badge className="shrink-0">{skill.library}</Badge>
      </div>
      {state === "broken" ? (
        <p className="rounded-md bg-danger-soft px-2 py-1 text-caption text-danger-ink">
          {skill.error}
        </p>
      ) : (
        <p className="line-clamp-2 text-caption text-label-2">{skill.description}</p>
      )}
    </button>
  );
}

function NewSkillDialog({
  open,
  libraries,
  projectId,
  onClose,
  onCreated,
}: {
  open: boolean;
  libraries: string[];
  projectId: string;
  onClose: () => void;
  onCreated: (skill: SkillCard) => void;
}) {
  const libraryId = useId();
  const nameId = useId();
  const [library, setLibrary] = useState(libraries[0] ?? "");
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: (): Promise<string> =>
      projectsApi.installSkill(projectId, library || (libraries[0] ?? ""), name.trim()),
    onSuccess: (path) => {
      const createdLibrary = library || libraries[0] || "";
      const createdName = name.trim();
      setName("");
      onCreated({
        name: createdName,
        description: "",
        library: createdLibrary,
        path,
        valid: true,
        error: null,
      });
    },
  });

  // A fresh form every time the dialog opens: the component instance persists
  // across opens, so a discarded attempt's name, library choice, or error would
  // otherwise linger into the next one.
  // biome-ignore lint/correctness/useExhaustiveDependencies: create.reset is re-created every render; only `open` should retrigger this.
  useEffect(() => {
    if (!open) return;
    setName("");
    setLibrary(libraries[0] ?? "");
    create.reset();
  }, [open]);

  const effectiveLibrary = library || libraries[0] || "";

  return (
    <Modal open={open} onClose={onClose}>
      <ModalTitle>New Skill</ModalTitle>

      <label htmlFor={libraryId} className="mt-3 flex flex-col gap-1 text-caption text-label-2">
        Library
        <Select
          id={libraryId}
          value={effectiveLibrary}
          onChange={(event) => setLibrary(event.target.value)}
          disabled={create.isPending}
        >
          {libraries.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </Select>
      </label>

      <label htmlFor={nameId} className="mt-3 flex flex-col gap-1 text-caption text-label-2">
        Name
        <Input
          id={nameId}
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="my-new-skill"
          disabled={create.isPending}
          autoFocus
        />
      </label>

      {create.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {studioErrorMessage(create.error)}
        </p>
      ) : null}

      <div className="mt-4 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose} disabled={create.isPending}>
          Cancel
        </Button>
        <Button
          onClick={() => create.mutate()}
          disabled={create.isPending || name.trim() === "" || effectiveLibrary === ""}
        >
          {create.isPending ? "Creating…" : "Create"}
        </Button>
      </div>
    </Modal>
  );
}
