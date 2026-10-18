import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { OpenFolderDialog } from "./open-folder-dialog";

afterEach(cleanup);

const BASE = "http://studio.test";

describe("<OpenFolderDialog />", () => {
  it("renders nothing when closed", () => {
    render(<OpenFolderDialog open={false} onClose={vi.fn()} onOpened={vi.fn()} />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("posts the typed absolute path and reports the registered project", async () => {
    server.use(
      http.post(`${BASE}/api/projects`, async ({ request }) => {
        expect(await request.json()).toEqual({ root_path: "/home/user/engram" });
        return HttpResponse.json({
          project: { id: "proj-1", name: "engram", root_path: "/home/user/engram" },
        });
      }),
    );
    const onOpened = vi.fn();
    const user = userEvent.setup();

    render(<OpenFolderDialog open onClose={vi.fn()} onOpened={onOpened} />);
    await user.type(screen.getByLabelText("Absolute path"), "/home/user/engram");
    await user.click(screen.getByRole("button", { name: "Open" }));

    await vi.waitFor(() =>
      expect(onOpened).toHaveBeenCalledWith({
        id: "proj-1",
        name: "engram",
        root_path: "/home/user/engram",
      }),
    );
  });

  it("surfaces a 400 (not a directory) inline, without closing", async () => {
    server.use(
      http.post(`${BASE}/api/projects`, () =>
        HttpResponse.json({ detail: "'/nope' is not a directory." }, { status: 400 }),
      ),
    );
    const onOpened = vi.fn();
    const onClose = vi.fn();
    const user = userEvent.setup();

    render(<OpenFolderDialog open onClose={onClose} onOpened={onOpened} />);
    await user.type(screen.getByLabelText("Absolute path"), "/nope");
    await user.click(screen.getByRole("button", { name: "Open" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("'/nope' is not a directory.");
    expect(onOpened).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("disables Open until a path is typed", () => {
    render(<OpenFolderDialog open onClose={vi.fn()} onOpened={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Open" })).toBeDisabled();
  });

  it("cancels via the Cancel button and via Escape", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();

    render(<OpenFolderDialog open onClose={onClose} onOpened={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("clears the field and any error on reopen", async () => {
    server.use(
      http.post(`${BASE}/api/projects`, () =>
        HttpResponse.json({ detail: "'/nope' is not a directory." }, { status: 400 }),
      ),
    );
    const user = userEvent.setup();

    const { rerender } = render(<OpenFolderDialog open onClose={vi.fn()} onOpened={vi.fn()} />);
    await user.type(screen.getByLabelText("Absolute path"), "/nope");
    await user.click(screen.getByRole("button", { name: "Open" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();

    rerender(<OpenFolderDialog open={false} onClose={vi.fn()} onOpened={vi.fn()} />);
    rerender(<OpenFolderDialog open onClose={vi.fn()} onOpened={vi.fn()} />);

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Absolute path")).toHaveValue("");
  });
});
