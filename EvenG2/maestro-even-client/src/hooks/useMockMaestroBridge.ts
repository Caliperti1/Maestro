import { useEffect, useMemo, useState } from "react";

import { mockMaestroBridge } from "../services/mockMaestroBridge";
import type { ChatMessage } from "../types/chat";

const initialMessages: ChatMessage[] = [
  {
    id: "init-1",
    role: "system",
    channel: "event",
    text: "Simulator-first mode. Maestro backend is not connected yet.",
    timestamp: new Date().toLocaleTimeString(),
  },
];

export function useMockMaestroBridge() {
  const [connected, setConnected] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);

  useEffect(() => {
    const unsubscribe = mockMaestroBridge.subscribe((event) => {
      if (event.type === "connected") {
        setConnected(true);
        return;
      }
      if (event.type === "disconnected") {
        setConnected(false);
        return;
      }
      if (event.message) {
        setMessages((prev) => [...prev, event.message!]);
      }
    });

    return unsubscribe;
  }, []);

  const actions = useMemo(
    () => ({
      connect: () => mockMaestroBridge.connect(),
      disconnect: () => mockMaestroBridge.disconnect(),
      sendText: (text: string) => mockMaestroBridge.sendTextFromUser(text),
      sendVoice: (transcript: string) => mockMaestroBridge.sendVoiceTranscript(transcript),
      simulateInbound: () => mockMaestroBridge.simulateIncomingMaestroMessage(),
    }),
    [],
  );

  return {
    connected,
    messages,
    ...actions,
  };
}
