"use client";

import { cn } from "@/shared/lib/cn";
import type { ReactElement, Ref } from "react";

// One option in a labelled group of radio cards: a filled dot when selected, the option's own
// name, and an optional one-line description below it. Every option is a real radio input, so
// arrow keys move the selection and it announces to assistive tech as a radio; the visible name
// is pinned with `aria-label` so an option's description never joins its accessible name. The
// input stays in the tab order but is visually hidden, so `has-[:focus-visible]` puts the ring
// on the card itself whenever that hidden input holds keyboard focus, keeping tabbing visible.

export interface RadioCardOption<Value extends string> {
  value: Value;
  label: string;
  // What a segment shows in the segmented variant, when the label is too long for one row of
  // three. Must be contained in `label`, which stays the accessible name.
  short?: string;
  description?: string;
  disabled?: boolean;
}

export interface RadioCardGroupProps<Value extends string> {
  name: string;
  legend: string;
  value: Value;
  onChange: (value: Value) => void;
  options: readonly RadioCardOption<Value>[];
  disabled?: boolean;
  firstOptionRef?: Ref<HTMLInputElement>;
  // `cards` stacks one card per option with its description under its name. `segmented` puts
  // every option on one pill row and shows only the chosen option's description beneath it,
  // the others staying readable to assistive tech, for a rail that cannot spend the height.
  variant?: "cards" | "segmented";
}

export function RadioCardGroup<Value extends string>({
  name,
  legend,
  value,
  onChange,
  options,
  disabled = false,
  firstOptionRef,
  variant = "cards",
}: RadioCardGroupProps<Value>): ReactElement {
  if (variant === "segmented") {
    const chosen = options.find((option) => option.value === value);
    return (
      <fieldset className="flex flex-col gap-1.5">
        <legend className="font-semibold text-label-3 text-micro uppercase tracking-caps">
          {legend}
        </legend>
        <div className="flex w-fit max-w-full rounded-full border border-line bg-well p-0.5">
          {options.map((option, index) => {
            const checked = option.value === value;
            const optionDisabled = disabled || (option.disabled ?? false);
            const descriptionId = option.description
              ? `${name}-${option.value}-description`
              : undefined;
            return (
              <label
                key={option.value}
                title={option.description}
                className={cn(
                  "flex h-7 items-center whitespace-nowrap rounded-full px-2.5 text-caption transition-colors",
                  "has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-accent has-[:focus-visible]:ring-offset-1 has-[:focus-visible]:ring-offset-well",
                  checked
                    ? "bg-label-1 font-semibold text-surface shadow-sm"
                    : "text-label-2 hover:text-label-1",
                  optionDisabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
                )}
              >
                <input
                  ref={index === 0 ? firstOptionRef : undefined}
                  type="radio"
                  name={name}
                  value={option.value}
                  checked={checked}
                  disabled={optionDisabled}
                  aria-label={option.label}
                  aria-describedby={descriptionId}
                  onChange={() => onChange(option.value)}
                  className="sr-only"
                />
                <span>{option.short ?? option.label}</span>
                {option.description && !checked ? (
                  <span id={descriptionId} className="sr-only">
                    {option.description}
                  </span>
                ) : null}
              </label>
            );
          })}
        </div>
        {chosen?.description ? (
          <p
            id={`${name}-${chosen.value}-description`}
            className="text-label-3 text-micro leading-relaxed"
          >
            {chosen.description}
          </p>
        ) : null}
      </fieldset>
    );
  }
  return (
    <fieldset className="flex flex-col gap-1.5">
      <legend className="font-semibold text-label-3 text-micro uppercase tracking-caps">
        {legend}
      </legend>
      {options.map((option, index) => {
        const checked = option.value === value;
        const optionDisabled = disabled || (option.disabled ?? false);
        const descriptionId = option.description
          ? `${name}-${option.value}-description`
          : undefined;
        return (
          <label
            key={option.value}
            className={cn(
              "flex items-start gap-2.5 rounded-md border px-3 py-2.5 transition-colors",
              "has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-accent has-[:focus-visible]:ring-offset-2 has-[:focus-visible]:ring-offset-surface",
              checked ? "border-label-1 bg-well" : "border-line",
              optionDisabled ? "cursor-not-allowed opacity-60" : "cursor-pointer",
            )}
          >
            <input
              ref={index === 0 ? firstOptionRef : undefined}
              type="radio"
              name={name}
              value={option.value}
              checked={checked}
              disabled={optionDisabled}
              aria-label={option.label}
              aria-describedby={descriptionId}
              onChange={() => onChange(option.value)}
              className="sr-only"
            />
            <span
              aria-hidden="true"
              className={cn(
                "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border-2",
                checked ? "border-label-1" : "border-line-strong",
              )}
            >
              {checked ? <span className="h-2 w-2 rounded-full bg-label-1" /> : null}
            </span>
            <span className="flex min-w-0 flex-col gap-0.5">
              <span
                className={cn(
                  "text-caption",
                  checked ? "font-semibold text-label-1" : "font-medium text-label-2",
                )}
              >
                {option.label}
              </span>
              {option.description ? (
                <span id={descriptionId} className="text-label-3 text-micro leading-relaxed">
                  {option.description}
                </span>
              ) : null}
            </span>
          </label>
        );
      })}
    </fieldset>
  );
}
