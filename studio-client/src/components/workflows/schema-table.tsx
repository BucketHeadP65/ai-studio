"use client";

import { cn } from "client-core/lib/cn";
import type { ReactElement } from "react";
import { useState } from "react";

import type { JsonSchema } from "@/api/studio-types";
import {
  isJsonRecord,
  resolveRef,
  rootFor,
  typeLabel,
  unwrapNullable,
} from "@/components/workflows/workflow-order";

// The one field-table reading every node's `output_schema` (and the run's own hand back) share:
// a caption sentence naming the shape, then a table of its fields when it has any -- an object's
// own properties, an array of objects' item fields, or an array of arrays of objects' innermost
// item fields. A bare primitive (or an array of one) gets the caption alone. A `Show as JSON`
// button always sits beside the caption, the one way to read a schema this table cannot yet
// shape into fields.

export interface SchemaTableProps {
  schema: JsonSchema | null;
  caption?: string;
}

interface FieldRow {
  name: string;
  type: string;
  required: boolean;
  allowedValues: string;
  nested: boolean;
}

function asTitle(schema: JsonSchema): string | null {
  const title = schema.title;
  return typeof title === "string" && title !== "" ? title : null;
}

function asDescription(schema: JsonSchema): string {
  const description = schema.description;
  return typeof description === "string" ? description : "";
}

function requiredNames(schema: JsonSchema): string[] {
  const required = schema.required;
  return Array.isArray(required)
    ? required.filter((name): name is string => typeof name === "string")
    : [];
}

// One level of an object schema's own field rows, sorted required first then by name. A property
// that is itself an object gets its own rows nested immediately after it, one level only -- its
// own properties never expand further.
function propertyRows(schema: JsonSchema, root: JsonSchema, nested: boolean): FieldRow[] {
  const properties = schema.properties;
  if (!isJsonRecord(properties)) return [];
  const required = requiredNames(schema);
  const names = Object.keys(properties).sort((a, b) => {
    const aRequired = required.includes(a);
    const bRequired = required.includes(b);
    if (aRequired !== bRequired) return aRequired ? -1 : 1;
    return a.localeCompare(b);
  });

  const rows: FieldRow[] = [];
  for (const name of names) {
    const raw = properties[name];
    if (!isJsonRecord(raw)) continue;
    const resolved = resolveRef(raw, root);
    // An optional literal arrives as `anyOf` of the enum and `null`; the values live one level in.
    const bare = unwrapNullable(resolved);
    const enumValues = Array.isArray(bare.enum) ? bare.enum : null;
    const allowedValues =
      enumValues !== null
        ? enumValues.map((value) => String(value)).join(", ")
        : asDescription(resolved);
    rows.push({
      name,
      type: typeLabel(resolved, root),
      required: required.includes(name),
      allowedValues,
      nested,
    });
    if (!nested && resolved.type === "object" && isJsonRecord(resolved.properties)) {
      rows.push(...propertyRows(resolved, root, true));
    }
  }
  return rows;
}

interface Shape {
  caption: string;
  rows: FieldRow[];
}

function describe(schema: JsonSchema, root: JsonSchema): Shape {
  const resolved = resolveRef(schema, root);

  if (resolved.type === "array") {
    const items = resolved.items;
    const itemsRoot = isJsonRecord(items) ? rootFor(items, root) : root;
    const resolvedItems = isJsonRecord(items) ? resolveRef(items, itemsRoot) : null;

    if (resolvedItems?.type === "object") {
      const itemsTitle = asTitle(resolvedItems) ?? "object";
      return {
        caption: `A list of ${itemsTitle}, each with these fields`,
        rows: propertyRows(resolvedItems, itemsRoot, false),
      };
    }

    if (resolvedItems?.type === "array") {
      const innerItems = resolvedItems.items;
      const innerRoot = isJsonRecord(innerItems) ? rootFor(innerItems, itemsRoot) : itemsRoot;
      const resolvedInner = isJsonRecord(innerItems) ? resolveRef(innerItems, innerRoot) : null;
      if (resolvedInner?.type === "object") {
        const innerTitle = asTitle(resolvedInner) ?? "object";
        return {
          caption: `A list per item, each a list of ${innerTitle} with these fields`,
          rows: propertyRows(resolvedInner, innerRoot, false),
        };
      }
    }

    return { caption: `A ${typeLabel(resolved, root)}`, rows: [] };
  }

  if (resolved.type === "object") {
    const title = asTitle(resolved);
    return {
      caption: title ? `One ${title} with these fields` : "An object with these fields",
      rows: propertyRows(resolved, root, false),
    };
  }

  return { caption: `A ${typeLabel(resolved, root)}`, rows: [] };
}

export function SchemaTable({ schema, caption }: SchemaTableProps): ReactElement {
  const [asJson, setAsJson] = useState(false);

  if (schema === null) {
    return (
      <p className="text-caption text-label-3">
        {caption ?? "The shape of this value is not known here."}
      </p>
    );
  }

  const shape = describe(schema, schema);
  const captionText = caption ?? shape.caption;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-caption text-label-3">{captionText}</p>
        <button
          type="button"
          onClick={() => setAsJson((value) => !value)}
          className="shrink-0 text-caption text-label-3 underline hover:text-label-1"
        >
          {asJson ? "Show as fields" : "Show as JSON"}
        </button>
      </div>
      {asJson ? (
        <pre className="overflow-x-auto whitespace-pre rounded-lg bg-well px-3 py-2 font-mono text-caption text-label-1">
          {JSON.stringify(schema, null, 2)}
        </pre>
      ) : null}
      {!asJson && shape.rows.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full text-caption">
            <thead>
              <tr className="border-line border-b bg-well">
                <th className="px-3 py-1.5 text-left font-semibold text-label-3 text-micro uppercase tracking-caps">
                  Field
                </th>
                <th className="px-3 py-1.5 text-left font-semibold text-label-3 text-micro uppercase tracking-caps">
                  Type
                </th>
                <th className="px-3 py-1.5 text-left font-semibold text-label-3 text-micro uppercase tracking-caps" />
                <th className="px-3 py-1.5 text-left font-semibold text-label-3 text-micro uppercase tracking-caps">
                  Allowed values
                </th>
              </tr>
            </thead>
            <tbody>
              {shape.rows.map((row) => (
                <tr
                  key={`${row.nested ? "nested" : "top"}-${row.name}`}
                  className="border-line border-b last:border-b-0"
                >
                  <td
                    className={cn(
                      "py-1.5 pr-3 font-mono text-label-1",
                      row.nested ? "pl-8" : "pl-3",
                    )}
                  >
                    {row.name}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-label-2">{row.type}</td>
                  <td className="px-3 py-1.5 text-label-3">
                    {row.required ? "required" : "optional"}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-label-2">{row.allowedValues}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
