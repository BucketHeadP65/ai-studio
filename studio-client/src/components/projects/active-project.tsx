"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";

import type { ReactNode } from "react";

const STORAGE_KEY = "engram.active-project-id";

interface ActiveProjectContextValue {
  activeProjectId: string | null;
  setActiveProjectId: (id: string | null) => void;
}

const ActiveProjectContext = createContext<ActiveProjectContextValue | null>(null);

function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStored(id: string | null): void {
  try {
    if (id === null) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, id);
    }
  } catch {
    // localStorage may be unavailable (private mode, disabled storage, quota) --
    // the active project stays in-memory for the rest of the session instead.
  }
}

// Which project the dropdown, station, and any consumer that mounts this
// provider agree is "open". Persisted in localStorage so a reload lands back
// on the same project; the read happens after mount (not in useState's lazy
// initializer) so the server-rendered pass and the first client render agree,
// and only then does the stored value replace it.
export function ActiveProjectProvider({ children }: { children: ReactNode }) {
  const [activeProjectId, setActiveProjectIdState] = useState<string | null>(null);

  useEffect(() => {
    setActiveProjectIdState(readStored());
  }, []);

  const value = useMemo<ActiveProjectContextValue>(
    () => ({
      activeProjectId,
      setActiveProjectId: (id: string | null) => {
        writeStored(id);
        setActiveProjectIdState(id);
      },
    }),
    [activeProjectId],
  );

  return <ActiveProjectContext.Provider value={value}>{children}</ActiveProjectContext.Provider>;
}

export function useActiveProject(): ActiveProjectContextValue {
  const context = useContext(ActiveProjectContext);
  if (context === null) {
    throw new Error("useActiveProject must be used within an ActiveProjectProvider");
  }
  return context;
}
