"use client";

import type { ReactElement } from "react";

import { SessionChat } from "@/components/chat/session-chat";
import { Drawer } from "@/shared/ui/drawer";

// Every run opens its own conversation: the session behind it, full height,
// in a drawer that leaves the page underneath untouched. Works the same for
// a chat-started run's session and a manual run's own runner session -- the
// drawer only ever needs that session's id.

export interface RunChatDrawerProps {
  sessionId: string;
  open: boolean;
  onClose: () => void;
}

export function RunChatDrawer({ sessionId, open, onClose }: RunChatDrawerProps): ReactElement {
  return (
    <Drawer open={open} title="Run chat" onClose={onClose}>
      <div className="flex h-full flex-col">
        <SessionChat key={sessionId} sessionId={sessionId} reply={null} onSettle={() => {}} />
      </div>
    </Drawer>
  );
}
