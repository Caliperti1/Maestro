import { useEffect, useMemo, useRef, useState } from "react";

import { MaestroApiBridge } from "../services/maestroApiBridge";
import { mockMaestroBridge } from "../services/mockMaestroBridge";
import type { ChatMessage } from "../types/chat";

type BridgeMode = "mock" | "live";

type BridgeLike = {
  subscribe: (listener: (event: { type: string; message?: ChatMessage }) => void) => () => void;
  connect: () => void | Promise<void>;
  disconnect: () => void;
  sendTextFromUser: (text: string) => void | Promise<void>;
  sendVoiceTranscript: (transcript: string) => void | Promise<void>;
};

const systemMessage = (text: string): ChatMessage => ({
  id: `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
  role: "system",
  channel: "event",
  text,
  timestamp: new Date().toLocaleTimeString(),
});

export function useMaestroBridge(mode: BridgeMode, baseUrl: string, pollIntervalMs: number) {
  const [connected, setConnected] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([
    systemMessage(
      mode === "live"
        ? "Live mode ready. Connect to call Maestro API."
        : "Mock mode ready. Connect to run local simulation.",
    ),
  ]);
  const bridgeRef = useRef<BridgeLike | null>(null);

  useEffect(() => {
    const bridge =
      mode === "live"
        ? new MaestroApiBridge(baseUrl.replace(/\/$/, ""), pollIntervalMs)
        : mockMaestroBridge;
    bridgeRef.current = bridge;
    setConnected(false);
    setMessages([
      systemMessage(
        mode === "live"
          ? `Transport switched to live API (${baseUrl.replace(/\/$/, "")}).`
          : "Transport switched to mock simulation.",
      ),
    ]);

    const unsubscribe = bridge.subscribe((event) => {
      if (event.type === "connected") {
        setConnected(true);
        return;
      }
      if (event.type === "disconnected") {
        setConnected(false);
        return;
      }
      if (event.message) {
        setMessages((prev) => [...prev, event.message]);
      }
    });

    return () => {
      unsubscribe();
      bridge.disconnect();
    };
  }, [mode, baseUrl, pollIntervalMs]);

  const actions = useMemo(
    () => ({
      connect: async () => {
        const bridge = bridgeRef.current;
        if (!bridge) return;
        await bridge.connect();
      },
      disconnect: () => {
        bridgeRef.current?.disconnect();
      },
      sendText: async (text: string) => {
        const bridge = bridgeRef.current;
        if (!bridge) return;
        await bridge.sendTextFromUser(text);
      },
      sendVoice: async (transcript: string) => {
        const bridge = bridgeRef.current;
        if (!bridge) return;
        await bridge.sendVoiceTranscript(transcript);
      },
      simulateInbound: async () => {
        if (mode === "mock") {
          mockMaestroBridge.simulateIncomingMaestroMessage();
          return;
        }
        const bridge = bridgeRef.current;
        if (bridge instanceof MaestroApiBridge) {
          await bridge.pollNow();
        }
      },
    }),
    [mode],
  );

  return {
    connected,
    messages,
    ...actions,
  };
}
