import uuid
from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Agent,
    Artifact,
    Conversation,
    Domain,
    MemoryItem,
    MemoryLink,
    MemoryProposal,
    Message,
    Report,
    ScheduledRun,
    SeedPackage,
    Task,
    ToolCall,
    ToolConnection,
    User,
)

ModelT = TypeVar("ModelT")


class Repository(Generic[ModelT]):
    def __init__(self, session: Session, model: type[ModelT]):
        self.session = session
        self.model = model

    def create(self, **values: Any) -> ModelT:
        instance = self.model(**values)
        self.session.add(instance)
        self.session.commit()
        self.session.refresh(instance)
        return instance

    def get(self, id_: uuid.UUID) -> ModelT | None:
        return self.session.get(self.model, id_)

    def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[ModelT]:
        return self.session.scalars(select(self.model).offset(offset).limit(limit)).all()


class UserRepository(Repository[User]):
    def __init__(self, session: Session):
        super().__init__(session, User)

    def get_by_email(self, email: str) -> User | None:
        return self.session.scalar(select(User).where(User.email == email))


class DomainRepository(Repository[Domain]):
    def __init__(self, session: Session):
        super().__init__(session, Domain)

    def get_by_key(self, key: str) -> Domain | None:
        return self.session.scalar(select(Domain).where(Domain.key == key))

    def list_active(self) -> Sequence[Domain]:
        return self.session.scalars(select(Domain).where(Domain.is_active.is_(True))).all()


class AgentRepository(Repository[Agent]):
    def __init__(self, session: Session):
        super().__init__(session, Agent)

    def get_by_key(self, key: str) -> Agent | None:
        return self.session.scalar(select(Agent).where(Agent.key == key))

    def list_by_domain(self, domain_id: uuid.UUID) -> Sequence[Agent]:
        return self.session.scalars(select(Agent).where(Agent.domain_id == domain_id)).all()


class ConversationRepository(Repository[Conversation]):
    def __init__(self, session: Session):
        super().__init__(session, Conversation)


class MessageRepository(Repository[Message]):
    def __init__(self, session: Session):
        super().__init__(session, Message)

    def list_by_conversation(self, conversation_id: uuid.UUID) -> Sequence[Message]:
        return self.session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        ).all()


class TaskRepository(Repository[Task]):
    def __init__(self, session: Session):
        super().__init__(session, Task)

    def list_by_status(self, status: str) -> Sequence[Task]:
        return self.session.scalars(select(Task).where(Task.status == status)).all()

    def list_children(self, parent_task_id: uuid.UUID) -> Sequence[Task]:
        return self.session.scalars(select(Task).where(Task.parent_task_id == parent_task_id)).all()


class ReportRepository(Repository[Report]):
    def __init__(self, session: Session):
        super().__init__(session, Report)

    def list_by_task(self, task_id: uuid.UUID) -> Sequence[Report]:
        return self.session.scalars(select(Report).where(Report.task_id == task_id)).all()


class MemoryItemRepository(Repository[MemoryItem]):
    def __init__(self, session: Session):
        super().__init__(session, MemoryItem)

    def list_by_scope(
        self,
        scope: str,
        *,
        domain_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
    ) -> Sequence[MemoryItem]:
        query = select(MemoryItem).where(MemoryItem.scope == scope)
        if domain_id is not None:
            query = query.where(MemoryItem.domain_id == domain_id)
        if agent_id is not None:
            query = query.where(MemoryItem.agent_id == agent_id)
        return self.session.scalars(query.order_by(MemoryItem.created_at.desc())).all()

    def list_domain_memory(self, domain_id: uuid.UUID) -> Sequence[MemoryItem]:
        return self.list_by_scope("domain", domain_id=domain_id)

    def list_agent_memory(self, agent_id: uuid.UUID) -> Sequence[MemoryItem]:
        return self.list_by_scope("agent", agent_id=agent_id)

    def list_global_memory(self) -> Sequence[MemoryItem]:
        return self.list_by_scope("global")


class MemoryProposalRepository(Repository[MemoryProposal]):
    def __init__(self, session: Session):
        super().__init__(session, MemoryProposal)

    def list_by_status(self, status: str) -> Sequence[MemoryProposal]:
        return self.session.scalars(
            select(MemoryProposal)
            .where(MemoryProposal.status == status)
            .order_by(MemoryProposal.created_at.desc())
        ).all()


class MemoryLinkRepository(Repository[MemoryLink]):
    def __init__(self, session: Session):
        super().__init__(session, MemoryLink)

    def list_from_memory(self, source_memory_id: uuid.UUID) -> Sequence[MemoryLink]:
        return self.session.scalars(
            select(MemoryLink).where(MemoryLink.source_memory_id == source_memory_id)
        ).all()


class ToolConnectionRepository(Repository[ToolConnection]):
    def __init__(self, session: Session):
        super().__init__(session, ToolConnection)

    def list_by_domain(self, domain_id: uuid.UUID) -> Sequence[ToolConnection]:
        return self.session.scalars(
            select(ToolConnection).where(ToolConnection.domain_id == domain_id)
        ).all()


class ToolCallRepository(Repository[ToolCall]):
    def __init__(self, session: Session):
        super().__init__(session, ToolCall)

    def list_by_task(self, task_id: uuid.UUID) -> Sequence[ToolCall]:
        return self.session.scalars(select(ToolCall).where(ToolCall.task_id == task_id)).all()


class ArtifactRepository(Repository[Artifact]):
    def __init__(self, session: Session):
        super().__init__(session, Artifact)

    def list_by_task(self, task_id: uuid.UUID) -> Sequence[Artifact]:
        return self.session.scalars(select(Artifact).where(Artifact.task_id == task_id)).all()


class SeedPackageRepository(Repository[SeedPackage]):
    def __init__(self, session: Session):
        super().__init__(session, SeedPackage)

    def list_by_status(self, status: str) -> Sequence[SeedPackage]:
        return self.session.scalars(select(SeedPackage).where(SeedPackage.status == status)).all()


class ScheduledRunRepository(Repository[ScheduledRun]):
    def __init__(self, session: Session):
        super().__init__(session, ScheduledRun)

    def list_active(self) -> Sequence[ScheduledRun]:
        return self.session.scalars(select(ScheduledRun).where(ScheduledRun.is_active.is_(True))).all()
