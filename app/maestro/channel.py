import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import Conversation, Message, RuntimeSetting
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains

MAESTRO_CHANNEL_KEY = "maestro_primary_channel"


def get_or_create_maestro_channel(session: Session) -> Conversation:
    setting = session.get(RuntimeSetting, MAESTRO_CHANNEL_KEY)
    value = setting.value if setting is not None else {}
    conversation_id = value.get("conversation_id") if isinstance(value, dict) else None
    if conversation_id:
        try:
            conversation = session.get(Conversation, uuid.UUID(str(conversation_id)))
            if conversation is not None:
                return conversation
        except (TypeError, ValueError):
            pass

    seed_default_domains(session)
    maestro_domain = DomainRepository(session).get_by_key("maestro-development")
    conversation = Conversation(
        domain_id=maestro_domain.id if maestro_domain else None,
        title="Maestro channel",
        metadata_={"channel": "primary", "archived": False},
    )
    session.add(conversation)
    session.flush()
    if setting is None:
        setting = RuntimeSetting(key=MAESTRO_CHANNEL_KEY, value={})
        session.add(setting)
    setting.value = {"conversation_id": str(conversation.id)}
    session.commit()
    session.refresh(conversation)
    return conversation


def record_channel_message(
    session: Session,
    *,
    sender: str,
    content: str,
    metadata: dict | None = None,
) -> Message:
    conversation = get_or_create_maestro_channel(session)
    message = Message(
        conversation_id=conversation.id,
        sender_type="user" if sender == "user" else "maestro",
        content=content,
        metadata_=metadata or {},
    )
    session.add(message)
    conversation.updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(message)
    session.refresh(conversation)
    return message
