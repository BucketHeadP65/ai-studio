import { describe, expect, it } from "vitest";

import { render } from "@/shared/test-support/render";

import { type PillTone, StatePill } from "./state-pill";

// A state is a shape plus a word. Every tone but queued carries an icon;
// queued carries the dashed border and the word alone. size changes the
// pill scale, never the tone's meaning.

const TONES_WITH_ICON: readonly PillTone[] = [
  "waiting",
  "running",
  "completed",
  "failed",
  "warning",
];

describe("StatePill", () => {
  it.each(TONES_WITH_ICON)("renders the word and an icon for the %s tone", (tone) => {
    const { container, getByText } = render(
      <StatePill tone={tone}>{`Word for ${tone}`}</StatePill>,
    );

    expect(getByText(`Word for ${tone}`)).toBeInTheDocument();
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("renders the queued tone's word with no icon", () => {
    const { container, getByText } = render(<StatePill tone="queued">Queued</StatePill>);

    expect(getByText("Queued")).toBeInTheDocument();
    expect(container.querySelector("svg")).toBeNull();
  });

  it("gives size md and size sm different padding", () => {
    const { container: mdContainer } = render(
      <StatePill tone="completed" size="md">
        Completed
      </StatePill>,
    );
    const { container: smContainer } = render(
      <StatePill tone="completed" size="sm">
        Completed
      </StatePill>,
    );

    const mdClass = mdContainer.querySelector("span")?.className ?? "";
    const smClass = smContainer.querySelector("span")?.className ?? "";

    expect(mdClass).not.toBe(smClass);
    expect(mdClass).toContain("py-1.75");
    expect(smClass).toContain("py-1");
  });

  it("defaults to size md when no size is given", () => {
    const { container } = render(<StatePill tone="completed">Completed</StatePill>);

    expect(container.querySelector("span")?.className ?? "").toContain("py-1.75");
  });

  it("merges a caller className alongside the tone classes", () => {
    const { container } = render(
      <StatePill tone="completed" className="ml-2">
        Completed
      </StatePill>,
    );

    expect(container.querySelector("span")?.className ?? "").toContain("ml-2");
  });
});

// "Never color alone" means every tone must be told apart by shape, not by
// hue. These tests read the rendered mark, an icon present or absent, a
// dashed or solid border, a striped or flat background, and the icon's own
// shape, never by comparing one class string between tones.

const ALL_TONES: readonly PillTone[] = [
  "waiting",
  "running",
  "completed",
  "failed",
  "queued",
  "warning",
];

interface ToneMark {
  hasIcon: boolean;
  iconShape: string;
  dashedBorder: boolean;
  stripedBackground: boolean;
}

function markOf(tone: PillTone): ToneMark {
  const { container } = render(<StatePill tone={tone}>{`Word for ${tone}`}</StatePill>);
  const pill = container.querySelector("span");
  const svg = container.querySelector("svg");
  const pillClass = pill?.className ?? "";
  return {
    hasIcon: svg !== null,
    iconShape: svg?.innerHTML ?? "",
    dashedBorder: pillClass.includes("border-dashed"),
    stripedBackground: pillClass.includes("repeating-linear-gradient"),
  };
}

describe("StatePill tone marks", () => {
  it("gives every tone a mark no other tone shares", () => {
    const marks = ALL_TONES.map((tone) => [tone, markOf(tone)] as const);

    for (let i = 0; i < marks.length; i += 1) {
      for (let j = i + 1; j < marks.length; j += 1) {
        const [toneA, markA] = marks[i];
        const [toneB, markB] = marks[j];
        expect(markA, `${toneA} should not look like ${toneB}`).not.toEqual(markB);
      }
    }
  });

  it("gives only the queued tone a dashed border and no icon", () => {
    for (const tone of ALL_TONES) {
      const mark = markOf(tone);
      expect(mark.hasIcon).toBe(tone !== "queued");
      expect(mark.dashedBorder).toBe(tone === "queued");
    }
  });

  it("gives only the failed tone a striped background", () => {
    for (const tone of ALL_TONES) {
      expect(markOf(tone).stripedBackground).toBe(tone === "failed");
    }
  });
});
