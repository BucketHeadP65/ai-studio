import { type ClassValue, clsx } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// The consuming app's named type-scale steps. Registered as font sizes so a
// merge treats `text-caption` as a size, not as a text color: without this, a
// caller's `text-caption` silently discards a primitive's `text-surface` and
// the text vanishes into its own background.
const TYPE_SCALE = ["micro", "caption", "body", "subtitle", "title"];

const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [{ text: TYPE_SCALE }],
    },
  },
});

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
