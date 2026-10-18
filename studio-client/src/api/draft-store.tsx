"use client";

import {
  type ReactElement,
  type ReactNode,
  createContext,
  useContext,
  useMemo,
  useState,
} from "react";

import type { ChatReply } from "./studio-types";

// Holds the last settle payload from the authoring conversation so the
// workflow draft page can read it after a client-side navigation, without a
// round trip back to the agent. Session-lived only: a hard reload starts empty.
//
// `ownerKey` is the view that produced the reply (a session id, or "fresh" for
// the no-session view), stamped at settle time. A view-scoped consumer (the
// chat transcript) compares this against its own key before rendering the
// reply, so a draft settled in one session never wears another session's
// conversation. `/workflows/draft` has no view of its own, it reads whichever
// reply is stored regardless of owner.

export interface DraftReply {
  ownerKey: string;
  reply: ChatReply;
}

interface DraftStore {
  draft: DraftReply | null;
  setDraft: (draft: DraftReply | null) => void;
}

const DraftContext = createContext<DraftStore | null>(null);

export function DraftProvider({ children }: { children: ReactNode }): ReactElement {
  const [draft, setDraft] = useState<DraftReply | null>(null);
  const value = useMemo(() => ({ draft, setDraft }), [draft]);
  return <DraftContext.Provider value={value}>{children}</DraftContext.Provider>;
}

export function useDraftStore(): DraftStore {
  const store = useContext(DraftContext);
  if (store === null) {
    throw new Error("useDraftStore must be used within a DraftProvider");
  }
  return store;
}
