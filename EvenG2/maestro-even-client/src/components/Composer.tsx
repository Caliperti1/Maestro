import { useState } from "react";
import type { FormEvent } from "react";

type ComposerProps = {
  disabled?: boolean;
  listening?: boolean;
  onSendText: (value: string) => void;
  onRequestListening: () => void;
};

export function Composer({ disabled, listening, onSendText, onRequestListening }: ComposerProps) {
  const [text, setText] = useState("");

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSendText(text);
    setText("");
  };

  return (
    <section className="composer" aria-label="Message composer">
      <form onSubmit={submit}>
        <input
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={
            listening
              ? "Listening mode: type your voice message and press Send"
              : "Type a message to Maestro"
          }
          disabled={disabled}
        />
        <button type="submit" disabled={disabled || !text.trim()}>
          Send
        </button>
      </form>
      <button type="button" className="voice-button" onClick={onRequestListening} disabled={disabled}>
        {listening ? "Listening Active" : "Start Listening"}
      </button>
    </section>
  );
}
