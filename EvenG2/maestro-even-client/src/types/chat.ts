export type MessageRole = "maestro" | "user" | "voice" | "system";

export type MessageChannel = "text" | "voice" | "event";

export type ChatMessage = {
  id: string;
  role: MessageRole;
  channel: MessageChannel;
  text: string;
  timestamp: string;
};

export type BridgeEvent = {
  type: "connected" | "disconnected" | "incoming_message" | "outgoing_message";
  message?: ChatMessage;
};
