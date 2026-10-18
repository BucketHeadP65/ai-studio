"use client";

import { Dialog } from "radix-ui";
import type { ReactNode } from "react";
import { useRef } from "react";
import { cn } from "../lib/cn";
import { Button } from "./button";

interface Props {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
  /**
   * A modal drawer (the default) dims and locks the page behind it. A non-modal one
   * leaves the page live: a pointer outside still closes the drawer, and then lands
   * where it was aimed.
   */
  modal?: boolean;
}

// A right-side overlay panel for work summoned from a row: open it, act, dismiss
// it, and the page underneath has not moved. Built on the radix Dialog: while
// modal, focus is trapped inside, the page behind stops scrolling, and focus
// returns to the opener on close. Esc and any pointer outside the panel close;
// the body slot owns its own scrolling.
export function Drawer({ open, title, onClose, children, className, modal = true }: Props) {
  // No `Dialog.Trigger` is used here (every open is controlled), so radix has no opener to
  // return focus to on close. Capture it ourselves while it is still the active element.
  const openerRef = useRef<HTMLElement | null>(null);

  return (
    <Dialog.Root
      open={open}
      modal={modal}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/40" />
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
            "fixed inset-y-0 right-0 z-50 flex w-[min(92vw,540px)] flex-col border-line border-l bg-surface shadow-lg",
            className,
          )}
        >
          <div className="flex items-center justify-between gap-2 border-line border-b px-4 py-2.5">
            <Dialog.Title className="font-semibold text-label-1 text-sm">{title}</Dialog.Title>
            <Button
              variant="secondary"
              className="h-6 px-2 text-xs"
              onClick={onClose}
              aria-label="Close drawer"
            >
              Close
            </Button>
          </div>
          <div className="min-h-0 flex-1">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
