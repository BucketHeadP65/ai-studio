"use client";

import { Input } from "@/shared/ui/input";
import { Select } from "@/shared/ui/select";
import { Textarea } from "@/shared/ui/textarea";
import type { ReactElement } from "react";
import { useId } from "react";

import { z } from "zod";

import {
  type InputControl,
  type JsonSchema,
  JsonValueSchema,
  type JsonValue,
  type WorkflowGraph,
  type WorkflowPresentation,
} from "@/api/studio-types";

// The control registry: one declared parameter, drawn as the control a person answers it
// through, and read back as the native JSON value a launch sends.
//
// Two things decide the control. The parameter's own JSON Schema says what the value IS, and the
// saved workflow's presentation says how the author wants it asked for. The schema wins on shape
// (an array is never a text box), the author's hint refines what the schema leaves open (a long
// string in a multiline box, a list of names through the knowledge-base selector). A shape with
// no control of its own is answered as checked JSON, never as the text of a value.
//
// Nothing here knows a knowledge base's name, a page type, or any workflow's own wording: every
// label comes from the card, every bound from the schema, and the registered knowledge bases
// arrive as a prop.

/** Which control one field is answered through, after schema and presentation are both read. */
export type ParameterControl =
  | "text"
  | "textarea"
  | "number"
  | "checkbox"
  | "select"
  | "string-list"
  | "knowledge-base"
  | "knowledge-base-one"
  | "json";

export interface ParameterField {
  /** The parameter's declared name, and the key its value rides under. */
  key: string;
  /** The author's own label, or the declared name when the card names none. */
  label: string;
  /** The parameter's description, shown under the control. Empty is a real state. */
  help: string;
  required: boolean;
  schema: JsonSchema;
  control: ParameterControl;
}

/** What a field holds while it is being answered: text as typed, a flag, or a list of entries. */
export type DraftValue = string | boolean | readonly string[];
export type ParameterDraft = Readonly<Record<string, DraftValue>>;

/**
 * What this deployment has said about its knowledge bases, for the one control that selects them.
 *
 * `names` is `null` until the registry has answered: unknown, which is never the same as none. A
 * name already in the draft is left alone while it is unknown, because nothing has said it is wrong.
 */
export interface KnowledgeBaseRegistry {
  names: readonly string[] | null;
  /** Why the registry could not be read, or `null`. */
  error: string | null;
}

/** The registry as it reads before anything has asked for it. */
export const UNKNOWN_KNOWLEDGE_BASES: KnowledgeBaseRegistry = { names: null, error: null };

export interface ParametersRead {
  /** Every field that answered, as the native JSON value it answered with. */
  values: Record<string, JsonValue>;
  /** One sentence per field that cannot answer yet, keyed by the field's own key. */
  errors: Record<string, string>;
}

// -- reading a schema ----------------------------------------------------------------

function schemaString(schema: JsonSchema, key: string): string | null {
  const value = schema[key];
  return typeof value === "string" ? value : null;
}

function schemaNumber(schema: JsonSchema, key: string): number | null {
  const value = schema[key];
  return typeof value === "number" ? value : null;
}

function schemaObject(schema: JsonSchema, key: string): JsonSchema | null {
  const value = schema[key];
  if (Array.isArray(value)) return null;
  const parsed = z.record(z.string(), z.unknown()).safeParse(value);
  return parsed.success ? parsed.data : null;
}

function enumMembers(schema: JsonSchema): JsonValue[] | null {
  const members = schema.enum;
  if (!Array.isArray(members)) return null;
  const parsed = members.map((member) => JsonValueSchema.safeParse(member));
  return parsed.every((one) => one.success) ? parsed.map((one) => one.data) : null;
}

function itemsSchema(schema: JsonSchema): JsonSchema {
  return schemaObject(schema, "items") ?? {};
}

function holdsStrings(schema: JsonSchema): boolean {
  return schemaString(itemsSchema(schema), "type") === "string";
}

// The keys that make a schema compound even with no `type` of its own. A schema carrying one of
// these describes a structure, and a structure is answered as JSON; a schema carrying none of them
// constrains nothing a control can read, and the plainest answer to that is a line of text.
const COMPOUND_KEYS = [
  "properties",
  "patternProperties",
  "additionalProperties",
  "items",
  "prefixItems",
  "anyOf",
  "oneOf",
  "allOf",
  "not",
  "$ref",
] as const;

function compound(schema: JsonSchema): boolean {
  return COMPOUND_KEYS.some((key) => key in schema);
}

