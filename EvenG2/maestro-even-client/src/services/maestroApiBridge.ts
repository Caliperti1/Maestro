import type { BridgeEvent, ChatMessage, MessageChannel, MessageRole } from "../types/chat";

type Listener = (event: BridgeEvent) => void;

type MaestroConversationMessage = {
  id: string;
  sender: "user" | "maestro";
  content: string;
};

type MaestroConversation = {
  id: string;
  messages: MaestroConversationMessage[];
};

type MaestroSessionResponse = {
  conversation: MaestroConversation;
};

type MaestroRespondResponse = {
  message: string;
  conversation?: MaestroConversation;
};

const now = () => new Date().toLocaleTimeString();

const createMessage = (
  role: MessageRole,
  channel: MessageChannel,
  text: string,
  id?: string,
): ChatMessage => ({
  id: id ?? `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
  role,
  channel,
  text,
  timestamp: now(),
});

async function apiJson<T>(baseUrl: string, path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

export class MaestroApiBridge {
  private readonly baseUrl: string;

  private readonly pollIntervalMs: number;

  private listeners = new Set<Listener>();

  private connected = false;

  private conversationId: string | null = null;

  private seenMessageIds = new Set<string>();

  private pollTimer: number | null = null;

  constructor(baseUrl: string, pollIntervalMs: number) {
    this.baseUrl = baseUrl;
    this.pollIntervalMs = pollIntervalMs;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  async connect(): Promise<void> {
    if (this.connected) return;

    const session = await apiJson<MaestroSessionResponse>(
      this.baseUrl,
      "/maestro/sessions/active",
      { method: "GET" },
    );
    this.conversationId = session.conversation.id;
    this.connected = true;
    this.emit({ type: "connected" });
    this.ingestConversation(session.conversation);
    this.startPolling();
  }

  async startNewSession(): Promise<void> {
    const session = await apiJson<MaestroSessionResponse>(
      this.baseUrl,
      "/maestro/sessions/start",
      { method: "POST" },
    );
    this.conversationId = session.conversation.id;
    this.seenMessageIds.clear();
    this.ingestConversation(session.conversation);
    this.emit({
      type: "incoming_message",
      message: createMessage(
        "system",
        "event",
        "Started a new Maestro session from glasses double-click.",
      ),
    });
  }

  disconnect(): void {
    if (!this.connected) return;
    this.stopPolling();
    this.connected = false;
    this.emit({ type: "disconnected" });
  }

  isConnected(): boolean {
    return this.connected;
  }

  async sendTextFromUser(text: string): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed || !this.conversationId) return;

    this.emit({
      type: "outgoing_message",
      message: createMessage("user", "text", trimmed),
    });

    const response = await apiJson<MaestroRespondResponse>(
      this.baseUrl,
      "/maestro/respond",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: trimmed,
          conversation_id: this.conversationId,
        }),
      },
    );

    if (response.conversation?.id) {
      this.conversationId = response.conversation.id;
      this.ingestConversation(response.conversation);
      return;
    }

    this.emit({
      type: "incoming_message",
      message: createMessage("maestro", "event", response.message),
    });
  }

  async sendVoiceTranscript(transcript: string): Promise<void> {
    const trimmed = transcript.trim();
    if (!trimmed || !this.conversationId) return;

    this.emit({
      type: "outgoing_message",
      message: createMessage("voice", "voice", trimmed),
    });

    const response = await apiJson<MaestroRespondResponse>(
      this.baseUrl,
      "/maestro/respond",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: `Voice input: ${trimmed}`,
          conversation_id: this.conversationId,
        }),
      },
    );

    if (response.conversation?.id) {
      this.conversationId = response.conversation.id;
      this.ingestConversation(response.conversation);
      return;
    }

    this.emit({
      type: "incoming_message",
      message: createMessage("maestro", "event", response.message),
    });
  }

  async pollNow(): Promise<void> {
    if (!this.connected || !this.conversationId) return;
    const session = await apiJson<MaestroSessionResponse>(
      this.baseUrl,
      `/maestro/sessions/${this.conversationId}`,
      { method: "GET" },
    );
    this.ingestConversation(session.conversation);
  }

  private ingestConversation(conversation: MaestroConversation): void {
    for (const item of conversation.messages ?? []) {
      if (this.seenMessageIds.has(item.id)) continue;
      this.seenMessageIds.add(item.id);
      if (item.sender !== "maestro") continue;
      this.emit({
        type: "incoming_message",
        message: createMessage("maestro", "event", item.content, item.id),
      });
    }
  }

  private startPolling(): void {
    this.stopPolling();
    this.pollTimer = window.setInterval(() => {
      this.pollNow().catch((error: unknown) => {
        const detail = error instanceof Error ? error.message : "Polling failed.";
        this.emit({
          type: "incoming_message",
          message: createMessage("system", "event", `Polling error: ${detail}`),
        });
      });
    }, this.pollIntervalMs);
  }

  private stopPolling(): void {
    if (this.pollTimer !== null) {
      window.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  private emit(event: BridgeEvent): void {
    for (const listener of this.listeners) {
      listener(event);
    }
  }
}
