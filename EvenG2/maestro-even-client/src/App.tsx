import { useEffect, useMemo, useRef, useState } from "react";

import { Composer } from "./components/Composer";
import { MessageList } from "./components/MessageList";
import { useMaestroBridge } from "./hooks/useMaestroBridge";
import { evenGlassesDisplay } from "./services/evenGlassesDisplay";

function App() {
  const [mode, setMode] = useState<"mock" | "live">("live");
  const [backendUrl, setBackendUrl] = useState("http://localhost:8000");
  const [voiceState, setVoiceState] = useState<"idle" | "listening" | "conducting" | "approval">("idle");
  const [displayCleared, setDisplayCleared] = useState(false);
  const [awaitingMaestro, setAwaitingMaestro] = useState(false);
  const [latestMaestroBody, setLatestMaestroBody] = useState("");
  const [motionTick, setMotionTick] = useState(0);
  const { connected, messages, connect, disconnect, sendText, sendVoice, simulateInbound } =
    useMaestroBridge(mode, backendUrl, 3000);
  const lastMaestroIdRef = useRef<string | null>(null);

  const connectLabel = mode === "live" ? "Connect API" : "Connect Mock";
  const actionLabel = mode === "live" ? "Poll Maestro" : "Simulate Maestro Event";

  useEffect(() => {
    if (!connected) return;
    if (voiceState !== "listening" && voiceState !== "conducting") return;

    const timer = window.setInterval(() => {
      setMotionTick((value) => value + 1);
    }, 320);

    return () => {
      window.clearInterval(timer);
    };
  }, [connected, voiceState]);

  const glassesText = useMemo(() => {
    const listeningFrames = ["(    )", "(.   )", "(..  )", "(... )", "(....)", "(... )", "(..  )", "(.   )"];
    const conductingFrames = ["|", "/", "-", "\\"];

    if (!connected) {
      return `M DISCONNECTED\n${mode.toUpperCase()}`;
    }

    if (voiceState === "listening") {
      const frame = listeningFrames[motionTick % listeningFrames.length] ?? "(.   )";
      return `LISTEN ${frame}`;
    }

    if (voiceState === "conducting") {
      const frame = conductingFrames[motionTick % conductingFrames.length] ?? "|";
      return `THINK ${frame}`;
    }

    if (voiceState === "approval") {
      return `${latestMaestroBody}\n\nTAP=Reply  ^ OK  v NO`;
    }

    if (displayCleared) {
      return "";
    }

    if (latestMaestroBody) {
      return latestMaestroBody;
    }

    return "M READY";
  }, [connected, displayCleared, latestMaestroBody, mode, motionTick, voiceState]);

  const isApprovalMessage = (text: string): boolean =>
    /approve|approval|disapprove|needs your approval|reject/i.test(text);

  const startListeningCycle = (): void => {
    if (!connected) return;
    setDisplayCleared(false);
    setVoiceState("listening");
    setMotionTick(0);
  };

  const handleTypedSend = async (text: string): Promise<void> => {
    if (!connected) return;
    const trimmed = text.trim();
    if (!trimmed) return;

    setDisplayCleared(false);
    setVoiceState("conducting");
    setMotionTick(0);
    setAwaitingMaestro(true);

    if (voiceState === "listening" || voiceState === "approval") {
      await sendVoice(trimmed);
      return;
    }

    await sendText(trimmed);
  };

  const clearDisplay = (): void => {
    setDisplayCleared(true);
  };

  const approveFromGesture = async (): Promise<void> => {
    if (!connected) return;
    setDisplayCleared(false);
    setVoiceState("conducting");
    setMotionTick(0);
    setAwaitingMaestro(true);
    await sendText("Approve the pending item.");
  };

  const disapproveFromGesture = async (): Promise<void> => {
    if (!connected) return;
    setDisplayCleared(false);
    setVoiceState("conducting");
    setMotionTick(0);
    setAwaitingMaestro(true);
    await sendText("Disapprove the pending item.");
  };

  useEffect(() => {
    const latestMaestro = [...messages].reverse().find((message) => message.role === "maestro");
    if (!latestMaestro || latestMaestro.id === lastMaestroIdRef.current) {
      return;
    }

    lastMaestroIdRef.current = latestMaestro.id;
    setLatestMaestroBody(latestMaestro.text);
    setAwaitingMaestro(false);
    setDisplayCleared(false);
    setVoiceState(isApprovalMessage(latestMaestro.text) ? "approval" : "idle");
  }, [messages]);

  useEffect(() => {
    if (!awaitingMaestro || voiceState !== "conducting") return;
    const timeout = window.setTimeout(() => {
      setVoiceState("idle");
    }, 20000);
    return () => {
      window.clearTimeout(timeout);
    };
  }, [awaitingMaestro, voiceState]);

  useEffect(() => {
    evenGlassesDisplay.renderText(glassesText).catch(() => {
      // No-op when bridge is unavailable (desktop browser outside Even runtime).
    });
  }, [glassesText]);

  useEffect(() => {
    if (mode !== "live") return;
    evenGlassesDisplay
      .onGestures({
        onDoubleTap: async () => {
          if (!connected) return;
          startListeningCycle();
        },
        onSingleTap: async () => {
          if (!connected) return;
          startListeningCycle();
        },
        onSwipeUp: async () => {
          if (voiceState === "approval") {
            await approveFromGesture();
            return;
          }
          clearDisplay();
        },
        onSwipeDown: async () => {
          if (voiceState === "approval") {
            await disapproveFromGesture();
            return;
          }
          clearDisplay();
        },
      })
      .catch(() => {
        // No-op when bridge is unavailable (desktop browser outside Even runtime).
      });
  }, [connected, mode, voiceState]);

  return (
    <main className="app-shell">
      <header className="app-header">
        <div>
          <h1>Maestro EvenG2 Client</h1>
          <p>Simulator-first prototype with switchable mock/live Maestro bridge.</p>
          <div className="bridge-config">
            <label>
              Mode
              <select value={mode} onChange={(event) => setMode(event.target.value as "mock" | "live")}> 
                <option value="live">Live API</option>
                <option value="mock">Mock</option>
              </select>
            </label>
            <label>
              Backend URL
              <input
                value={backendUrl}
                onChange={(event) => setBackendUrl(event.target.value)}
                placeholder="http://localhost:8000"
                disabled={mode !== "live"}
              />
            </label>
          </div>
        </div>
        <div className="status-group">
          <span className={`status-dot ${connected ? "online" : "offline"}`}>
            {connected ? "Bridge Connected" : "Bridge Disconnected"}
          </span>
          <span className="status-dot online">Voice: {voiceState.toUpperCase()}</span>
          <button
            type="button"
            onClick={() => {
              const action = connected ? disconnect() : connect();
              Promise.resolve(action).catch((error: unknown) => {
                const detail = error instanceof Error ? error.message : "Connection failed.";
                window.alert(detail);
              });
            }}
          >
            {connected ? "Disconnect" : connectLabel}
          </button>
          <button
            type="button"
            onClick={() => {
              Promise.resolve(simulateInbound()).catch((error: unknown) => {
                const detail = error instanceof Error ? error.message : "Action failed.";
                window.alert(detail);
              });
            }}
            disabled={!connected}
          >
            {actionLabel}
          </button>
        </div>
      </header>

      <MessageList messages={messages} />

      <Composer
        disabled={!connected}
        listening={voiceState === "listening" || voiceState === "approval"}
        onSendText={(value) => {
          Promise.resolve(handleTypedSend(value)).catch((error: unknown) => {
            const detail = error instanceof Error ? error.message : "Send failed.";
            window.alert(detail);
          });
        }}
        onRequestListening={startListeningCycle}
      />
    </main>
  );
}

export default App;
