import type { ReactNode } from "react";
import { cn } from "../lib/cn";

interface Props {
  icon?: ReactNode;
  title: string;
  hint?: string;
  children?: ReactNode;
  className?: string;
}

// What an empty shelf says instead of nothing: what this place is for, and,
// through `children`, the first action that would fill it.
export function EmptyState({ icon, title, hint, children, className }: Props) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 px-4 py-8 text-center",
        className,
      )}
    >
      {icon ? (
        <span aria-hidden className="text-label-3">
          {icon}
        </span>
      ) : null}
      <p className="font-medium text-body text-label-2">{title}</p>
      {hint ? <p className="max-w-sm text-caption text-label-3 leading-relaxed">{hint}</p> : null}
      {children}
    </div>
  );
}
