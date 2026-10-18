"use client";

import { useEffect, useState } from "react";
import type { ReactElement } from "react";

import { RadioCardGroup, type RadioCardOption } from "@/components/workflows/radio-card";

import { SettingsSectionCard } from "./settings-section-card";

// The theme presets, each is one palette block in globals.css keyed by data-theme.
// The choice persists in localStorage and a pre-paint script in the root layout stamps
// it on <html> before first paint, so a reload never flashes the wrong skin.
// "paper" stays the stored value for the default so an already-persisted
// choice keeps resolving; its skin is the Wire light palette.

export const THEME_STORAGE_KEY = "studio-theme";

type ThemeValue = "paper" | "factory" | "graphite";

const THEME_OPTIONS: readonly RadioCardOption<ThemeValue>[] = [
  {
    value: "paper",
    label: "Wire",
    description: "The light theme, warm neutrals and a vermilion signal.",
  },
  {
    value: "factory",
    label: "Factory",
    description: "The dark industrial theme, slate tones and a cyan glow.",
  },
  {
    value: "graphite",
    label: "Graphite",
    description: "The restrained dark theme, neutral grays and a calm blue accent.",
  },
];

function isThemeValue(value: string): value is ThemeValue {
  return THEME_OPTIONS.some((option) => option.value === value);
}

function apply(value: ThemeValue): void {
  window.localStorage.setItem(THEME_STORAGE_KEY, value);
  if (value === "paper") {
    delete document.documentElement.dataset.theme;
  } else {
    document.documentElement.dataset.theme = value;
  }
}

export function AppearanceSection(): ReactElement {
  // Render the stamped value after mount; the server renders the default so hydration
  // never disagrees with markup, and the pre-paint script has already set the attribute.
  const [theme, setTheme] = useState<ThemeValue>("paper");

  useEffect(() => {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored !== null && isThemeValue(stored)) {
      setTheme(stored);
    }
  }, []);

  function select(value: ThemeValue): void {
    setTheme(value);
    apply(value);
  }

  return (
    <SettingsSectionCard title="Appearance" explainer="The theme every screen uses.">
      <div className="p-4">
        <RadioCardGroup
          name="theme"
          legend="Appearance"
          value={theme}
          onChange={select}
          options={THEME_OPTIONS}
        />
      </div>
    </SettingsSectionCard>
  );
}
