import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { render } from "@/test-support/render";

import { type GlyphFill, type GlyphKind, NodeGlyph } from "./node-glyph";

const KINDS: GlyphKind[] = ["step", "person-gate", "agent-gate", "auto-gate"];
const FILLS: GlyphFill[] = ["ran", "not-run", "running", "waiting", "failed"];

describe("NodeGlyph", () => {
  it.each(KINDS.flatMap((kind) => FILLS.map((fill) => [kind, fill] as const)))(
    "renders %s at fill %s as one svg carrying both as data attributes",
    (kind, fill) => {
      const { container } = render(<NodeGlyph kind={kind} fill={fill} />);
      const svg = container.querySelector("svg");
      expect(svg).not.toBeNull();
      expect(svg?.getAttribute("data-glyph-kind")).toBe(kind);
      expect(svg?.getAttribute("data-glyph-fill")).toBe(fill);
    },
  );

  it("defaults to the ran fill", () => {
    const { container } = render(<NodeGlyph kind="step" />);
    expect(container.querySelector("svg")?.getAttribute("data-glyph-fill")).toBe("ran");
  });

  it("is aria-hidden with no label, since a sibling word carries the meaning", () => {
    const { container } = render(<NodeGlyph kind="step" />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("aria-hidden");
    expect(svg).not.toHaveAttribute("role");
  });

  it("becomes a labeled image once it stands alone", () => {
    const { container } = render(<NodeGlyph kind="person-gate" label="Person gate, waiting" />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("role", "img");
    expect(svg).toHaveAttribute("aria-label", "Person gate, waiting");
    expect(svg).not.toHaveAttribute("aria-hidden");
  });

  it("scales a square glyph to the requested size", () => {
    const { container } = render(<NodeGlyph kind="step" size={24} />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("width", "24");
    expect(svg).toHaveAttribute("height", "24");
  });

  it("keeps the hexagon's natural wider-than-tall proportion at any size", () => {
    const { container } = render(<NodeGlyph kind="agent-gate" size={28} />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("width", "28");
    expect(svg).toHaveAttribute("height", "25"); // 28 * 38 / 42, rounded
  });

  it("draws a step as a circle and a person gate as a rotated square, never the other's shape", () => {
    const { container: stepBox } = render(<NodeGlyph kind="step" fill="not-run" />);
    expect(stepBox.querySelector("circle")).not.toBeNull();
    expect(stepBox.querySelector("rect")).toBeNull();

    const { container: gateBox } = render(<NodeGlyph kind="person-gate" fill="not-run" />);
    expect(gateBox.querySelector('rect[transform*="rotate(45"]')).not.toBeNull();
    expect(gateBox.querySelector("circle")).toBeNull();
  });

  it("draws an agent gate as a hexagon that stays ink filled once it has run", () => {
    const { container } = render(<NodeGlyph kind="agent-gate" fill="ran" />);
    const hex = container.querySelector("polygon");
    expect(hex).not.toBeNull();
    expect(hex?.getAttribute("class")).toMatch(/fill-label-1/);
  });

  it("hollows the agent gate's hexagon out to a dashed outline before the run reaches it", () => {
    const { container } = render(<NodeGlyph kind="agent-gate" fill="not-run" />);
    const hex = container.querySelector("polygon");
    expect(hex?.getAttribute("fill")).toBe("none");
    expect(hex?.getAttribute("stroke-dasharray")).toBeTruthy();
  });

  it("gives the automatic gate the same solid ink diamond frame whether reached or not", () => {
    const { container: notRun } = render(<NodeGlyph kind="auto-gate" fill="not-run" />);
    const { container: ran } = render(<NodeGlyph kind="auto-gate" fill="ran" />);
    const frame = (c: HTMLElement) => c.querySelector('rect[transform*="rotate(45"]');
    expect(frame(notRun)?.getAttribute("class")).toBe(frame(ran)?.getAttribute("class"));
  });

  it("breathes only while running or waiting on a person, never once resolved", () => {
    const pulseClass = /motion-safe:animate-pulse/;
    const classOf = (el: ReactElement) =>
      render(el).container.querySelector("svg")?.getAttribute("class");
    expect(classOf(<NodeGlyph kind="step" fill="running" />)).toMatch(pulseClass);
    expect(classOf(<NodeGlyph kind="person-gate" fill="waiting" />)).toMatch(pulseClass);
    expect(classOf(<NodeGlyph kind="step" fill="ran" />)).not.toMatch(pulseClass);
    expect(classOf(<NodeGlyph kind="step" fill="not-run" />)).not.toMatch(pulseClass);
  });

  it("never stripes a glyph for failed, a halo ring instead, striping stays a pill-only treatment", () => {
    const { container } = render(<NodeGlyph kind="step" fill="failed" />);
    const svg = container.querySelector("svg");
    expect(svg?.innerHTML).not.toContain("repeating-linear-gradient");
    const circles = container.querySelectorAll("circle");
    expect(circles.length).toBeGreaterThanOrEqual(2); // the halo circle behind the solid one
  });

  it("passes a caller's className through to the root svg", () => {
    const { container } = render(<NodeGlyph kind="step" className="mystery-class" />);
    expect(container.querySelector("svg")?.getAttribute("class")).toContain("mystery-class");
  });
});
