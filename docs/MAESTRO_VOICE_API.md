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
recorded Maestro response instead of creating or executing the turn again. A concurrent retry while
the first request is still processing receives HTTP 409 and may retry shortly.

