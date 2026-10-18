import { act, render } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { beforeEach, describe, expect, it } from "vitest";

import { useStoredChoice, useStoredFlag, useStoredJson, useStoredNumber } from "./use-stored";

// The bug these exist to prevent: reading localStorage inside a `useState`
// initializer. The server has no localStorage so it renders the fallback; the
// browser reads a stored value and renders something else; React throws
// "Hydration failed because the server rendered HTML didn't match the client"
// and throws the tree away. Live symptom: the studio's chat rail had been
// collapsed, so every page load hydration-errored on the rail markup.
//
// The invariant is therefore about the FIRST render, not the final state:
// server output and the client's initial output must be identical whatever is
// in storage, and the stored value may only arrive afterwards, in an effect.

function Flag() {
  const [open] = useStoredFlag("probe:flag", true);
  return <span>{open ? "open" : "closed"}</span>;
}

function Width() {
  const [width] = useStoredNumber("probe:width", 330);
  return <span>{width}</span>;
}

describe("useStoredFlag / useStoredNumber", () => {
  beforeEach(() => window.localStorage.clear());

  it("renders the fallback on the server even when storage disagrees", () => {
    window.localStorage.setItem("studio:probe:flag", "0");
    window.localStorage.setItem("studio:probe:width", "500");
    expect(renderToString(<Flag />)).toContain("open");
    expect(renderToString(<Width />)).toContain("330");
  });

  it("adopts the stored value after mount, so the two renders can agree first", () => {
    window.localStorage.setItem("studio:probe:flag", "0");
    const { container } = render(<Flag />);
    // Effects have flushed by now: the stored value is in.
    expect(container.textContent).toBe("closed");
  });

  it("writes through on change so the choice survives a reload", () => {
    let set: (next: boolean) => void = () => {};
    function Harness() {
      const [open, setOpen] = useStoredFlag("probe:flag", true);
      set = setOpen;
      return <span>{open ? "open" : "closed"}</span>;
    }
    const { container } = render(<Harness />);
    act(() => set(false));
    expect(container.textContent).toBe("closed");
    expect(window.localStorage.getItem("studio:probe:flag")).toBe("0");
  });

  it("ignores a stored value that is not a number", () => {
    window.localStorage.setItem("studio:probe:width", "not-a-number");
    const { container } = render(<Width />);
    expect(container.textContent).toBe("330");
  });

  it("keeps two mounted hooks on the same key in sync within a tab", () => {
    let setFirst: (next: boolean) => void = () => {};

    function First() {
      const [open, setOpen] = useStoredFlag("sync-probe", false);
      setFirst = setOpen;
      return <span data-testid="first">{open ? "open" : "closed"}</span>;
    }

    function Second() {
      const [open] = useStoredFlag("sync-probe", false);
      return <span data-testid="second">{open ? "open" : "closed"}</span>;
    }

    const { getByTestId } = render(
      <>
        <First />
        <Second />
      </>,
    );
    expect(getByTestId("second").textContent).toBe("closed");

    act(() => setFirst(true));

    expect(getByTestId("first").textContent).toBe("open");
    expect(getByTestId("second").textContent).toBe("open");
  });
});

type Corner = "left" | "right";

const CORNERS: readonly Corner[] = ["left", "right"];

interface Box {
  w: number;
  h: number;
}

function parseBox(value: unknown): Box | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const source: Record<string, unknown> = { ...value };
  return typeof source.w === "number" && typeof source.h === "number"
    ? { w: source.w, h: source.h }
    : undefined;
}

function Corner() {
  const [corner] = useStoredChoice<Corner>("probe:corner", "left", CORNERS);
  return <span>{corner}</span>;
}

function BoxProbe() {
  const [box] = useStoredJson<Box | null>("probe:box", null, parseBox);
  return <span>{box === null ? "none" : `${box.w}x${box.h}`}</span>;
}

describe("useStoredChoice", () => {
  beforeEach(() => window.localStorage.clear());

  it("renders the fallback first, then adopts a stored member of the set", () => {
    window.localStorage.setItem("studio:probe:corner", "right");
    expect(renderToString(<Corner />)).toContain("left");
    const { container } = render(<Corner />);
    expect(container.textContent).toBe("right");
  });

  it("ignores a stored value outside the set", () => {
    window.localStorage.setItem("studio:probe:corner", "ceiling");
    const { container } = render(<Corner />);
    expect(container.textContent).toBe("left");
  });

  it("writes the plain value through, no quoting", () => {
    let set: (next: Corner) => void = () => {};
    function Harness() {
      const [corner, setCorner] = useStoredChoice<Corner>("probe:corner", "left", CORNERS);
      set = setCorner;
      return <span>{corner}</span>;
    }
    render(<Harness />);
    act(() => set("right"));
    expect(window.localStorage.getItem("studio:probe:corner")).toBe("right");
  });
});

describe("useStoredJson", () => {
  beforeEach(() => window.localStorage.clear());

  it("renders the fallback first, then adopts a stored shape that parses", () => {
    window.localStorage.setItem("studio:probe:box", JSON.stringify({ w: 12, h: 34 }));
    expect(renderToString(<BoxProbe />)).toContain("none");
    const { container } = render(<BoxProbe />);
    expect(container.textContent).toBe("12x34");
  });

  it("ignores broken json and a shape the parser rejects", () => {
    window.localStorage.setItem("studio:probe:box", "{not json");
    expect(render(<BoxProbe />).container.textContent).toBe("none");

    window.localStorage.setItem("studio:probe:box", JSON.stringify({ w: 12 }));
    expect(render(<BoxProbe />).container.textContent).toBe("none");
  });

  it("writes the shape through as json", () => {
    let set: (next: Box | null) => void = () => {};
    function Harness() {
      const [box, setBox] = useStoredJson<Box | null>("probe:box", null, parseBox);
      set = setBox;
      return <span>{box === null ? "none" : `${box.w}x${box.h}`}</span>;
    }
    render(<Harness />);
    act(() => set({ w: 9, h: 8 }));
    expect(window.localStorage.getItem("studio:probe:box")).toBe('{"w":9,"h":8}');
  });
});
