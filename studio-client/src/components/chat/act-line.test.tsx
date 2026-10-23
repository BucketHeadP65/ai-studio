import { screen } from "@testing-library/dom";
import type { ToolUIPart } from "ai";
import { describe, expect, it } from "vitest";

import { render } from "@/shared/test-support/render";

import { ActLine } from "./act-line";

describe("ActLine", () => {
  it.each([
    [
      { type: "tool-navigate", toolCallId: "c", state: "input-available", input: { url: "/chat" } },
      "navigate, pending",
    ],
    [
      {
        type: "tool-navigate",
        toolCallId: "c",
        state: "output-available",
        input: {},
        output: "navigated to /chat",
      },
      "navigate: navigated to /chat",
    ],
    [
      {
        type: "tool-toggle_plugin",
        toolCallId: "c",
        state: "output-available",
        input: {},
        output: "pending",
      },
      "toggle_plugin, left waiting and never ran",
    ],
    [
      {
        type: "tool-toggle_plugin",
        toolCallId: "c",
        state: "approval-requested",
        input: {},
        approval: { id: "c" },
      },
      "toggle_plugin, waiting for your approval",
    ],
    [
      {
        type: "tool-toggle_plugin",
        toolCallId: "c",
        state: "approval-responded",
        input: {},
        approval: { id: "c", approved: false, reason: "superseded" },
      },
      "toggle_plugin, denied, superseded",
    ],
    [
      { type: "tool-toggle_plugin", toolCallId: "c", state: "output-denied", input: {} },
      "toggle_plugin, denied",
    ],
    [
      {
        type: "tool-toggle_plugin",
        toolCallId: "c",
        state: "output-error",
        input: {},
        errorText: "boom",
      },
      "toggle_plugin failed: boom",
    ],
  ])("renders %o as %s", (part, text) => {
    render(<ActLine part={part as ToolUIPart} />);
    expect(screen.getByText(text)).toBeInTheDocument();
  });
});