function controlFor(schema: JsonSchema, hint: InputControl | null): ParameterControl {
  const type = schemaString(schema, "type");
  if (hint === "json") return "json";
  if (hint === "knowledge-base") {
    if (type === "array" && holdsStrings(schema)) return "knowledge-base";
    if (type === "string") return "knowledge-base-one";
  }
  if ((hint === "text" || hint === "textarea") && (type === "string" || type === null)) {
    return hint === "textarea" ? "textarea" : "text";
  }
  if (enumMembers(schema) !== null) return "select";
  if (type === "boolean") return "checkbox";
  if (type === "number" || type === "integer") return "number";
  if (type === "string") return "text";
  if (type === "array" && holdsStrings(schema)) return "string-list";
  if (type === null && !compound(schema)) return "text";
  return "json";
}

/**
 * One field per declared parameter, in the order the workflow declares them.
 *
 * The card's `inputs` supply a label and a control for the parameters the author described; every
 * other parameter keeps its declared name and is read from its schema alone.
 */
export function parameterFields(
  graph: WorkflowGraph,
  presentation: WorkflowPresentation | null | undefined,
): ParameterField[] {
  const described = new Map((presentation?.inputs ?? []).map((input) => [input.key, input]));
  return graph.nodes
    .filter((node) => node.kind === "param")
    .map((node) => {
      const words = described.get(node.id);
      const schema = node.output_schema ?? {};
      return {
        key: node.id,
        label: words?.label ?? node.id,
        help: node.detail,
        required: node.required,
        schema,
        control: controlFor(schema, words?.control ?? null),
      };
    });
}

/** Whether any field here is answered through the knowledge-base selector, the only one that needs the registry. */
export function usesKnowledgeBases(fields: readonly ParameterField[]): boolean {
  return fields.some(
    (field) => field.control === "knowledge-base" || field.control === "knowledge-base-one",
  );
}

// -- the draft a person fills --------------------------------------------------------

function optionKey(member: JsonValue): string {
  return typeof member === "string" ? member : JSON.stringify(member);
}

function listControl(control: ParameterControl): boolean {
  return control === "string-list" || control === "knowledge-base";
}

function initialValue(field: ParameterField): DraftValue {
  const given = field.schema.default;
  if (listControl(field.control)) {
    return Array.isArray(given)
      ? given.filter((entry): entry is string => typeof entry === "string")
      : [];
  }
  if (field.control === "checkbox") return given === true;
  if (given === undefined) return "";
  if (typeof given === "string") return given;
  if (field.control === "json") return JSON.stringify(given, null, 2);
  if (typeof given === "number" || typeof given === "boolean") return String(given);
  return "";
}

/** A fresh draft: every field empty, or holding the default its schema declares. */
export function emptyDraft(fields: readonly ParameterField[]): ParameterDraft {
  return Object.fromEntries(fields.map((field) => [field.key, initialValue(field)]));
}

// One value a run was given, back in the control it was answered through. A value whose shape the
// control cannot hold falls back to the empty answer rather than being shown as something else:
// the point of reopening is to edit what was actually sent, and a value drawn wrong is not that.
// A list is kept whole or refused whole, never quietly shortened to the entries that happen to fit.
function heldValue(field: ParameterField, value: JsonValue | undefined): DraftValue {
  if (value === undefined) return initialValue(field);
  if (listControl(field.control)) {
    if (!Array.isArray(value)) return initialValue(field);
    const entries = value.filter((entry): entry is string => typeof entry === "string");
    return entries.length === value.length ? entries : initialValue(field);
  }
  if (field.control === "checkbox") return value === true;
  if (field.control === "json") return JSON.stringify(value, null, 2);
  if (field.control === "select") return optionKey(value);
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return initialValue(field);
}

/**
 * A draft holding the values one run was actually given, ready to be edited and sent again.
 *
 * What a rerun opens with. A parameter the run carried no value for opens as a fresh field, and a
 * value the current form has no field for is dropped: the questions are the workflow's as it
 * stands now, never as the older run happened to be asked them.
 */
export function draftFrom(
  fields: readonly ParameterField[],
  values: Readonly<Record<string, JsonValue>>,
): ParameterDraft {
  return Object.fromEntries(fields.map((field) => [field.key, heldValue(field, values[field.key])]));
}

function textOf(draft: ParameterDraft, key: string): string {
  const held = draft[key];
  return typeof held === "string" ? held : "";
}

function entriesOf(draft: ParameterDraft, key: string): readonly string[] {
  const held = draft[key];
  return Array.isArray(held) ? held : [];
}

