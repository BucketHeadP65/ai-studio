import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DiffView } from "./diff-view";

describe("DiffView", () => {
  it("renders a one-line replacement as one removed and one added row", () => {
    render(<DiffView oldText={"foo\nbar\nbaz\n"} newText={"foo\nBAR\nbaz\n"} />);

    const removed = screen.getByText("bar").closest("tr");
    const added = screen.getByText("BAR").closest("tr");
    expect(removed).not.toBeNull();
    expect(added).not.toBeNull();
    if (removed === null || added === null) {
      throw new Error("unreachable");
    }

    const removedCells = within(removed).getAllByRole("cell");
    expect(removedCells[0]).toHaveTextContent("2");
    expect(removedCells[1]).toHaveTextContent("");
    expect(removedCells[2]).toHaveTextContent("−");

    const addedCells = within(added).getAllByRole("cell");
    expect(addedCells[0]).toHaveTextContent("");
    expect(addedCells[1]).toHaveTextContent("2");
    expect(addedCells[2]).toHaveTextContent("+");
  });

  it("keeps both line numbers on an unchanged line", () => {
    render(<DiffView oldText={"foo\nbar\n"} newText={"foo\nbaz\n"} />);

    const context = screen.getByText("foo").closest("tr");
    expect(context).not.toBeNull();
    if (context === null) {
      throw new Error("unreachable");
    }
    const cells = within(context).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("1");
    expect(cells[1]).toHaveTextContent("1");
  });

  it("never re-lists an unchanged last line that merely gained its newline", () => {
    render(<DiffView oldText={"a\nb"} newText={"a\nb\nc\n"} />);

    expect(screen.getAllByText("b")).toHaveLength(1);
    expect(screen.queryByText("−")).not.toBeInTheDocument();
    expect(screen.getAllByText("+")).toHaveLength(1);
  });

  it("leaves the old line number blank on a pure addition", () => {
    render(<DiffView oldText={"a\n"} newText={"a\nb\n"} />);

    const added = screen.getByText("b").closest("tr");
    expect(added).not.toBeNull();
    if (added === null) {
      throw new Error("unreachable");
    }
    const cells = within(added).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("");
    expect(cells[1]).toHaveTextContent("2");
  });

  it("never re-lists a CRLF file's unchanged last line as removed just for gaining its own newline", () => {
    render(<DiffView oldText={"a\r\nb"} newText={"a\r\nb\r\nc\r\n"} />);

    expect(screen.getAllByText("b")).toHaveLength(1);
    expect(screen.queryByText("−")).not.toBeInTheDocument();
    expect(screen.getAllByText("+")).toHaveLength(1);
  });

  it("carries no carriage return in a CRLF file's own line text", () => {
    render(<DiffView oldText={"foo\r\nbar\r\nbaz\r\n"} newText={"foo\r\nBAR\r\nbaz\r\n"} />);

    expect(screen.getByText("bar").textContent).toBe("bar");
    expect(screen.getByText("BAR").textContent).toBe("BAR");
  });

  it("renders the line text wrapped in a monospace, wrapping cell", () => {
    render(<DiffView oldText={"a\n"} newText={"a\nb\n"} />);

    const text = screen.getByText("b");
    expect(text.className).toContain("font-mono");
    expect(text.className).toContain("whitespace-pre-wrap");
    expect(text.className).toContain("break-words");
  });
});
