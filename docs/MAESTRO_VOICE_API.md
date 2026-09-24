# Maestro Voice API

Maestro Voice uses the existing `POST /maestro/respond` endpoint and Knowledge interaction mode.
It does not create a separate agent, memory, or orchestration path.

Voice clients may send:

```json
{
  "message": "What should I focus on next?",
  "conversation_id": null,
  "interaction_mode": "knowledge",
  "interface": "voice",
  "response_mode": "voice",
  "client_turn_id": "ce59806e-1bee-4e0c-8ae1-7d938cb4ef93"
}
```

When `response_mode` is `voice`, Maestro supplies the model with spoken-response guidance and
returns a compact envelope containing the response text, conversation summary, echoed client-turn
ID, and `continue_listening`. Voice presentation does not change tool permissions, approval gates,
or action authority.

`client_turn_id` is globally unique for user messages. A completed retry returns the previously
recorded Maestro response instead of creating or executing the turn again.

## Durable Asynchronous Turns

An iOS client may ask Maestro to persist the turn before processing it by adding this header:

```http
X-Maestro-Async: true
```

Maestro immediately records the user message with the supplied `client_turn_id`, returns a compact
pending response, and completes the same turn in the background. The pending response includes the
resolved `conversation_id` and `client_turn_id`, so the client can retain both before iOS suspends it.

The client can recover the turn with:

```http
GET /maestro/turns/{client_turn_id}
```

The returned `status` is `pending`, `completed`, or `failed`. A completed response is the exact
Maestro reply associated with that client turn. Retrying the original request or status lookup does
not create another user message or execute the action again.

This contract supports continuation across normal iOS foreground and background transitions. It
does not itself provide APNs delivery; proactive completion notification remains a separate client
and backend capability.
