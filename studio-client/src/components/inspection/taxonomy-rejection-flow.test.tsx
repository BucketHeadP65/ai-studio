import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { render } from "@/shared/test-support/render";

import captured from "./fixtures/taxonomy-rejected.json";
import multiple from "./fixtures/multiple-outputs.json";
import { TurnMessages } from "./turn-messages";

// Recorded inspector conversations, drawn the way the finding page draws one: as chat turns.

function recorded(payloads: { kind: string }[]) {
  return payloads.map((payload, idx) => ({ run_id: "inspector-synthetic", idx, kind: payload.kind, payload, text: "" }));
}

describe("recorded taxonomy rejection through the chat rendering", () => {
  it("reads the accepted output's return and the skipped output's own, each on its card", async () => {
    expect(multiple.resolution).toBe("dismissed");
    render(<TurnMessages messages={recorded(multiple.messages)} speaker="synthetic-inspector" />);

    await userEvent.click(screen.getByRole("button", { name: "Called 2 tools" }));

    expect(screen.getByRole("button", { name: /^Completed\s*final_result_NoIssue\s*Final result processed\./ })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Completed\s*final_result_Attribution\s*Output tool not used - a final result was already processed\./ }),
    ).toBeInTheDocument();
  });

  it("shows the refused answer as an error whose body reads the refusal, and the exhausted answer as never answered", async () => {
    expect(captured.resolution).toBe("invalid");
    render(<TurnMessages messages={recorded(captured.messages)} speaker="synthetic-inspector" />);

    expect(screen.getByRole("button", { name: /^Completed\s*final_result_Attribution\s*pending/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^Error\s*final_result_Attribution/ }));

    expect(
      await screen.findByText(`the inspector named something the taxonomy does not know: "no symptom named 'not_in_taxonomy'"`),
    ).toBeInTheDocument();
  });
});
