import { isTextUIPart, type UIMessage } from "ai";
import type { ReactElement } from "react";

import type { ConversationMessage } from "@/api/inspection";
import { conversationPayloads } from "@/api/inspection";
import { stepConversationToUiMessages } from "@/api/step-chat";
import { AssistantTurn } from "@/components/chat/assistant-turn";

import { Folded } from "./folded";

// One step's messages, drawn exactly as the chat draws a turn: a user bubble folded past 12
// lines, then an assistant bubble per run of response rows. Used by the step drawer.

function textOf(message: UIMessage): string {
  return message.parts
    .filter(isTextUIPart)
    .map((part) => part.text)
    .join("");
}

function UserTurn({ message }: { message: UIMessage }): ReactElement {
  return (
    <div className="flex max-w-[92%] flex-col self-end">
      <p className="mb-1 text-right text-label-3 text-micro font-semibold uppercase tracking-caps">You</p>
      <div className="max-w-[640px] break-words rounded-tl-lg rounded-tr-lg rounded-bl-lg rounded-br-sm bg-label-1 px-4 py-3.5 text-body text-surface">
        <Folded text={textOf(message)} lines={12} />
      </div>
    </div>
  );
}

export function TurnMessages({ messages }: { messages: ConversationMessage[] }): ReactElement {
  const payloads = conversationPayloads(messages);
  return (
    <div className="flex flex-col gap-3">
      {stepConversationToUiMessages(payloads, { toolReturnsFrom: payloads }).map((message) =>
        message.role === "user" ? (
          <UserTurn key={message.id} message={message} />
        ) : (
          <AssistantTurn key={message.id} message={message} foldSettled={false} />
        ),
      )}
    </div>
  );
}
