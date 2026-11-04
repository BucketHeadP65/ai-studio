import { isTextUIPart, type UIMessage } from "ai";
import type { ReactElement } from "react";

import type { ConversationMessage } from "@/api/inspection";
import { conversationPayloads } from "@/api/inspection";
import { refusalOf, stepConversationToUiMessages } from "@/api/step-chat";
import { AssistantTurn } from "@/components/chat/assistant-turn";
import { ReasoningNote } from "@/components/chat/reasoning-note";
import { RefusalNote } from "@/components/chat/refusal-note";

import { Folded } from "./folded";

// Recorded messages drawn exactly as the chat draws a turn: a user bubble folded past 12 lines,
// then an assistant bubble per run of response rows, its thinking ahead of it the way the step
// chat draws a step's, and a refused answer's refusal between the turns it sits between. Used by
// the step drawer and by the inspector's conversation.

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

function Turn({ message, speaker }: { message: UIMessage; speaker: string | undefined }): ReactElement {
  if (message.role === "user") {
    return <UserTurn message={message} />;
  }
  const refusal = refusalOf(message);
  if (refusal !== null) {
    return <RefusalNote text={refusal} />;
  }
  return (
    <div className="flex flex-col gap-2">
      <ReasoningNote message={message} live={false} />
      <AssistantTurn message={message} speaker={speaker} foldSettled={false} />
    </div>
  );
}

export function TurnMessages({ messages, speaker }: { messages: ConversationMessage[]; speaker?: string }): ReactElement {
  const payloads = conversationPayloads(messages);
  return (
    <div className="flex flex-col gap-3">
      {stepConversationToUiMessages(payloads, { toolReturnsFrom: payloads }).map((message) => (
        <Turn key={message.id} message={message} speaker={speaker} />
      ))}
    </div>
  );
}
