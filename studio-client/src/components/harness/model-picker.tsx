"use client";

import { useQuery } from "@tanstack/react-query";
import { cn } from "@/shared/lib/cn";
import { Card } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";
import { type KeyboardEvent, useEffect, useId, useState } from "react";

import { harnessApi } from "@/api/harness";
import type { ModelEntry } from "@/api/harness-types";

// The model combobox: search-as-you-type over the harness's model list (the owner's
// gateway route plus OpenRouter's catalogue), one tier at a time. The W3C APG combobox
// pattern, the same one KnowledgeBaseGraphView's page finder uses: focus never leaves the
// input, `aria-activedescendant` points at the arrowed-to option, Enter picks it,
// Escape dismisses the list without picking. The query is debounced so a fast typist
// does not fire one request per keystroke.

const DEBOUNCE_MS = 150;

export function formatMoney(value: string | null): string {
  return value === null ? "unknown" : `$${value} per M`;
}

export function formatContext(value: number | null): string {
  return value === null ? "unknown" : `${value.toLocaleString("en-US")} tokens`;
}

export interface ModelPickerProps {
  value: ModelEntry;
  onPick: (entry: ModelEntry) => void;
  label: string;
  disabled?: boolean;
}

export function ModelPicker({ value, onPick, label, disabled = false }: ModelPickerProps) {
  const [term, setTerm] = useState("");
  const [debounced, setDebounced] = useState("");
  const [cursor, setCursor] = useState(-1);
  const [dismissed, setDismissed] = useState(false);
  const [focused, setFocused] = useState(false);
  const listId = useId();

  useEffect(() => {
    const handle = setTimeout(() => setDebounced(term), DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [term]);

  const { data } = useQuery({
    queryKey: ["harness", "models", debounced],
    queryFn: () => harnessApi.models(debounced),
  });

  const options = data?.models ?? [];
  const open = !disabled && focused && !dismissed && options.length > 0;
  // Nothing is active until the reader arrows into the list, an option pre-selected
  // on the first keystroke is one an unlucky Enter commits to.
  const active = open && cursor >= 0 ? options[cursor] : undefined;

  function pick(entry: ModelEntry) {
    onPick(entry);
    setTerm("");
    setCursor(-1);
    setDismissed(true);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      setDismissed(true);
      return;
    }
    if (!open) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setCursor((current) => (current + 1) % options.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setCursor((current) => (current <= 0 ? options.length : current) - 1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const chosen = active ?? options[0];
      if (chosen) pick(chosen);
    }
  }

  return (
    <div className="relative">
      <Input
        role="combobox"
        aria-label={label}
        aria-expanded={open}
        aria-controls={listId}
        aria-activedescendant={active ? `model-option-${active.id}` : undefined}
        autoComplete="off"
        value={term}
        onChange={(event) => {
          setTerm(event.target.value);
          setCursor(-1);
          setDismissed(false);
        }}
        onFocus={() => {
          setFocused(true);
          setDismissed(false);
        }}
        onBlur={() => setFocused(false)}
        onKeyDown={onKeyDown}
        placeholder={value.name}
        disabled={disabled}
        className="w-full"
      />
      {open ? (
        <Card
          id={listId}
          // biome-ignore lint/a11y/useSemanticElements: a <select> cannot render this filtered, priced picker; this is the W3C APG combobox pattern, where the listbox is a div and the input keeps focus
          role="listbox"
          tabIndex={-1}
          aria-label={`${label} options`}
          className="absolute top-full left-0 z-20 mt-1 max-h-72 w-full min-w-72 overflow-y-auto py-1 shadow-lg"
        >
          {options.map((entry, index) => (
            <div
              key={entry.id}
              id={`model-option-${entry.id}`}
              // biome-ignore lint/a11y/useSemanticElements: <option> cannot hold the price and context detail line, and the APG pattern keeps options unfocusable, addressed by aria-activedescendant
              role="option"
              tabIndex={-1}
              aria-selected={index === cursor}
              // Keeps focus on the input through the click, so `onBlur` never fires
              // and closes the list before `onClick` below gets to run `pick`.
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => pick(entry)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") pick(entry);
              }}
              className={cn(
                "flex cursor-pointer flex-col gap-0.5 px-2.5 py-1.5",
                index === cursor && "bg-well",
              )}
            >
              <span className="truncate text-body text-label-2">{entry.name}</span>
              <span className="truncate text-caption text-label-3">
                {entry.id}, {entry.route}, {formatContext(entry.context_length)}, in{" "}
                {formatMoney(entry.price_in_per_m)}, out {formatMoney(entry.price_out_per_m)}
              </span>
            </div>
          ))}
        </Card>
      ) : null}
    </div>
  );
}
