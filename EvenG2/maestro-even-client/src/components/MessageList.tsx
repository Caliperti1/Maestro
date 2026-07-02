import type { ChatMessage } from "../types/chat";

type MessageListProps = {
  messages: ChatMessage[];
};

export function MessageList({ messages }: MessageListProps) {
  return (
    <section className="message-panel" aria-label="Message timeline">
      {messages.map((message) => (
        <article key={message.id} className={`message message-${message.role}`}>
          <header>
            <span>{labelForRole(message.role)}</span>
            <time>{message.timestamp}</time>
          </header>
          <p>{message.text}</p>
          <small>{message.channel.toUpperCase()}</small>
        </article>
      ))}
    </section>
  );
}

function labelForRole(role: ChatMessage["role"]): string {
  if (role === "maestro") return "Maestro";
  if (role === "voice") return "Voice Input";
  if (role === "system") return "System";
  return "You";
}
