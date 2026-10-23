import type { Ref, TextareaHTMLAttributes } from "react";
import { cn } from "../lib/cn";

interface Props extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  ref?: Ref<HTMLTextAreaElement>;
}

// The multi-line sibling of Input, same box, no resize handle by default —
// composers own their height.
export function Textarea({ className, ref, ...rest }: Props) {
  return (
    <textarea
      ref={ref}
      className={cn(
        "w-full min-w-0 resize-none rounded-md border border-line bg-surface px-2.5 py-1.5 text-body text-label-1 placeholder:text-label-3 focus:border-accent focus:outline-none disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...rest}
    />
  );
}
