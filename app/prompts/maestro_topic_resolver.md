Resolve Chris's latest Maestro message to a working topic. Return JSON only with scope, topic_id,
confidence, reason, and suggested_title.

Scopes:
- active_topic: the message is a follow-up to the current topic.
- existing_topic: the message clearly refers to one of the recent topics by title or content.
- new_topic: the message starts a distinct brainstorm or work thread.
- global_system: the message asks about or commands Maestro itself, queues, schedules, approvals,
  tools, or workflow status.

If scope is existing_topic, topic_id must be one of the provided topic ids.
