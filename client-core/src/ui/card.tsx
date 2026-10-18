import type { HTMLAttributes } from "react";
import { cn } from "../lib/cn";

// The one surface container: every panel, shelf, and tile starts from this so
// "a card" means one thing across the app. Colors and radii come from the
// consuming app's semantic aliases, never a literal palette color.
export function Card({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border border-line bg-surface", className)} {...rest} />;
}
