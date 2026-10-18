"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ChatStreamError, chatStream } from "@/api/chat-stream";
import { studioApi, studioErrorMessage } from "@/api/studio";
import type { ChatReply } from "@/api/studio-types";

import type { LiveTurn } from "./console";

// The stateful half every conversation surface shares: the one conversation
// query, the streaming send (live deltas + tool acts), and Forget. There is one
// agent and one memory behind every surface, so the query key is one key —
// a turn sent from any room updates them all.

// What a send hears when the conversation read has not landed yet, so no session is named.
const NO_SESSION_YET = "the conversation is still loading, try that again in a moment";

export interface UseConversationOptions {
  onReply?: (reply: ChatReply) => void;
  onSettled?: () => void;
  onForgotten?: () => void;
}

export function useConversation(options: UseConversationOptions = {}) {
  const queryClient = useQueryClient();
  const [liveUser, setLiveUser] = useState<string | null>(null);
  const [liveText, setLiveText] = useState("");
  const [act, setAct] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const conversation = useQuery({
    queryKey: ["studio", "conversation"],
    queryFn: () => studioApi.conversation(),
  });

  const send = useMutation({
    // A turn is sent to a named session. Until the conversation read lands there is no session to
    // send to, so the turn is refused here rather than posted to a path no route matches: a message
    // the person typed must come back as a refusal they can retry, never as a silent 404. Once the
    // read has landed with no session yet, the first send starts the conversation before streaming
    // to the id it starts.
    mutationFn: async (text: string) => {
      if (conversation.data === undefined) {
        throw new ChatStreamError(NO_SESSION_YET);
      }
      const sessionId = conversation.data.session_id ?? (await studioApi.startConversation()).session_id;
      return chatStream(sessionId, text, {
        onDelta: (fragment) => setLiveText((current) => current + fragment),
        onStatus: (note) => setAct(note),
      });
    },
    onMutate: (text: string) => {
      setError(null);
      setLiveUser(text);
      setLiveText("");
      setAct(null);
    },
    onSuccess: (reply) => options.onReply?.(reply),
    onError: (failure) => setError(studioErrorMessage(failure)),
    onSettled: async () => {
      // The agent may have acted mid-turn: refresh the conversation first, and
      // only then drop the live bubbles so the settled turn takes their place.
      await queryClient.invalidateQueries({ queryKey: ["studio", "conversation"] });
      options.onSettled?.();
      setLiveUser(null);
      setLiveText("");
      setAct(null);
    },
  });

  const forget = useMutation({
    mutationFn: () => studioApi.forget(),
    onSuccess: () => options.onForgotten?.(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["studio", "conversation"] }),
  });

  const live: LiveTurn = { user: liveUser, text: liveText, act };

  return {
    turns: conversation.data?.turns ?? [],
    live,
    thinking: send.isPending,
    error,
    // Whether a turn can be sent at all: false until the conversation read has resolved, a null
    // id included -- the first send starts the conversation itself. The surfaces hold their
    // composer shut on it, so the refusal above is the backstop rather than the thing a person
    // meets.
    ready: conversation.data !== undefined,
    send: (text: string) => send.mutate(text),
    forget: () => forget.mutate(),
    forgetPending: forget.isPending,
  };
}
