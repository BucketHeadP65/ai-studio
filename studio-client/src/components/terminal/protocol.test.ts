import { describe, expect, it } from "vitest";

import { resizeMessage, terminalWsUrl } from "./protocol";

describe("resizeMessage", () => {
  it("frames a resize as the control JSON the route expects", () => {
    expect(JSON.parse(resizeMessage(120, 40))).toEqual({ type: "resize", cols: 120, rows: 40 });
  });
});

describe("terminalWsUrl", () => {
  it("swaps http for ws and carries the cwd as a query param", () => {
    expect(terminalWsUrl("http://127.0.0.1:8770", "/knowledge-bases/inbox")).toBe(
      "ws://127.0.0.1:8770/api/terminal/ws?cwd=%2Fknowledge-bases%2Finbox",
    );
  });

  it("swaps https for wss", () => {
    expect(terminalWsUrl("https://studio.example", "/x")).toBe(
      "wss://studio.example/api/terminal/ws?cwd=%2Fx",
    );
  });

  it("percent-encodes a cwd with spaces or other reserved characters", () => {
    expect(terminalWsUrl("http://127.0.0.1:8770", "/knowledge-bases/my inbox & co")).toBe(
      "ws://127.0.0.1:8770/api/terminal/ws?cwd=%2Fknowledge-bases%2Fmy%20inbox%20%26%20co",
    );
  });
});
