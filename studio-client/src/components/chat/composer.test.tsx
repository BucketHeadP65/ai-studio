import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { render } from "@/test-support/render";

import { Composer, type ComposerProps, useComposerDraft } from "./composer";

vi.mock("next/navigation", () => ({
  usePathname: () => "/chat",
  useSearchParams: () => new URLSearchParams(),
}));

// Imported after the mock so the components under test see the mocked module.
import type { FollowMe } from "@/components/follow-me/provider";
import { FollowMeProvider, useFollowMe } from "@/components/follow-me/provider";

// The draft lives above the composer in the app, so every render here goes through a host that
// holds one, the way a chat surface does.
function ComposerHost({
  target,
  ...props
}: Omit<ComposerProps, "draft"> & { target?: string }): ReactElement {
  return <Composer {...props} draft={useComposerDraft(target)} />;
}

function renderComposer(overrides: Partial<Omit<ComposerProps, "draft">> = {}) {
  return render(
    <ComposerHost
      onSend={vi.fn()}
      busy={false}
      onStop={null}
      notice={null}
      gateMode="wait"
      {...overrides}
    />,
  );
}

let followMe: FollowMe = {
  collectContext: () => {
    throw new Error("Probe did not mount");
  },
  registerExtras: () => () => {},
  registerTarget: () => () => {},
  findTarget: () => null,
  targetNames: () => [],
  whenAt: () => Promise.resolve(),
};

function Probe(): null {
  followMe = useFollowMe();
  return null;
}

function renderWithProbe(element: ReactElement) {
  const rendered = render(
    <FollowMeProvider>
      <Probe />
      {element}
    </FollowMeProvider>,
  );
  return { ...rendered, followMe };
}

describe("Composer", () => {
  it("sends on Enter and clears the box", async () => {
    const onSend = vi.fn();
    renderComposer({ onSend });
    const box = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(box, "hello{Enter}");
    expect(onSend).toHaveBeenCalledWith("hello");
    expect(box).toHaveValue("");
  });

  it("keeps Shift+Enter for a newline instead of sending", async () => {
    const onSend = vi.fn();
    renderComposer({ onSend });
    const box = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(box, "line{Shift>}{Enter}{/Shift}two");
    expect(onSend).not.toHaveBeenCalled();
    expect(box).toHaveValue("line\ntwo");
  });

  it("starts one line tall, with Send as the icon at the right", () => {
    renderComposer();
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveAttribute("rows", "1");
    expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("title", "Send (Return)");
  });

  it("disables the textarea and Send while busy", () => {
    renderComposer({ busy: true });
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("hides Stop when onStop is null", () => {
    renderComposer({ onStop: null });
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("shows Stop when onStop is given, and calls it", async () => {
    const onStop = vi.fn();
    renderComposer({ onStop, busy: true });
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("renders the notice with role status", () => {
    renderComposer({ notice: "answering elsewhere" });
    expect(screen.getByRole("status")).toHaveTextContent("answering elsewhere");
  });

  it.each([
    ["wait", "gates wait for you"],
    ["orchestrate", "the orchestrator decides gates"],
    ["auto", "gates pass automatically"],
  ] as const)("reads the rail's gate mode %s as %s", (gateMode, hint) => {
    renderComposer({ gateMode });
    expect(screen.getByText(hint)).toBeInTheDocument();
  });

  it("stays usable, textarea and Send present, while busy from a gate wait upstream", () => {
    // The composer itself carries no notion of a pending gate any more -- a caller keeps it
    // enabled by not marking it busy for that reason. This only pins that the textarea and
    // Send always render, with no takeover swap left to remove them.
    renderComposer({ busy: false });
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
  });

  it("registers as an input target when given a name and takes a prefill", () => {
    const { followMe } = renderWithProbe(
      <ComposerHost
        target="composer"
        onSend={vi.fn()}
        busy={false}
        onStop={null}
        notice={null}
        gateMode="wait"
      />,
    );

    expect(followMe.collectContext().targets.map((t) => t.name)).toEqual(["composer"]);

    act(() => {
      followMe.findTarget("composer")?.handle.prefill?.("drafted for you");
    });

    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("drafted for you");
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
  });

  it("registers nothing without a target name", () => {
    const { followMe } = renderWithProbe(
      <ComposerHost onSend={vi.fn()} busy={false} onStop={null} notice={null} gateMode="wait" />,
    );

    expect(followMe.collectContext().targets).toEqual([]);
  });
});
