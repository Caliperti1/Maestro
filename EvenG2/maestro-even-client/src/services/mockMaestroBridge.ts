import type { BridgeEvent, ChatMessage, MessageChannel, MessageRole } from "../types/chat";

type Listener = (event: BridgeEvent) => void;

const now = () => new Date().toLocaleTimeString();

const createMessage = (
  role: MessageRole,
  channel: MessageChannel,
  text: string,
): ChatMessage => ({
  id: `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
  role,
  channel,
  text,
  timestamp: now(),
});

export class MockMaestroBridge {
  private listeners = new Set<Listener>();

  private connected = false;

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  connect(): void {
    if (this.connected) {
      return;
    }
    this.connected = true;
    this.emit({ type: "connected" });
    this.emit({
      type: "incoming_message",
      message: createMessage(
        "maestro",
        "event",
        "Maestro bridge initialized (mock). Waiting for tasking.",
      ),
    });
  }

  disconnect(): void {
    if (!this.connected) {
      return;
    }
    this.connected = false;
    this.emit({ type: "disconnected" });
  }

  isConnected(): boolean {
    return this.connected;
  }

  sendTextFromUser(text: string): void {
    if (!text.trim()) {
      return;
    }
    this.emit({
      type: "outgoing_message",
      message: createMessage("user", "text", text.trim()),
    });
  }

  sendVoiceTranscript(transcript: string): void {
    if (!transcript.trim()) {
      return;
    }
    this.emit({
      type: "outgoing_message",
      message: createMessage("voice", "voice", transcript.trim()),
    });
  }

  simulateIncomingMaestroMessage(): void {
    const choices = [
      "Workflow update: Draft synthesis is 40% complete.",
      "Approval requested: Publish standup summary to team channel?",
      "Notification: Memory curation batch finished for maestro-development.",
      "Status: One queued task is waiting on user feedback.",
    ];
    const text = choices[Math.floor(Math.random() * choices.length)] ?? "Maestro update received.";
    this.emit({
      type: "incoming_message",
      message: createMessage("maestro", "event", text),
    });
  }

  private emit(event: BridgeEvent): void {
    for (const listener of this.listeners) {
      listener(event);
    }
  }
}

export const mockMaestroBridge = new MockMaestroBridge();
