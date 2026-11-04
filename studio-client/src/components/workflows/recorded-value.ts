// A run's recorded values arrive keyed by node id, each rendered as text, and a result field names
// one as `values.<node>` or `values.<node>.<field>`. The node id is the longest leading part of the
// path that is a key, since a turn's own value sits under `<node>.<turn>`. The rest of the path
// walks the value read as JSON. A path that reaches nothing says so instead of showing a guess.
export const NOT_RECORDED = "not recorded";

export function recordedValue(
  values: Record<string, string> | null | undefined,
  path: string,
): string {
  if (!values) return NOT_RECORDED;
  const segments = path.split(".");
  const start = segments[0] === "values" ? 1 : 0;
  for (let end = segments.length; end > start; end -= 1) {
    const key = segments.slice(start, end).join(".");
    const text = values[key];
    if (text === undefined) continue;
    const rest = segments.slice(end);
    return rest.length === 0 ? text : walked(text, rest);
  }
  return NOT_RECORDED;
}

function walked(text: string, rest: string[]): string {
  let current: unknown;
  try {
    current = JSON.parse(text);
  } catch {
    return NOT_RECORDED;
  }
  for (const segment of rest) {
    if (current === null || typeof current !== "object") return NOT_RECORDED;
    const entry = Object.entries(current).find(([name]) => name === segment);
    if (entry === undefined) return NOT_RECORDED;
    current = entry[1];
  }
  return typeof current === "string" ? current : JSON.stringify(current, null, 2);
}
