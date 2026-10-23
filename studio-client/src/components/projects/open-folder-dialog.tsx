"use client";

import { useMutation } from "@tanstack/react-query";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Modal, ModalTitle } from "@/shared/ui/modal";
import { useEffect, useId, useState } from "react";

import { type Project, projectsApi } from "@/api/projects";
import { studioErrorMessage } from "@/api/studio";

interface Props {
  open: boolean;
  onClose: () => void;
  onOpened: (project: Project) => void;
}

// Registers a project by an absolute filesystem path, typed by hand -- there
// is no picker, the caller may be pointing at a path on a remote box. The
// backend refuses a path that is not a directory with a 400; that refusal
// renders inline in the dialog, never as a toast, and never closes it.
export function OpenFolderDialog({ open, onClose, onOpened }: Props) {
  const pathId = useId();
  const [path, setPath] = useState("");

  const register = useMutation({
    mutationFn: (rootPath: string) => projectsApi.register(rootPath),
    onSuccess: (project) => {
      setPath("");
      onOpened(project);
    },
  });

  // A fresh form every time the dialog opens: the component instance persists
  // across opens, so a discarded attempt's path or error would otherwise linger
  // into the next one.
  // biome-ignore lint/correctness/useExhaustiveDependencies: register.reset is re-created every render; only `open` should retrigger this.
  useEffect(() => {
    if (!open) return;
    setPath("");
    register.reset();
  }, [open]);

  function submit() {
    const trimmed = path.trim();
    if (trimmed === "") return;
    register.mutate(trimmed);
  }

  return (
    <Modal open={open} onClose={onClose}>
      <ModalTitle>Open folder</ModalTitle>
      <label htmlFor={pathId} className="mt-3 flex flex-col gap-1 text-caption text-label-2">
        Absolute path
        <Input
          id={pathId}
          value={path}
          onChange={(event) => setPath(event.target.value)}
          placeholder="/Users/you/projects/example"
          disabled={register.isPending}
          autoFocus
        />
      </label>

      {register.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {studioErrorMessage(register.error)}
        </p>
      ) : null}

      <div className="mt-4 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose} disabled={register.isPending}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={register.isPending || path.trim() === ""}>
          {register.isPending ? "Opening…" : "Open"}
        </Button>
      </div>
    </Modal>
  );
}