function flagOf(draft: ParameterDraft, key: string): boolean {
  return draft[key] === true;
}

// -- reading the draft back as values -------------------------------------------------

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

interface Answer {
  value?: JsonValue;
  error?: string;
}

function readString(field: ParameterField, text: string): Answer {
  const min = schemaNumber(field.schema, "minLength");
  const max = schemaNumber(field.schema, "maxLength");
  if (min !== null && text.length < min) {
    return { error: `Enter at least ${plural(min, "character")}.` };
  }
  if (max !== null && text.length > max) {
    return { error: `Enter ${plural(max, "character")} or fewer.` };
  }
  return { value: text };
}

function readNumber(field: ParameterField, text: string): Answer {
  const parsed = Number(text);
  if (text.trim() === "" || !Number.isFinite(parsed)) return { error: "Enter a number." };
  if (schemaString(field.schema, "type") === "integer" && !Number.isInteger(parsed)) {
    return { error: "Enter a whole number." };
  }
  const min = schemaNumber(field.schema, "minimum");
  const max = schemaNumber(field.schema, "maximum");
  if (min !== null && parsed < min) return { error: `Enter ${min} or more.` };
  if (max !== null && parsed > max) return { error: `Enter ${max} or less.` };
  return { value: parsed };
}

function readSelect(field: ParameterField, text: string): Answer {
  const member = (enumMembers(field.schema) ?? []).find((one) => optionKey(one) === text);
  return member === undefined ? { error: "Choose one of the offered values." } : { value: member };
}

function readList(field: ParameterField, entries: readonly string[]): Answer {
  const kept = entries.map((entry) => entry.trim()).filter((entry) => entry !== "");
  const min = schemaNumber(field.schema, "minItems");
  const max = schemaNumber(field.schema, "maxItems");
  if (min !== null && kept.length < min) return { error: `Choose at least ${min}.` };
  if (max !== null && kept.length > max) return { error: `Choose ${max} or fewer.` };
  if (field.schema.uniqueItems === true && new Set(kept).size !== kept.length) {
    return { error: "Every entry must be different." };
  }
  const itemMin = schemaNumber(itemsSchema(field.schema), "minLength");
  if (itemMin !== null && kept.some((entry) => entry.length < itemMin)) {
    return { error: `Enter at least ${plural(itemMin, "character")} in every entry.` };
  }
  return { value: kept };
}

function readKnowledgeBases(
  field: ParameterField,
  chosen: readonly string[],
  registered: readonly string[] | null,
): Answer {
  const stranger = registered === null ? undefined : chosen.find((name) => !registered.includes(name));
  if (stranger !== undefined) {
    return { error: `${stranger} is not a knowledge base registered here.` };
  }
  return readList(field, chosen);
}

function readJson(text: string): Answer {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { error: "This is not valid JSON." };
  }
  const checked = JsonValueSchema.safeParse(parsed);
  return checked.success ? { value: checked.data } : { error: "This is not valid JSON." };
}

function blank(field: ParameterField, draft: ParameterDraft): boolean {
  if (field.control === "checkbox") return false;
  if (listControl(field.control)) {
    return entriesOf(draft, field.key).every((entry) => entry.trim() === "");
  }
  return textOf(draft, field.key).trim() === "";
}

function answer(
  field: ParameterField,
  draft: ParameterDraft,
  registered: readonly string[] | null,
): Answer {
  switch (field.control) {
    case "checkbox":
      return { value: flagOf(draft, field.key) };
    case "number":
      return readNumber(field, textOf(draft, field.key));
    case "select":
      return readSelect(field, textOf(draft, field.key));
    case "string-list":
      return readList(field, entriesOf(draft, field.key));
    case "knowledge-base":
      return readKnowledgeBases(field, entriesOf(draft, field.key), registered);
    case "knowledge-base-one": {
      const name = textOf(draft, field.key);
      return registered === null || registered.includes(name)
        ? { value: name }
        : { error: `${name} is not a knowledge base registered here.` };
    }
    case "json":
      return readJson(textOf(draft, field.key));
    default:
      return readString(field, textOf(draft, field.key));
  }
}

/**
 * The draft read as the arguments a launch carries, and the sentence every unanswerable field says.
 *
 * An omitted optional parameter is left out rather than sent empty. Values are native: an array
 * stays an array, a boolean a boolean, and an object the object it parsed to.
 */
