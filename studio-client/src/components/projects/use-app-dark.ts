"use client";

import { useEffect, useState } from "react";

/**
 * Whether an `html[data-theme]` value is one of the two the dashboard paints dark.
 * "paper" clears the attribute entirely, and any other or missing value reads as light.
 */
export function isDarkAppTheme(htmlTheme: string | undefined): boolean {
  return htmlTheme === "factory" || htmlTheme === "graphite";
}

/**
 * Tracks the app shell's dark-ness by watching `<html data-theme>`. The project
 * workspace's prose surfaces key their own dark palette to a `data-theme` attribute
 * scoped to their own container, so this hook is the only way the two ever agree.
 */
export function useAppIsDark(): boolean {
  const [isDark, setIsDark] = useState(() =>
    typeof document === "undefined"
      ? false
      : isDarkAppTheme(document.documentElement.dataset.theme),
  );

  useEffect(() => {
    const root = document.documentElement;
    function sync(): void {
      setIsDark(isDarkAppTheme(root.dataset.theme));
    }
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);

  return isDark;
}

/** Stamps or clears the `data-theme="dark"` attribute a `.projects-prose` root keys its dark palette to. */
export function stampProseTheme(element: HTMLElement | null | undefined, isDark: boolean): void {
  if (!element) return;
  if (isDark) {
    element.dataset.theme = "dark";
  } else {
    delete element.dataset.theme;
  }
}
