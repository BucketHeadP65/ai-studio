// Module-scope formatter — `new Intl.RelativeTimeFormat(...)` is cheap but the
// sidebar can call this once per row per re-render. Hoisting saves the
// allocation and the locale lookup.
const RTF = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

// Coarse relative-time formatter. Falls back to a locale date string past one
// week — beyond that "5 days ago" stops being a useful anchor and the absolute
// date is more informative.
export function formatRelative(iso: string, now: number = Date.now()): string {
  const date = new Date(iso);
  const diffMs = now - date.getTime();
  const diffMinutes = Math.floor(diffMs / 60_000);
  const diffHours = Math.floor(diffMinutes / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffMinutes < 1) return "just now";
  if (diffMinutes < 60) return RTF.format(-diffMinutes, "minute");
  if (diffHours < 24) return RTF.format(-diffHours, "hour");
  if (diffDays < 7) return RTF.format(-diffDays, "day");
  return date.toLocaleDateString();
}
