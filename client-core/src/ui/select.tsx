import type { Ref, SelectHTMLAttributes } from "react";
import { cn } from "../lib/cn";

interface Props extends SelectHTMLAttributes<HTMLSelectElement> {
  ref?: Ref<HTMLSelectElement>;
}

// The one dropdown, native on purpose: the platform picker beats a rebuilt
// listbox for keyboard and mobile, and the box matches Input.
export function Select({ className, ref, ...rest }: Props) {
  return (
    <select
      ref={ref}
      className={cn(
        "rounded-md border border-line bg-surface px-2 py-1.5 text-caption text-label-2 focus:border-accent focus:outline-none disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...rest}
    />
  );
}
