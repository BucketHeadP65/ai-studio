"use client";

import { cn } from "@/shared/lib/cn";
import { ChevronDown } from "lucide-react";
import type { ReactElement, ReactNode } from "react";
import { useState } from "react";

// A card whose body folds away behind its title. The chevron and the title are one button
// (`aria-expanded` says which way it stands); anything interactive in the header rides `aside`,
// outside that button, so a chip or a pill never nests one control inside another.

export interface CollapsibleCardProps {
  title: string;
  aside?: ReactNode;
  defaultOpen?: boolean;
  id?: string;
  className?: string;
  bodyClassName?: string;
  children: ReactNode;
}

export function CollapsibleCard({
  title,
  aside,
  defaultOpen = true,
  id,
  className,
  bodyClassName,
  children,
}: CollapsibleCardProps): ReactElement {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section
      id={id}
      className={cn(
        "flex shrink-0 flex-col overflow-hidden rounded-lg border border-line bg-surface shadow-sm",
        className,
      )}
    >
      <div
        className={cn(
          "flex flex-wrap items-center gap-3 px-5 py-3",
          open && "border-line border-b",
        )}
      >
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className="flex min-w-0 items-center gap-2 text-left"
        >
          <ChevronDown
            aria-hidden
            size={16}
            className={cn("shrink-0 text-label-3 transition-transform", !open && "-rotate-90")}
          />
          <h2 className="font-semibold text-label-1 text-subtitle">{title}</h2>
        </button>
        {aside !== undefined ? (
          <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-2">
            {aside}
          </div>
        ) : null}
      </div>
      {open ? <div className={bodyClassName}>{children}</div> : null}
    </section>
  );
}