export function readParameters(
  fields: readonly ParameterField[],
  draft: ParameterDraft,
  options: { knowledgeBases: KnowledgeBaseRegistry },
): ParametersRead {
  const values: Record<string, JsonValue> = {};
  const errors: Record<string, string> = {};
  for (const field of fields) {
    if (blank(field, draft)) {
      if (field.required) errors[field.key] = "This is required.";
      continue;
    }
    const read = answer(field, draft, options.knowledgeBases.names);
    if (read.error !== undefined) errors[field.key] = read.error;
    else if (read.value !== undefined) values[field.key] = read.value;
  }
  return { values, errors };
}

// -- drawing the fields ---------------------------------------------------------------

const FIELD_LABEL = "text-micro font-semibold uppercase tracking-caps text-label-3";

export interface ParameterFieldsProps {
  fields: readonly ParameterField[];
  draft: ParameterDraft;
  /** One sentence per field to show under it. A field with no entry here shows none. */
  errors: Readonly<Record<string, string>>;
  /** What this deployment has said about its knowledge bases, for the selector only. */
  knowledgeBases: KnowledgeBaseRegistry;
  onChange: (key: string, value: DraftValue) => void;
  disabled?: boolean;
}

function KnowledgeBaseChoice({
  field,
  chosen,
  registry,
  disabled,
  onChange,
}: {
  field: ParameterField;
  chosen: readonly string[];
  registry: KnowledgeBaseRegistry;
  disabled: boolean;
  onChange: (value: DraftValue) => void;
}): ReactElement {
  if (registry.error !== null) {
    return (
      <p role="alert" className="text-caption text-danger-ink">
        {registry.error}
      </p>
    );
  }
  if (registry.names === null) {
    return <p className="text-caption text-label-3">Reading the knowledge bases.</p>;
  }
  if (registry.names.length === 0) {
    return <p className="text-caption text-label-3">No knowledge base is registered here yet.</p>;
  }
  const single = field.control === "knowledge-base-one";
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1.5">
      {registry.names.map((name) => (
        <label key={name} className="flex items-center gap-2 text-body text-label-2">
          <input
            type="checkbox"
            checked={chosen.includes(name)}
            disabled={disabled}
            onChange={(event) => {
              if (single) {
                onChange(event.target.checked ? name : "");
                return;
              }
              onChange(
                event.target.checked
                  ? [...chosen, name]
                  : chosen.filter((picked) => picked !== name),
              );
            }}
          />
          {name}
        </label>
      ))}
    </div>
  );
}

function StringList({
  field,
  entries,
  disabled,
  onChange,
}: {
  field: ParameterField;
  entries: readonly string[];
  disabled: boolean;
  onChange: (value: DraftValue) => void;
}): ReactElement {
  const rows = entries.length === 0 ? [""] : entries;
  const max = schemaNumber(field.schema, "maxItems");
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((entry, index) => (
        // The rows are positional: an entry has no identity of its own beyond where it sits.
        <div key={index} className="flex items-center gap-1.5">
          <Input
            aria-label={`${field.label} ${index + 1}`}
            value={entry}
            disabled={disabled}
            onChange={(event) =>
              onChange(rows.map((row, at) => (at === index ? event.target.value : row)))
            }
          />
          {rows.length > 1 ? (
            <button
              type="button"
              aria-label={`Remove ${field.label} ${index + 1}`}
              disabled={disabled}
              onClick={() => onChange(rows.filter((_, at) => at !== index))}
              className="shrink-0 rounded-md border border-line px-2 py-1 text-caption text-label-2 hover:border-line-strong"
            >
              Remove
            </button>
          ) : null}
        </div>
      ))}
      {max === null || rows.length < max ? (
        <button
          type="button"
          aria-label={`Add to ${field.label}`}
          disabled={disabled}
          onClick={() => onChange([...rows, ""])}
          className="self-start rounded-md border border-line px-2 py-1 text-caption text-label-2 hover:border-line-strong"
        >
          Add another
        </button>
      ) : null}
    </div>
  );
}

