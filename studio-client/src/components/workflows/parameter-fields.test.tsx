import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import type { GraphNode, JsonSchema, WorkflowGraph, WorkflowPresentation } from "@/api/studio-types";
import { render } from "@/test-support/render";

import {
  type KnowledgeBaseRegistry,
  UNKNOWN_KNOWLEDGE_BASES,
  type ParameterDraft,
  ParameterFields,
  type ParameterField,
  draftFrom,
  emptyDraft,
  parameterFields,
  readParameters,
  usesKnowledgeBases,
} from "./parameter-fields";

/** The registry as a deployment that has answered with exactly these names. */
function registered(...names: string[]): KnowledgeBaseRegistry {
  return { names, error: null };
}

// The control registry: one declared parameter, its JSON Schema, and the author's own
// presentation hint, drawn as the control a person answers through. Every assertion here is
// about the NATIVE value that comes back out (an array stays an array, a boolean a boolean)
// and the sentence a field says when what was typed cannot be that value.

function paramNode(id: string, schema: JsonSchema | null, optional = false): GraphNode {
  return {
    key: `param:${id}`,
    id,
    kind: "param",
    signature: "value",
    required: !optional,
    detail: "",
    tools: [],
    is_output: false,
    unknown: false,
    brought: false,
    briefing: "",
    input_schemas: {},
    output_schema: schema,
    source: "",
  };
}

function graphOf(...nodes: GraphNode[]): WorkflowGraph {
  return { name: "decide", version: 1, nodes, edges: [] };
}

function presentationOf(
  inputs: WorkflowPresentation["inputs"],
): WorkflowPresentation {
  return {
    purpose: "",
    produces: "",
    owner_participation: "",
    not_for: "",
    inputs,
    result_fields: [],
    actions: [],
  };
}

function Harness({
  fields,
  knowledgeBases = UNKNOWN_KNOWLEDGE_BASES,
}: {
  fields: readonly ParameterField[];
  knowledgeBases?: KnowledgeBaseRegistry;
}): ReactElement {
  const [draft, setDraft] = useState<ParameterDraft>(() => emptyDraft(fields));
  const read = readParameters(fields, draft, { knowledgeBases });
  return (
    <>
      <ParameterFields
        fields={fields}
        draft={draft}
        errors={read.errors}
        knowledgeBases={knowledgeBases}
        onChange={(key, value) => setDraft((current) => ({ ...current, [key]: value }))}
      />
      <pre data-testid="values">{JSON.stringify(read.values)}</pre>
      <pre data-testid="errors">{JSON.stringify(read.errors)}</pre>
    </>
  );
}

function values(): unknown {
  return JSON.parse(screen.getByTestId("values").textContent ?? "{}");
}

function renderFields(
  graph: WorkflowGraph,
  presentation: WorkflowPresentation | null = null,
  knowledgeBases: KnowledgeBaseRegistry = UNKNOWN_KNOWLEDGE_BASES,
) {
  const fields = parameterFields(graph, presentation);
  render(<Harness fields={fields} knowledgeBases={knowledgeBases} />);
  return fields;
}

describe("parameterFields", () => {
  it("reads a parameter's own schema, its label, and whether it may be omitted", () => {
    const fields = parameterFields(
      graphOf(
        paramNode("question", { type: "string" }),
        paramNode("constraints", { type: "string" }, true),
      ),
      presentationOf([{ key: "question", label: "What are you deciding?", control: "textarea" }]),
    );

    expect(fields).toEqual([
      {
        key: "question",
        label: "What are you deciding?",
        help: "",
        required: true,
        schema: { type: "string" },
        control: "textarea",
      },
      {
        key: "constraints",
        label: "constraints",
        help: "",
        required: false,
        schema: { type: "string" },
        control: "text",
      },
    ]);
  });

  it("reads whether a launch must supply a parameter off the node's own boolean", () => {
    const fields = parameterFields(
      graphOf(paramNode("question", { type: "string" }), paramNode("notes", { type: "string" }, true)),
      null,
    );
    expect(fields.map((field) => field.required)).toEqual([true, false]);
  });

  it("names the knowledge-base control as the only one needing the registry", () => {
    const withSelector = parameterFields(
      graphOf(paramNode("bases", { type: "array", items: { type: "string" } })),
      presentationOf([{ key: "bases", label: "Knowledge bases", control: "knowledge-base" }]),
    );
    const without = parameterFields(graphOf(paramNode("question", { type: "string" })), null);

    expect(usesKnowledgeBases(withSelector)).toBe(true);
    expect(usesKnowledgeBases(without)).toBe(false);
  });
});

