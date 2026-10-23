import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { render } from "@/shared/test-support/render";

import ProjectsPage from "./page";

afterEach(cleanup);

describe("ProjectsPage", () => {
  it("asks the owner to pick a project when none is selected", () => {
    render(<ProjectsPage />);

    expect(screen.getByText("Pick a project.")).toBeInTheDocument();
  });
});
