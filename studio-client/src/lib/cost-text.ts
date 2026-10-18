// A priced cost, read off the wire as a decimal string, in words: a bare "$0" for nothing spent,
// four decimals under a dollar so a cheap call still reads as more than zero, two decimals at or
// above one. A reading that is not a plain non-negative decimal is not a cost, and reads as none.
//
// The rounding works on the string's own digits rather than on a float, because the wire carries
// money as a decimal string precisely so nothing is lost on the way here. A figure landing exactly
// on a tie rounds up, which a binary float cannot promise: 1.005 as a float is a shade under the
// tie and would round down to 1.00.

const DECIMAL = /^(\d+)(?:\.(\d*))?$/;
const SCIENTIFIC = /^(\d+)(?:\.(\d*))?[eE]([+-]?\d+)$/;
const UNDER_A_DOLLAR = 4;
const A_DOLLAR_OR_MORE = 2;

// A decimal string with its point moved by the exponent, digit by digit. Python's Decimal prints
// small sums in scientific notation ("7.5E-8" for a few hundred tokens on a cheap model), and that
// is a real price rather than a malformed one, so it has to reach the reader as one. The shift is
// done on the digits and never through a float, for the same reason the rounding below is.
function expandScientific(costUsd: string): string | null {
  const read = SCIENTIFIC.exec(costUsd);
  if (read === null) return null;
  const digits = read[1] + (read[2] ?? "");
  const point = read[1].length + Number(read[3]);
  if (point <= 0) return `0.${"0".repeat(-point)}${digits}`;
  if (point >= digits.length) return digits + "0".repeat(point - digits.length);
  return `${digits.slice(0, point)}.${digits.slice(point)}`;
}

/** `digits` rounded to `places`, carrying a rounded-up tie leftwards, as `whole.fraction`. */
function roundDigits(whole: string, fraction: string, places: number): string {
  const kept = fraction.slice(0, places).padEnd(places, "0");
  if (fraction.length <= places || fraction[places] < "5") return `${whole}.${kept}`;
  const carried = (whole + kept).split("");
  let at = carried.length - 1;
  while (at >= 0 && carried[at] === "9") {
    carried[at] = "0";
    at -= 1;
  }
  if (at < 0) carried.unshift("1");
  else carried[at] = String(Number(carried[at]) + 1);
  const raised = carried.join("");
  const point = raised.length - places;
  return `${raised.slice(0, point)}.${raised.slice(point)}`;
}

export function costText(costUsd: string): string {
  const trimmed = costUsd.trim();
  const read = DECIMAL.exec(trimmed) ?? DECIMAL.exec(expandScientific(trimmed) ?? "");
  if (read === null) return "$0";
  const whole = read[1].replace(/^0+(?=\d)/, "");
  const fraction = read[2] ?? "";
  if (/^0*$/.test(whole + fraction)) return "$0";
  const places = whole === "0" ? UNDER_A_DOLLAR : A_DOLLAR_OR_MORE;
  return `$${roundDigits(whole, fraction, places)}`;
}

// A cost beside however many responses nothing could price. A figure nothing could price is
// UNKNOWN, never zero: a run reading "$0" when the model that ran it has no published price says
// the work was free, which is the one thing it certainly was not. Where some responses priced and
// others did not, the figure that is known is still worth saying, as the floor it actually is.
export function spendText(costUsd: string, unpriced: number): string {
  if (unpriced <= 0) return costText(costUsd);
  const known = costText(costUsd);
  return known === "$0" ? "unknown" : `at least ${known}`;
}
