import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "../lib/cn";

interface Props extends HTMLAttributes<HTMLElement> {
  title: string;
  count?: number;
  actions?: ReactNode;
}

// The label above a shelf or panel section: quiet uppercase, an optional
// count, and a right-aligned slot for the section's own controls.
export function SectionHeader({ title, count, actions, className, ...rest }: Props) {
  return (
    <header className={cn("flex items-center justify-between gap-2", className)} {...rest}>
      <h2 className="font-medium text-caption text-label-3 uppercase tracking-wider">
        {title}
        {count !== undefined ? <span className="normal-case"> ({count})</span> : null}
      </h2>
      {actions}
    </header>
  );
}
