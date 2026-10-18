// The words the inspection screens share: how a source, a name, a date, a span or a line reads to a person.
// Pure functions, no React, so every screen says the same thing the same way.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const SHORT_ID = /^(.*)-([0-9a-f]{6,})$/;
// A run id the transcript derives: `sp-`, then unpadded base64url of
// `<byte length>:<agent><run id>`.
const DERIVED_ID = /^sp-([A-Za-z0-9_-]+)$/;
const LENGTH_PREFIX = /^(\d+):/;
// How many of the run id's last hex digits a derived id shows. A uuid7 starts with its timestamp,
// so its first digits repeat for runs a minute apart.
const SHORT_DIGITS = 8;
// A coordinate written `step.message.part`, as it appears inside a blame's own words.
export const SPAN_TOKEN = /\b\d+\.\d+\.\d+\b/g;

// A source's own locator, the scheme dropped: `record:<run id>` reads as `<run id>`.
export function runLocator(source: string): string {
  const at = source.indexOf(":");
  return at === -1 ? source : source.slice(at + 1);
}

// A run id split into its agent and a short id: `<agent>-<hex>` as the record once wrote them,
// or an `sp-` id the transcript derives. Anything else is all agent.
export function runName(locator: string): { agent: string; short: string } {
  const derived = derivedRunName(locator);
  if (derived !== null) return derived;
  const match = SHORT_ID.exec(locator);
  return match?.[1] !== undefined && match[2] !== undefined ? { agent: match[1], short: match[2] } : { agent: locator, short: "" };
}

// The agent and the run id's last hex digits inside an `sp-` id, or null when it does not decode.
function derivedRunName(locator: string): { agent: string; short: string } | null {
  const encoded = DERIVED_ID.exec(locator)?.[1];
  if (encoded === undefined) return null;
  const base64 = encoded.replaceAll("-", "+").replaceAll("_", "/");
  let bytes: Uint8Array;
  try {
    bytes = Uint8Array.from(
      atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, "=")),
      (character) => character.charCodeAt(0),
    );
  } catch {
    return null;
  }
  const decoder = new TextDecoder();
  const prefix = LENGTH_PREFIX.exec(decoder.decode(bytes));
  if (prefix?.[1] === undefined) return null;
  const start = prefix[0].length;
  const end = start + Number(prefix[1]);
  if (end > bytes.length) return null;
  const run = decoder.decode(bytes.slice(end)).replaceAll("-", "");
  return { agent: decoder.decode(bytes.slice(start, end)), short: run.slice(-SHORT_DIGITS) };
}

export function spaced(name: string): string {
  return name.replaceAll("_", " ");
}

export function sentence(words: string): string {
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function surfaceLabel(kind: string): string {
  return spaced(kind);
}

// `1 message`, `12 messages` -- a count and the word for it, singular at exactly one.
export function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function two(value: number): string {
  return String(value).padStart(2, "0");
}

// `10 Sep 12:17`, in the reader's own time zone.
export function shortDate(iso: string): string {
  const date = new Date(iso);
  return `${date.getDate()} ${MONTHS[date.getMonth()]} ${two(date.getHours())}:${two(date.getMinutes())}`;
}

// `10 Sep 2026 12:17`.
export function longDate(iso: string): string {
  const date = new Date(iso);
  return `${date.getDate()} ${MONTHS[date.getMonth()]} ${date.getFullYear()} ${two(date.getHours())}:${two(date.getMinutes())}`;
}

// How long something took, in its largest two units: `1 h 3 min`, `4 min 12 s`, `48 s`. Empty when it has not ended.
export function duration(startIso: string, endIso: string | null): string {
  if (endIso === null) return "";
  const seconds = Math.max(0, Math.round((new Date(endIso).getTime() - new Date(startIso).getTime()) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours > 0) return minutes > 0 ? `${hours} h ${minutes} min` : `${hours} h`;
  if (minutes > 0) return rest > 0 ? `${minutes} min ${rest} s` : `${minutes} min`;
  return `${rest} s`;
}

// The spans a blame's why names, in the order the why names them, kept to the spans the finding has. A why
// that names none of them cites every span.
export function citesOf(why: string, spans: string[]): string[] {
  const named = [...why.matchAll(SPAN_TOKEN)].map((match) => match[0]);
  const cited = named.filter((token, index) => spans.includes(token) && named.indexOf(token) === index);
  return cited.length === 0 ? spans : cited;
}

// A span's own step, the first dot-separated number in `step.message.part`. Never `NaN`: a span
// with no leading number reads as step 0.
export function spanStep(span: string): number {
  const value = Number(span.split(".")[0]);
  return Number.isNaN(value) ? 0 : value;
}

// The words after `step N,` in a part's own label -- what a reader needs when a header has no
// fixed phrase of its own for the kind.
export function partWords(label: string): string {
  const at = label.indexOf(", ");
  return at === -1 ? label : label.slice(at + 2);
}

// The line a card shows for a span: the first line quoting a phrase a blame's why quotes (case-insensitive),
// else the first line with text on it.
export function keyLine(text: string, phrases: string[]): string {
  const lines = text.split("\n");
  const lower = phrases.map((phrase) => phrase.toLowerCase());
  const quoted = lines.find((line) => {
    const candidate = line.toLowerCase();
    return lower.some((phrase) => candidate.includes(phrase));
  });
  return quoted ?? lines.find((line) => line.trim() !== "") ?? "";
}
