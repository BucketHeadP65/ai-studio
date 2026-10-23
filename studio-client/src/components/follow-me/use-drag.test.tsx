import { fireEvent, screen } from "@testing-library/dom";
import { type ReactElement, useState } from "react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { render } from "@/shared/test-support/render";

import { useDrag } from "./use-drag";

// The one pointer gesture every companion grip is built on: press, follow, release. jsdom builds
// PointerEvent but never implements capture, so the two capture calls are stubbed here and
// asserted the same way a browser would honour them.

beforeAll(() => {
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
});

interface Move {
  dx: number;
  dy: number;
}

function Probe({ onStart, onEnd }: { onStart?: () => void; onEnd?: () => void }): ReactElement {
  const [moves, setMoves] = useState<Move[]>([]);
  const drag = useDrag({
    onStart,
    onMove: (dx, dy) => setMoves((seen) => [...seen, { dx, dy }]),
    onEnd,
  });
  return (
    <div>
      <div data-testid="grip" {...drag.handlers} />
      <span data-testid="dragging">{drag.dragging ? "yes" : "no"}</span>
      <span data-testid="moves">{JSON.stringify(moves)}</span>
    </div>
  );
}

function grip(): HTMLElement {
  return screen.getByTestId("grip");
}

function moves(): Move[] {
  const text = screen.getByTestId("moves").textContent ?? "[]";
  const parsed: unknown = JSON.parse(text);
  return Array.isArray(parsed) ? (parsed as Move[]) : [];
}

describe("useDrag", () => {
  it("reports every move as a delta from where the press landed", () => {
    render(<Probe />);

    fireEvent.pointerDown(grip(), { pointerId: 1, clientX: 100, clientY: 50 });
    expect(screen.getByTestId("dragging")).toHaveTextContent("yes");

    fireEvent.pointerMove(grip(), { pointerId: 1, clientX: 160, clientY: 20 });
    fireEvent.pointerMove(grip(), { pointerId: 1, clientX: 90, clientY: 80 });

    expect(moves()).toEqual([
      { dx: 60, dy: -30 },
      { dx: -10, dy: 30 },
    ]);
  });

  it("tells the caller to snapshot before the first move arrives", () => {
    const seen: string[] = [];
    render(<Probe onStart={() => seen.push("start")} />);

    fireEvent.pointerDown(grip(), { pointerId: 1, clientX: 0, clientY: 0 });
    expect(seen).toEqual(["start"]);

    fireEvent.pointerMove(grip(), { pointerId: 1, clientX: 10, clientY: 0 });
    expect(seen).toEqual(["start"]);
    expect(moves()).toEqual([{ dx: 10, dy: 0 }]);
  });

  it("captures the pointer so a move that leaves the grip still reaches it", () => {
    render(<Probe />);

    fireEvent.pointerDown(grip(), { pointerId: 7, clientX: 0, clientY: 0 });

    expect(Element.prototype.setPointerCapture).toHaveBeenCalledWith(7);
  });

  it("ignores a second pointer and any move that arrives before a press", () => {
    render(<Probe />);

    fireEvent.pointerMove(grip(), { pointerId: 1, clientX: 40, clientY: 40 });
    expect(moves()).toEqual([]);

    fireEvent.pointerDown(grip(), { pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(grip(), { pointerId: 2, clientX: 40, clientY: 40 });
    expect(moves()).toEqual([]);
  });

  it("ends on release, tells the caller once, and stops following", () => {
    const onEnd = vi.fn();
    render(<Probe onEnd={onEnd} />);

    fireEvent.pointerDown(grip(), { pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerUp(grip(), { pointerId: 1, clientX: 30, clientY: 0 });

    expect(onEnd).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("dragging")).toHaveTextContent("no");

    fireEvent.pointerMove(grip(), { pointerId: 1, clientX: 300, clientY: 0 });
    expect(moves()).toEqual([]);
  });

  it("ends the same way when the browser cancels the gesture", () => {
    const onEnd = vi.fn();
    render(<Probe onEnd={onEnd} />);

    fireEvent.pointerDown(grip(), { pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerCancel(grip(), { pointerId: 1 });

    expect(onEnd).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("dragging")).toHaveTextContent("no");
  });
});