describe("draftFrom", () => {
  it("puts the values one run was given back in the controls they were answered through", () => {
    const fields = parameterFields(
      graphOf(
        paramNode("question", { type: "string" }),
        paramNode("tags", { type: "array", items: { type: "string" } }),
      ),
      null,
    );

    expect(draftFrom(fields, { question: "which cache?", tags: ["alpha", "beta"] })).toEqual({
      question: "which cache?",
      tags: ["alpha", "beta"],
    });
  });

  it("refuses a list whole when one entry is not a string, rather than dropping that entry", () => {
    const fields = parameterFields(
      graphOf(paramNode("tags", { type: "array", items: { type: "string" } })),
      null,
    );

    expect(draftFrom(fields, { tags: ["alpha", 7] })).toEqual({ tags: [] });
  });
});

describe("ParameterFields", () => {
  it("asks for a required string in a text box and hands back the string typed", async () => {
    renderFields(graphOf(paramNode("question", { type: "string", minLength: 1 })));

    expect(screen.getByText("This is required.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("question"), "Which cache?");

    expect(values()).toEqual({ question: "Which cache?" });
    expect(screen.queryByText("This is required.")).not.toBeInTheDocument();
  });

  it("leaves an optional string out of the arguments rather than sending an empty one", () => {
    renderFields(graphOf(paramNode("constraints", { type: "string" }, true)));

    expect(values()).toEqual({});
    expect(screen.queryByText("This is required.")).not.toBeInTheDocument();
  });

  it("says how short a required string may be, in the schema's own bound", async () => {
    renderFields(graphOf(paramNode("code", { type: "string", minLength: 3, maxLength: 4 })));

    await userEvent.type(screen.getByLabelText("code"), "ab");
    expect(screen.getByText("Enter at least 3 characters.")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("code"), "cde");
    expect(screen.getByText("Enter 4 characters or fewer.")).toBeInTheDocument();
  });

  it("answers a textarea hint with a multiline box", async () => {
    renderFields(
      graphOf(paramNode("question", { type: "string" })),
      presentationOf([{ key: "question", label: "Question", control: "textarea" }]),
    );

    const box = screen.getByLabelText("Question");
    expect(box.tagName).toBe("TEXTAREA");
    await userEvent.type(box, "Which cache?");
    expect(values()).toEqual({ question: "Which cache?" });
  });

  it("draws an enum as a select and hands back the member itself", async () => {
    renderFields(graphOf(paramNode("depth", { type: "string", enum: ["quick", "thorough"] })));

    const select = screen.getByLabelText("depth");
    expect(select.tagName).toBe("SELECT");
    await userEvent.selectOptions(select, "thorough");

    expect(values()).toEqual({ depth: "thorough" });
  });

  it("draws a boolean as a checkbox and hands back true or false, never a string", async () => {
    renderFields(graphOf(paramNode("publish", { type: "boolean" })));

    expect(values()).toEqual({ publish: false });
    await userEvent.click(screen.getByLabelText("publish"));
    expect(values()).toEqual({ publish: true });
  });

  it("draws a number as a number box and hands back a number, refusing what is not one", async () => {
    renderFields(graphOf(paramNode("budget", { type: "number", minimum: 1, maximum: 10 })));

    await userEvent.type(screen.getByLabelText("budget"), "2.5");
    expect(values()).toEqual({ budget: 2.5 });

    await userEvent.clear(screen.getByLabelText("budget"));
    await userEvent.type(screen.getByLabelText("budget"), "40");
    expect(screen.getByText("Enter 10 or less.")).toBeInTheDocument();
  });

  it("holds an integer parameter to whole numbers", async () => {
    renderFields(graphOf(paramNode("rounds", { type: "integer", minimum: 1 })));

    await userEvent.type(screen.getByLabelText("rounds"), "2.5");
    expect(screen.getByText("Enter a whole number.")).toBeInTheDocument();
  });

  it("collects a bounded string array as an array, one entry at a time", async () => {
    renderFields(
      graphOf(
        paramNode("tags", {
          type: "array",
          items: { type: "string", minLength: 1 },
          minItems: 2,
          maxItems: 3,
          uniqueItems: true,
        }),
      ),
    );

    await userEvent.type(screen.getByLabelText("tags 1"), "payments");
    expect(screen.getByText("Choose at least 2.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Add to tags" }));
    await userEvent.type(screen.getByLabelText("tags 2"), "payments");
    expect(screen.getByText("Every entry must be different.")).toBeInTheDocument();

    await userEvent.clear(screen.getByLabelText("tags 2"));
    await userEvent.type(screen.getByLabelText("tags 2"), "cache");
    expect(values()).toEqual({ tags: ["payments", "cache"] });
  });

  it("offers the registered knowledge bases and nothing else, inside the schema's own bounds", async () => {
    renderFields(
      graphOf(
        paramNode("knowledge_bases", {
          type: "array",
          items: { type: "string" },
          minItems: 1,
          maxItems: 3,
          uniqueItems: true,
        }),
      ),
      presentationOf([
        { key: "knowledge_bases", label: "Knowledge bases", control: "knowledge-base" },
      ]),
      registered("research", "repo-docs", "vault", "notebook"),
    );

    expect(screen.getByText("This is required.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "research" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "repo-docs" }));
    expect(values()).toEqual({ knowledge_bases: ["research", "repo-docs"] });

    await userEvent.click(screen.getByRole("checkbox", { name: "vault" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "notebook" }));
    expect(screen.getByText("Choose 3 or fewer.")).toBeInTheDocument();
  });

  it("says it is still reading the registry rather than that nothing is registered", () => {
    renderFields(
      graphOf(paramNode("knowledge_bases", { type: "array", items: { type: "string" } })),
      presentationOf([
        { key: "knowledge_bases", label: "Knowledge bases", control: "knowledge-base" },
      ]),
      UNKNOWN_KNOWLEDGE_BASES,
    );

    expect(screen.getByText("Reading the knowledge bases.")).toBeInTheDocument();
    expect(screen.queryByText("No knowledge base is registered here yet.")).not.toBeInTheDocument();
  });

  it("shows the registry's own refusal when it could not be read", () => {
    renderFields(
      graphOf(paramNode("knowledge_bases", { type: "array", items: { type: "string" } })),
      presentationOf([
        { key: "knowledge_bases", label: "Knowledge bases", control: "knowledge-base" },
      ]),
      { names: null, error: "the registry of knowledge bases could not be read" },
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "the registry of knowledge bases could not be read",
    );
  });

  it("leaves an already chosen knowledge base alone while the registry is unknown", () => {
    const fields = parameterFields(
      graphOf(paramNode("knowledge_bases", { type: "array", items: { type: "string" } })),
      presentationOf([
        { key: "knowledge_bases", label: "Knowledge bases", control: "knowledge-base" },
      ]),
    );
    const read = readParameters(
      fields,
      { knowledge_bases: ["research"] },
      { knowledgeBases: UNKNOWN_KNOWLEDGE_BASES },
    );

    expect(read.errors).toEqual({});
    expect(read.values).toEqual({ knowledge_bases: ["research"] });
  });

  it("says the registry holds nothing only once it has answered with nothing", () => {
    renderFields(
      graphOf(paramNode("knowledge_bases", { type: "array", items: { type: "string" } })),
      presentationOf([
        { key: "knowledge_bases", label: "Knowledge bases", control: "knowledge-base" },
      ]),
      registered(),
    );

    expect(screen.getByText("No knowledge base is registered here yet.")).toBeInTheDocument();
  });

  it("refuses a selected knowledge base this deployment does not have registered", () => {
    const fields = parameterFields(
      graphOf(paramNode("knowledge_bases", { type: "array", items: { type: "string" } })),
      presentationOf([
        { key: "knowledge_bases", label: "Knowledge bases", control: "knowledge-base" },
      ]),
    );
    const read = readParameters(
      fields,
      { knowledge_bases: ["ghost"] },
      { knowledgeBases: registered("research") },
    );

    expect(read.errors).toEqual({
      knowledge_bases: "ghost is not a knowledge base registered here.",
    });
    expect(read.values).toEqual({});
  });

  it("falls back to a checked JSON box for a shape it has no control for", async () => {
    renderFields(
      graphOf(
        paramNode("budget", {
          type: "object",
          properties: { cap: { type: "number" } },
          required: ["cap"],
        }),
      ),
    );

    const box = screen.getByLabelText("budget");
    expect(box.tagName).toBe("TEXTAREA");
    await userEvent.type(box, "{{ not json");
    expect(screen.getByText("This is not valid JSON.")).toBeInTheDocument();

    await userEvent.clear(box);
    await userEvent.type(box, '{{"cap": 4}');
    expect(values()).toEqual({ budget: { cap: 4 } });
  });

  it("never stringifies an object, and never sends the text a person typed as the value", async () => {
    renderFields(graphOf(paramNode("budget", { type: "object" })));

    await userEvent.type(screen.getByLabelText("budget"), '{{"cap": 4}');
    const read = values();
    expect(read).toEqual({ budget: { cap: 4 } });
    expect(JSON.stringify(read)).not.toContain('"{');
  });

  it("collects a decision brief as native JSON, under the author's own labels only", async () => {
    renderFields(
      graphOf(
        paramNode("question", { type: "string", minLength: 1 }),
        paramNode("knowledge_bases", {
          type: "array",
          items: { type: "string" },
          minItems: 1,
          maxItems: 3,
          uniqueItems: true,
        }),
        paramNode("project_context", { type: "string" }),
        paramNode("constraints", { type: "string" }, true),
      ),
      presentationOf([
        { key: "question", label: "What are you deciding?", control: "textarea" },
        { key: "knowledge_bases", label: "Knowledge bases", control: "knowledge-base" },
      ]),
      registered("research", "repo-docs"),
    );

    await userEvent.type(screen.getByLabelText("What are you deciding?"), "Which cache?");
    await userEvent.click(screen.getByRole("checkbox", { name: "research" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "repo-docs" }));
    await userEvent.type(screen.getByLabelText("project_context"), "payments");
    await userEvent.type(screen.getByLabelText("constraints"), "offline");

    expect(values()).toEqual({
      question: "Which cache?",
      knowledge_bases: ["research", "repo-docs"],
      project_context: "payments",
      constraints: "offline",
    });
    // The card named two inputs, so the other two keep their declared names: nothing here
    // invents a sentence for a parameter the author never described.
    expect(screen.getByText("project_context")).toBeInTheDocument();
    expect(screen.getByText("constraints")).toBeInTheDocument();
  });

  it("answers a schema that constrains nothing with a line of text, not JSON", () => {
    renderFields(graphOf(paramNode("note", { description: "anything at all" })));

    expect(screen.getByLabelText("note").tagName).toBe("INPUT");
  });

  it("keeps the JSON box for a compound shape that names no type of its own", () => {
    renderFields(graphOf(paramNode("budget", { properties: { cap: { type: "number" } } })));

    expect(screen.getByLabelText("budget").tagName).toBe("TEXTAREA");
  });

  it("tells assistive technology which fields must be answered", () => {
    renderFields(
      graphOf(
        paramNode("question", { type: "string" }),
        paramNode("constraints", { type: "string" }, true),
      ),
    );

    expect(screen.getByLabelText("question")).toHaveAttribute("aria-required", "true");
    expect(screen.getByLabelText("constraints")).toHaveAttribute("aria-required", "false");
  });

  it("points a control at its own description and its own refusal, in that order", async () => {
    const graph = graphOf(paramNode("question", { type: "string", minLength: 4 }, true));
    graph.nodes[0] = { ...graph.nodes[0], detail: "what you are deciding" };
    renderFields(graph);

    const box = screen.getByLabelText("question");
    const help = screen.getByText("what you are deciding");
    expect(box.getAttribute("aria-describedby")).toBe(help.id);

    await userEvent.type(box, "ab");
    const refusal = screen.getByText("Enter at least 4 characters.");
    expect(box.getAttribute("aria-describedby")).toBe(`${help.id} ${refusal.id}`);
    expect(box).toHaveAttribute("aria-invalid", "true");
  });

  it("names the group's own refusal on the group itself", async () => {
    renderFields(
      graphOf(paramNode("tags", { type: "array", items: { type: "string" }, minItems: 2 })),
    );

    await userEvent.type(screen.getByLabelText("tags 1"), "payments");
    const group = screen.getByRole("group");
    const refusal = screen.getByText("Choose at least 2.");
    expect(group.getAttribute("aria-describedby")).toBe(refusal.id);
  });

  it("says every field's sentence in the house's own punctuation", async () => {
    renderFields(
      graphOf(
        paramNode("question", { type: "string", minLength: 1 }),
        paramNode("budget", { type: "object" }),
      ),
    );

    await userEvent.type(screen.getByLabelText("budget"), "nope");
    for (const sentence of Object.values(
      JSON.parse(screen.getByTestId("errors").textContent ?? "{}") as Record<string, string>,
    )) {
      expect(sentence).toMatch(/^[\w\s,.?()-]*$/);
    }
  });
});