function OneField({
  field,
  id,
  draft,
  error,
  knowledgeBases,
  disabled,
  onChange,
}: {
  field: ParameterField;
  id: string;
  draft: ParameterDraft;
  error: string | undefined;
  knowledgeBases: KnowledgeBaseRegistry;
  disabled: boolean;
  onChange: (value: DraftValue) => void;
}): ReactElement {
  const errorId = error === undefined ? undefined : `${id}-error`;
  const helpId = field.help === "" ? undefined : `${id}-help`;
  // The description and the refusal both belong to the control, in reading order.
  const describedBy = [helpId, errorId].filter((one) => one !== undefined).join(" ") || undefined;
  const shared = {
    id,
    disabled,
    "aria-required": field.required,
    "aria-invalid": error !== undefined,
    "aria-describedby": describedBy,
  };

  function control(): ReactElement {
    switch (field.control) {
      case "textarea":
        return (
          <Textarea
            {...shared}
            rows={4}
            value={textOf(draft, field.key)}
            onChange={(event) => onChange(event.target.value)}
          />
        );
      case "json":
        return (
          <Textarea
            {...shared}
            rows={5}
            spellCheck={false}
            value={textOf(draft, field.key)}
            onChange={(event) => onChange(event.target.value)}
            className="font-mono"
          />
        );
      case "number":
        return (
          <Input
            {...shared}
            type="number"
            inputMode="decimal"
            value={textOf(draft, field.key)}
            onChange={(event) => onChange(event.target.value)}
          />
        );
      case "checkbox":
        return (
          <input
            {...shared}
            type="checkbox"
            checked={flagOf(draft, field.key)}
            onChange={(event) => onChange(event.target.checked)}
          />
        );
      case "select":
        return (
          <Select
            {...shared}
            value={textOf(draft, field.key)}
            onChange={(event) => onChange(event.target.value)}
          >
            <option value="">Choose one</option>
            {(enumMembers(field.schema) ?? []).map((member) => (
              <option key={optionKey(member)} value={optionKey(member)}>
                {optionKey(member)}
              </option>
            ))}
          </Select>
        );
      default:
        return (
          <Input
            {...shared}
            value={textOf(draft, field.key)}
            onChange={(event) => onChange(event.target.value)}
          />
        );
    }
  }

  // A list and the knowledge-base selector are several controls under one name, so they are
  // grouped rather than labelled: a `label` can only name one box.
  if (listControl(field.control) || field.control === "knowledge-base-one") {
    return (
      // ARIA supports `aria-required` on a control, not on a `group`, so a group carries its
      // requirement the way a reader meets it: the word beside its name, and the sentence below.
      <fieldset className="flex flex-col gap-1" aria-describedby={describedBy}>
        <legend className={FIELD_LABEL}>{field.label}</legend>
        {field.required ? null : <Optional />}
        {field.help ? (
          <p id={helpId} className="text-caption text-label-3">
            {field.help}
          </p>
        ) : null}
        {field.control === "string-list" ? (
          <StringList
            field={field}
            entries={entriesOf(draft, field.key)}
            disabled={disabled}
            onChange={onChange}
          />
        ) : (
          <KnowledgeBaseChoice
            field={field}
            chosen={
              field.control === "knowledge-base-one"
                ? [textOf(draft, field.key)]
                : entriesOf(draft, field.key)
            }
            registry={knowledgeBases}
            disabled={disabled}
            onChange={onChange}
          />
        )}
        {error === undefined ? null : (
          <p id={errorId} className="text-caption text-danger-ink">
            {error}
          </p>
        )}
      </fieldset>
    );
  }

  if (field.control === "checkbox") {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          {control()}
          <label htmlFor={id} className="text-body text-label-2">
            {field.label}
          </label>
          {field.required ? null : <Optional />}
        </div>
        {field.help ? (
          <p id={helpId} className="text-caption text-label-3">
            {field.help}
          </p>
        ) : null}
        {error === undefined ? null : (
          <p id={errorId} className="text-caption text-danger-ink">
            {error}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline gap-2">
        <label htmlFor={id} className={FIELD_LABEL}>
          {field.label}
        </label>
        {field.required ? null : <Optional />}
      </div>
      {field.help ? (
        <p id={helpId} className="text-caption text-label-3">
          {field.help}
        </p>
      ) : null}
      {control()}
      {error === undefined ? null : (
        <p id={errorId} className="text-caption text-danger-ink">
          {error}
        </p>
      )}
    </div>
  );
}

// The one word that says a parameter may be left out, kept out of every control's own name.
function Optional(): ReactElement {
  return <span className="text-label-3 text-micro">optional</span>;
}

export function ParameterFields({
  fields,
  draft,
  errors,
  knowledgeBases,
  onChange,
  disabled = false,
}: ParameterFieldsProps): ReactElement | null {
  const prefix = useId();
  if (fields.length === 0) return null;
  return (
    <div className="flex flex-col gap-3">
      {fields.map((field) => (
        <OneField
          key={field.key}
          field={field}
          id={`${prefix}-${field.key}`}
          draft={draft}
          error={errors[field.key]}
          knowledgeBases={knowledgeBases}
          disabled={disabled}
          onChange={(value) => onChange(field.key, value)}
        />
      ))}
    </div>
  );
}
