import type { InputHTMLAttributes, Ref } from "react";
import { cn } from "../lib/cn";

interface Props extends InputHTMLAttributes<HTMLInputElement> {
  ref?: Ref<HTMLInputElement>;
}

// The one text box. Focus is a border tint, not a ring, so dense toolbars
// stay calm; sizing overrides ride className.
export function Input({ className, ref, ...rest }: Props) {
  return (
    <input
      ref={ref}
      className={cn(
        "w-full min-w-0 rounded-md border border-line bg-surface px-2.5 py-1.5 text-body text-label-1 placeholder:text-label-3 focus:border-accent focus:outline-none disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...rest}
    />
  );
}
