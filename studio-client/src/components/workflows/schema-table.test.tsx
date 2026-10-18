import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { JsonSchema } from "@/api/studio-types";
import { render } from "@/test-support/render";

import { SchemaTable } from "./schema-table";

// The DigestResult, Undigested, and DigestReport schemas are verbatim from the real research
// digest workflow's own artifact.

const DIGEST_RESULT: JsonSchema = {
  type: "object",
  title: "DigestResult",
  required: ["digest_id", "status"],
  properties: {
    reason: { type: "string" },
    status: { enum: ["written", "skipped"], type: "string" },
    digest_id: { type: "string" },
  },
};

const DIGEST_REPORT: JsonSchema = {
  type: "object",
  title: "DigestReport",
  required: ["written", "skipped", "requested"],
  properties: {
    skipped: { type: "integer" },
    written: { type: "integer" },
    requested: { type: "integer" },
  },
};

const UNDIGESTED_LIST: JsonSchema = {
  type: "array",
  items: {
    type: "object",
    title: "Undigested",
    required: ["source_id", "item_type", "title"],
    properties: {
      title: { type: "string" },
      item_type: { enum: ["video", "paper", "article", "post"], type: "string" },
      source_id: { type: "string" },
    },
  },
};

describe("SchemaTable", () => {
  it("captions a titled object and sorts its fields required first, then by name", () => {
    render(<SchemaTable schema={DIGEST_RESULT} />);

    expect(screen.getByText("One DigestResult with these fields")).toBeInTheDocument();
    const rows = screen.getAllByRole("row").slice(1); // drop the header row
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("digest_id"),
      expect.stringContaining("status"),
      expect.stringContaining("reason"),
    ]);
  });

  it("shows a property's enum as its allowed values, joined with a comma", () => {
    render(<SchemaTable schema={DIGEST_RESULT} />);

    expect(screen.getByText("written, skipped")).toBeInTheDocument();
  });

  it("marks a field required or optional off the schema's own required array", () => {
    render(<SchemaTable schema={DIGEST_RESULT} />);

    const statusRow = screen.getByText("status").closest("tr");
    const reasonRow = screen.getByText("reason").closest("tr");
    expect(statusRow).toHaveTextContent("required");
    expect(reasonRow).toHaveTextContent("optional");
  });

  it("captions a list of a titled object and reads its item fields", () => {
    render(<SchemaTable schema={UNDIGESTED_LIST} />);

    expect(screen.getByText("A list of Undigested, each with these fields")).toBeInTheDocument();
    expect(screen.getByText("source_id")).toBeInTheDocument();
    expect(screen.getByText("item_type")).toBeInTheDocument();
  });

  it("reads all three of DigestReport's own integer fields", () => {
    render(<SchemaTable schema={DIGEST_REPORT} />);

    expect(screen.getByText("One DigestReport with these fields")).toBeInTheDocument();
    for (const name of ["requested", "skipped", "written"]) {
      const row = screen.getByText(name).closest("tr");
      expect(row).toHaveTextContent("integer");
    }
  });

  it("captions an object with no title as an object with these fields", () => {
    render(<SchemaTable schema={{ type: "object", properties: { x: { type: "string" } } }} />);

    expect(screen.getByText("An object with these fields")).toBeInTheDocument();
  });

  it("captions a bare primitive with no table", () => {
    render(<SchemaTable schema={{ type: "string", minLength: 1 }} />);

    expect(screen.getByText("A string")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("captions a list of a primitive", () => {
    render(<SchemaTable schema={{ type: "array", items: { type: "string" } }} />);

    expect(screen.getByText("A list of string")).toBeInTheDocument();
  });

  it("captions a list per item, each a list of a titled object, off a nested array", () => {
    const nested: JsonSchema = {
      type: "array",
      items: { type: "array", items: DIGEST_RESULT },
    };
    render(<SchemaTable schema={nested} />);

    expect(
      screen.getByText("A list per item, each a list of DigestResult with these fields"),
    ).toBeInTheDocument();
    expect(screen.getByText("digest_id")).toBeInTheDocument();
  });

  it("shows a property's description in the allowed values column when it carries no enum", () => {
    const schema: JsonSchema = {
      type: "object",
      title: "Note",
      properties: { text: { type: "string", description: "the note body" } },
    };
    render(<SchemaTable schema={schema} />);

    expect(screen.getByText("the note body")).toBeInTheDocument();
  });

  it("nests an object property's own fields one level, indented, no deeper", () => {
    const schema: JsonSchema = {
      type: "object",
      title: "Wrapper",
      properties: {
        inner: {
          type: "object",
          title: "Inner",
          properties: { deep: { type: "string" } },
        },
      },
    };
    render(<SchemaTable schema={schema} />);

    expect(screen.getByText("inner")).toBeInTheDocument();
    expect(screen.getByText("deep")).toBeInTheDocument();
  });

  it("resolves a $ref against the schema's own $defs before reading its fields", () => {
    const schema: JsonSchema = {
      $ref: "#/$defs/DigestResult",
      $defs: { DigestResult: DIGEST_RESULT },
    };
    render(<SchemaTable schema={schema} />);

    expect(screen.getByText("One DigestResult with these fields")).toBeInTheDocument();
  });

  it("says the shape is not known here for a null schema", () => {
    render(<SchemaTable schema={null} />);

    expect(screen.getByText("The shape of this value is not known here.")).toBeInTheDocument();
  });

  it("an explicit caption prop overrides the computed one", () => {
    render(<SchemaTable schema={DIGEST_RESULT} caption="Its own custom caption" />);

    expect(screen.getByText("Its own custom caption")).toBeInTheDocument();
    expect(screen.queryByText("One DigestResult with these fields")).not.toBeInTheDocument();
  });

  it("toggles between the field table and raw JSON, flipping its own label", async () => {
    render(<SchemaTable schema={DIGEST_RESULT} />);

    await userEvent.click(screen.getByRole("button", { name: "Show as JSON" }));
    expect(screen.getByText(/"title": "DigestResult"/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Show as fields" }));
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

describe("SchemaTable, the shapes pydantic emits", () => {
  it("reads a field's type, never the title pydantic gives every field after its own name", () => {
    render(
      <SchemaTable
        schema={{
          type: "object",
          title: "Undigested",
          required: ["item_type"],
          properties: {
            item_type: { title: "Item Type", type: "string", enum: ["video", "paper"] },
          },
        }}
      />,
    );

    expect(screen.getByText("string")).toBeInTheDocument();
    expect(screen.queryByText("Item Type")).not.toBeInTheDocument();
  });

  it("resolves a $ref against $defs carried by a wrapped items schema, as a map's output is", () => {
    const wrapped: JsonSchema = {
      type: "array",
      items: {
        $defs: {
          Reference: {
            type: "object",
            title: "Reference",
            required: ["why"],
            properties: {
              why: { title: "Why", type: "string" },
              url: { anyOf: [{ type: "string" }, { type: "null" }], default: null, title: "Url" },
            },
          },
        },
        type: "array",
        items: { $ref: "#/$defs/Reference" },
      },
    };
    render(<SchemaTable schema={wrapped} />);

    expect(
      screen.getByText("A list per item, each a list of Reference with these fields"),
    ).toBeInTheDocument();
    expect(screen.getByText("why")).toBeInTheDocument();
    expect(screen.getByText("url")).toBeInTheDocument();
    expect(screen.getAllByText("string")).toHaveLength(2);
  });

  it("reads the allowed values of an optional literal through its anyOf with null", () => {
    render(
      <SchemaTable
        schema={{
          type: "object",
          title: "Flag",
          properties: {
            kind: {
              anyOf: [{ enum: ["idea", "risk", "question"], type: "string" }, { type: "null" }],
              default: null,
              title: "Kind",
            },
          },
        }}
      />,
    );

    expect(screen.getByText("idea, risk, question")).toBeInTheDocument();
    expect(screen.getByText("string")).toBeInTheDocument();
    expect(screen.getByText("optional")).toBeInTheDocument();
  });
});
