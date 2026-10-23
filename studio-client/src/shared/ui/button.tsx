import type { ButtonHTMLAttributes, Ref } from "react";
import { cn } from "../lib/cn";

type Variant = "primary" | "secondary" | "danger";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  ref?: Ref<HTMLButtonElement>;
}

// Colors come from the consuming app's semantic aliases (its globals.css @theme block):
// ground/surface/well, line, label tiers, accent, danger pair. The primitives never name a
// literal palette color, so one app can be the dark factory floor and another plain light.
const VARIANT_CLASSES: Record<Variant, string> = {
  primary:
    "bg-label-1 text-surface hover:bg-label-1/85 disabled:bg-muted-soft disabled:text-label-3",
  secondary: "border border-line-strong bg-surface text-label-2 hover:bg-well disabled:opacity-50",
  danger:
    "border border-danger-ink/35 bg-surface text-danger-ink hover:bg-danger-soft disabled:opacity-50",
};

export function Button({ variant = "primary", className, type, ref, ...rest }: Props) {
  return (
    <button
      ref={ref}
      type={type ?? "button"}
      className={cn(
        "rounded-full px-4 py-1.5 text-sm font-semibold transition-colors disabled:cursor-not-allowed",
        VARIANT_CLASSES[variant],
        className,
      )}
      {...rest}
    />
  );
}
