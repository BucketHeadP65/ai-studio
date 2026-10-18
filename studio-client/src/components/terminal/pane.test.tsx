import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { type TerminalHandle, TerminalPane, type TerminalSocket } from "./pane";

function fakeSocket(): TerminalSocket {
  return {
    binaryType: "blob",
    readyState: 0,
    send: () => undefined,
    close: () => undefined,
    addEventListener: () => undefined,
  };
}

describe("<TerminalPane />", () => {
  it("mounts the terminal into its container once the dialog's portal exists", async () => {
    const opens: HTMLElement[] = [];
    const handle: TerminalHandle = {
      open: (container) => opens.push(container),
      fit: () => undefined,
      cols: 80,
      rows: 24,
      onData: () => ({ dispose: () => undefined }),
      write: () => undefined,
      dispose: () => undefined,
    };

    render(
      <TerminalPane
        cwd="/knowledge-bases/inbox"
        onClose={vi.fn()}
        createTerminal={() => handle}
        createSocket={fakeSocket}
      />,
    );

    await waitFor(() => expect(opens).toHaveLength(1));
    expect(opens[0]?.isConnected).toBe(true);
  });
});
