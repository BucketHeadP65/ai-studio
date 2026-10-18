// Makes a tool-return string readable for the markdown viewer. A spilled or
// serialized return embeds JSON string escapes as literal backslash pairs
// (\n, \", \t), which render as noise; when those are present the common
// escapes are decoded for display. Text without them comes back unchanged,
// and unknown escapes are left as written rather than guessed at.
export function readableToolText(text: string): string {
  if (!text.includes("\\n") && !text.includes('\\"')) {
    return text;
  }
  let out = "";
  let i = 0;
  const decoded: Record<string, string> = { n: "\n", t: "\t", r: "\r", '"': '"', "\\": "\\" };
  while (i < text.length) {
    const char = text[i];
    const next = text[i + 1];
    if (char === "\\" && next !== undefined && decoded[next] !== undefined) {
      out += decoded[next];
      i += 2;
      continue;
    }
    out += char;
    i += 1;
  }
  return out;
}
