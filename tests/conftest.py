from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def disable_live_local_classifiers(monkeypatch):
    monkeypatch.setenv("MAESTRO_INTENT_CLASSIFIER_PROVIDER", "none")
    monkeypatch.setenv("MAESTRO_TOPIC_RESOLVER_PROVIDER", "none")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as db:
        yield db
