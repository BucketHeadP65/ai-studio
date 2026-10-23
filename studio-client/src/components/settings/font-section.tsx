"use client";

import { useEffect, useState } from "react";
import type { ReactElement } from "react";

import { cn } from "@/shared/lib/cn";

import { SettingsSectionCard } from "./settings-section-card";

// The typeface presets, each a self-hosted face the root layout loads at the full 400 to 700
// weight range, keyed by data-font in globals.css. The choice persists in localStorage and the
// root layout's pre-paint script stamps it on <html> beside the theme, so a reload never
// flashes the default face. "grotesk" is the default and stamps nothing.
//
// The menu is a compact grid, not a card stack: each cell renders its own name in its own
// face at the app's weight, so the cell is the preview and fifteen options cost five rows.

export const FONT_STORAGE_KEY = "studio-font";

type FontValue =
  | "grotesk"
  | "inter"
  | "manrope"
  | "plex"
  | "outfit"
  | "dm-sans"
  | "geist"
  | "sora"
  | "jakarta"
  | "work-sans"
  | "figtree"
  | "hanken"
  | "instrument"
  | "lexend"
  | "archivo";

interface FontOption {
  value: FontValue;
  label: string;
  // The face's own CSS variable, stable regardless of which face is active.
  cssVar: string;
  note: string;
}

const FONT_OPTIONS: readonly FontOption[] = [
  {
    value: "grotesk",
    label: "Space Grotesk",
    cssVar: "--font-grotesk",
    note: "The house voice, geometric with a technical edge.",
  },
  {
    value: "inter",
    label: "Inter",
    cssVar: "--font-inter",
    note: "The neutral workhorse, drawn for screens.",
  },
  {
    value: "manrope",
    label: "Manrope",
    cssVar: "--font-manrope",
    note: "Rounded geometry, soft but firm at medium weight.",
  },
  {
    value: "plex",
    label: "IBM Plex Sans",
    cssVar: "--font-plex",
    note: "The IBM engineering sans, sturdy at every weight.",
  },
  {
    value: "outfit",
    label: "Outfit",
    cssVar: "--font-outfit",
    note: "Clean display geometry, confident in headings.",
  },
  {
    value: "dm-sans",
    label: "DM Sans",
    cssVar: "--font-dm-sans",
    note: "Low contrast geometric, sibling of DM Mono.",
  },
  {
    value: "geist",
    label: "Geist",
    cssVar: "--font-geist",
    note: "Cool and precise, drawn for developer tools.",
  },
  {
    value: "sora",
    label: "Sora",
    cssVar: "--font-sora",
    note: "Wide geometric with a quiet future lean.",
  },
  {
    value: "jakarta",
    label: "Plus Jakarta Sans",
    cssVar: "--font-jakarta",
    note: "Friendly geometric, generous counters.",
  },
  {
    value: "work-sans",
    label: "Work Sans",
    cssVar: "--font-work-sans",
    note: "Plainspoken grotesque, tuned for interfaces.",
  },
  {
    value: "figtree",
    label: "Figtree",
    cssVar: "--font-figtree",
    note: "Round and warm without going soft.",
  },
  {
    value: "hanken",
    label: "Hanken Grotesk",
    cssVar: "--font-hanken",
    note: "A humanist grotesk, calm at body sizes.",
  },
  {
    value: "instrument",
    label: "Instrument Sans",
    cssVar: "--font-instrument",
    note: "Compact neo grotesque with crisp terminals.",
  },
  {
    value: "lexend",
    label: "Lexend",
    cssVar: "--font-lexend",
    note: "Drawn for reading speed, open and even.",
  },
  {
    value: "archivo",
    label: "Archivo",
    cssVar: "--font-archivo",
    note: "Sturdy grotesque, strong in dense tables.",
  },
];

function isFontValue(value: string): value is FontValue {
  return FONT_OPTIONS.some((option) => option.value === value);
}

function apply(value: FontValue): void {
  window.localStorage.setItem(FONT_STORAGE_KEY, value);
  if (value === "grotesk") {
    delete document.documentElement.dataset.font;
  } else {
    document.documentElement.dataset.font = value;
  }
}

export function FontSection(): ReactElement {
  // Render the stamped value after mount; the server renders the default so hydration
  // never disagrees with markup, and the pre-paint script has already set the attribute.
  const [font, setFont] = useState<FontValue>("grotesk");

  useEffect(() => {
    const stored = window.localStorage.getItem(FONT_STORAGE_KEY);
    if (stored !== null && isFontValue(stored)) {
      setFont(stored);
    }
  }, []);

  function select(value: FontValue): void {
    setFont(value);
    apply(value);
  }

  return (
    <SettingsSectionCard
      title="Type"
      explainer="The typeface every screen uses, applied the moment you pick it. Each cell shows its own face, and every face carries real medium and bold weights."
    >
      <fieldset className="p-4">
        <legend className="sr-only">Type</legend>
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
          {FONT_OPTIONS.map((option) => {
            const checked = font === option.value;
            return (
              <label
                key={option.value}
                title={option.note}
                style={{ fontFamily: `var(${option.cssVar}), sans-serif` }}
                className={cn(
                  "flex cursor-pointer items-center justify-center rounded-lg border px-2 py-2.5 text-center font-medium text-body transition-colors has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-accent",
                  checked
                    ? "border-label-1 bg-well text-label-1"
                    : "border-line text-label-2 hover:border-line-strong hover:bg-well hover:text-label-1",
                )}
              >
                <input
                  type="radio"
                  name="font"
                  value={option.value}
                  checked={checked}
                  onChange={() => select(option.value)}
                  className="sr-only"
                />
                {option.label}
              </label>
            );
          })}
        </div>
      </fieldset>
    </SettingsSectionCard>
  );
}
