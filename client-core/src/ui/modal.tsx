"use client";

import { Dialog } from "radix-ui";
import type { ReactNode } from "react";
import { useRef } from "react";
import { cn } from "../lib/cn";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  /** Panel classes; merged over the default width and surface, so `max-w-lg` widens it. */
  className?: string;
  /** Backdrop classes; merged over the default dim. */
  overlayClassName?: string;
}

// A centered modal on the radix Dialog: focus is trapped inside, the page behind
// stops scrolling and is hidden from assistive tech, focus returns to the opener
// on close, and Esc or a pointer on the backdrop close via onClose. The panel is
// the dialog surface itself, so a backdrop press always lands outside it. Place a
// `ModalTitle` anywhere inside; it labels the dialog, and a `ModalDescription`
// describes it (radix wires `aria-describedby` to it automatically).
export function Modal({ open, onClose, children, className, overlayClassName }: ModalProps) {
  // No `Dialog.Trigger` is used here (every open is controlled), so radix has no opener to
  // return focus to on close. Capture it ourselves while it is still the active element.
  const openerRef = useRef<HTMLElement | null>(null);

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className={cn("fixed inset-0 z-50 bg-black/55", overlayClassName)} />
        <Dialog.Content
          onOpenAutoFocus={() => {
            openerRef.current =
              document.activeElement instanceof HTMLElement ? document.activeElement : null;
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            openerRef.current?.focus();
          }}
          className={cn(
            "fixed top-1/2 left-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg bg-surface p-5 shadow-lg ring-1 ring-line",
            className,
          )}
        >
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function ModalTitle({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <Dialog.Title className={cn("font-semibold text-label-1 text-sm", className)}>
      {children}
    </Dialog.Title>
  );
}

export function ModalDescription({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <Dialog.Description className={cn("mt-2 text-label-2 text-sm", className)}>
      {children}
    </Dialog.Description>
  );
}
