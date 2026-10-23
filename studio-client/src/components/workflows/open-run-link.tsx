"use client";

import { cn } from "@/shared/lib/cn";
import Link from "next/link";
import type { ReactElement } from "react";

// The Secondary-styled link to a run's own page, reused everywhere the chat station points at
// one: a turn's own info box (the compact `sm` size) and the session rail's live card (the
// larger `md` size). `className` carries only layout concerns from the caller (e.g. pinning it
// to the far edge of a row).
export function OpenRunLink({
  href,
  size = "sm",
  className,
}: {
  href: string;
  size?: "sm" | "md";
  className?: string;
}): ReactElement {
  return (
    <Link
      href={href}
      className={cn(
        "inline-flex items-center rounded-full border border-line-strong bg-surface text-caption text-label-2 hover:bg-well",
        size === "md" ? "h-8 px-3.5" : "h-7 px-3",
        className,
      )}
    >
      Open the run
    </Link>
  );
}
