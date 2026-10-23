"use client";

import { Button } from "./button";
import { Modal, ModalDescription, ModalTitle } from "./modal";

interface Props {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  confirming?: boolean;
  destructive?: boolean;
  /** A refusal to show inside the dialog; the dialog stays open so the caller can retry. */
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

// Minimal confirm modal: Esc and a pointer on the backdrop close via onCancel.
// Initial focus lands on Cancel so a stray Enter does not confirm a destructive
// action by accident.
export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  confirming = false,
  destructive = false,
  error,
  onConfirm,
  onCancel,
}: Props) {
  return (
    <Modal open={open} onClose={onCancel}>
      <ModalTitle>{title}</ModalTitle>
      <ModalDescription>{message}</ModalDescription>
      {error ? (
        <p role="alert" className="mt-3 text-caption text-danger-ink">
          {error}
        </p>
      ) : null}
      <div className="mt-4 flex justify-end gap-2">
        <Button variant="secondary" onClick={onCancel} disabled={confirming}>
          {cancelLabel}
        </Button>
        <Button
          variant={destructive ? "danger" : "primary"}
          onClick={onConfirm}
          disabled={confirming}
        >
          {confirming ? "Working…" : confirmLabel}
        </Button>
      </div>
    </Modal>
  );
}
