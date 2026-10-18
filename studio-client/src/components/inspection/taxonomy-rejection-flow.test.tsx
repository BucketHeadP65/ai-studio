import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { traceEntries } from "@/api/inspector-trace";
import { MessagePayloadSchema } from "@/api/sessions-types";

import captured from "./fixtures/taxonomy-rejected.json";
import multiple from "./fixtures/multiple-outputs.json";
import { RowContent, RowLabel } from "./trace-rows";

describe("recorded taxonomy rejection through the trace UI", () => {
  it("accepts only the selected output when a second invalid output was skipped", () => {
    const entries = traceEntries(MessagePayloadSchema.array().parse(multiple.messages));
    expect(multiple.resolution).toBe("dismissed");
    expect(entries.filter((entry) => entry.kind === "answered")).toMatchObject([
      { outcome: "dismissed" }, { outcome: "unvalidated" },
    ]);

    render(<>{entries.map((entry, index) => <div key={`${entry.kind}-${index}`}>
      <RowLabel entry={entry} model="synthetic-inspector" afterAsked={false} />
      <RowContent entry={entry} spans={[]} />
    </div>)}</>);

    expect(screen.getByText("Dismissed")).toBeInTheDocument();
    expect(screen.getByText("Output not accepted")).toBeInTheDocument();
    expect(screen.queryByText("diagnosis, pending verification")).not.toBeInTheDocument();
  });

  it("renders the first rejected and final exhausted answers without accepting either diagnosis", () => {
    const messages = MessagePayloadSchema.array().parse(captured.messages);
    const entries = traceEntries(messages);
    expect(captured.resolution).toBe("invalid");
    expect(entries.filter((entry) => entry.kind === "refused")).toHaveLength(1);
    expect(entries.filter((entry) => entry.kind === "answered")).toMatchObject([{ outcome: "unvalidated" }]);

    render(<>{entries.map((entry, index) => <div key={`${entry.kind}-${index}`}>
      <RowLabel entry={entry} model="synthetic-inspector" afterAsked={false} />
      <RowContent entry={entry} spans={[]} />
    </div>)}</>);

    expect(screen.getByText("Refused")).toBeInTheDocument();
    expect(screen.getByText("Output not accepted")).toBeInTheDocument();
    expect(screen.queryByText("Answered")).not.toBeInTheDocument();
    expect(screen.queryByText("diagnosis, pending verification")).not.toBeInTheDocument();
  });
});
