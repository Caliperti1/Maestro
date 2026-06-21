import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Protocol
from urllib import request
from urllib.error import URLError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import MemoryEmbedding, MemoryItem


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient(Protocol):
    provider: str
    model: str

    def embed(self, text: str) -> list[float]:
        pass


@dataclass(frozen=True)
class EmbeddingWriteResult:
    memory_item_id: uuid.UUID
    status: str
    error: str | None = None


class OllamaEmbeddingClient:
    def __init__(self, *, model: str | None = None, base_url: str | None = None):
        settings = get_settings()
        self.provider = "ollama"
        self.model = model or settings.embedding_model
        self.base_url = (base_url or settings.embedding_base_url).rstrip("/")

    def embed(self, text: str) -> list[float]:
        payload = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        http_request = request.Request(
            f"{self.base_url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except URLError as exc:
            raise EmbeddingError(f"Could not reach Ollama embedding service: {exc}") from exc

        embeddings = body.get("embeddings")
        if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
            return _validate_embedding(embeddings[0])

        # Older Ollama versions used /api/embeddings and a singular `embedding` field.
        legacy_payload = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
        legacy_request = request.Request(
            f"{self.base_url}/api/embeddings",
            data=legacy_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(legacy_request, timeout=60) as response:
                legacy_body = json.loads(response.read().decode("utf-8"))
        except URLError as exc:
            raise EmbeddingError(f"Ollama did not return embeddings: {body}") from exc

        return _validate_embedding(legacy_body.get("embedding"))


class OpenAICompatibleEmbeddingClient:
    def __init__(
        self,
        *,
        provider: str = "openai_compatible",
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        settings = get_settings()
        self.provider = provider
        self.model = model or settings.embedding_model
        self.api_key = api_key or settings.embedding_api_key or settings.openai_api_key
        self.base_url = base_url
        if not self.api_key:
            raise EmbeddingError("An embedding API key is required for OpenAI-compatible embeddings.")

    def embed(self, text: str) -> list[float]:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise EmbeddingError("Install the `openai` package for API embeddings.") from exc

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        response = client.embeddings.create(model=self.model, input=text)
        return _validate_embedding(response.data[0].embedding)


class MemoryEmbeddingService:
    def __init__(
        self,
        session: Session,
        *,
        client: EmbeddingClient | None = None,
        best_effort: bool | None = None,
    ):
        settings = get_settings()
        self.session = session
        self.client = client or build_embedding_client()
        self.best_effort = settings.memory_embedding_best_effort if best_effort is None else best_effort

    def upsert_memory_embedding(self, memory_item: MemoryItem) -> EmbeddingWriteResult:
        source_text = memory_embedding_text(memory_item)
        source_hash = _sha256(source_text)
        existing = self.session.scalar(
            select(MemoryEmbedding).where(
                MemoryEmbedding.memory_item_id == memory_item.id,
                MemoryEmbedding.provider == self.client.provider,
                MemoryEmbedding.model == self.client.model,
            )
        )
        if existing is not None and existing.source_text_hash == source_hash:
            return EmbeddingWriteResult(memory_item_id=memory_item.id, status="current")

        try:
            embedding = self.client.embed(source_text)
        except Exception as exc:
            if self.best_effort:
                return EmbeddingWriteResult(
                    memory_item_id=memory_item.id,
                    status="failed",
                    error=str(exc),
                )
            raise

        if existing is None:
            existing = MemoryEmbedding(
                memory_item_id=memory_item.id,
                provider=self.client.provider,
                model=self.client.model,
                dimensions=len(embedding),
                source_text_hash=source_hash,
                embedding=embedding,
                metadata_={},
            )
            self.session.add(existing)
        else:
            existing.dimensions = len(embedding)
            existing.source_text_hash = source_hash
            existing.embedding = embedding
        self.session.flush()
        return EmbeddingWriteResult(memory_item_id=memory_item.id, status="written")

    def backfill(self, *, limit: int | None = None) -> list[EmbeddingWriteResult]:
        query = select(MemoryItem).order_by(MemoryItem.created_at.asc())
        if limit is not None:
            query = query.limit(limit)
        results = [
            self.upsert_memory_embedding(memory_item)
            for memory_item in self.session.scalars(query).all()
        ]
        self.session.commit()
        return results


def build_embedding_client() -> EmbeddingClient:
    settings = get_settings()
    if settings.embedding_provider == "ollama":
        return OllamaEmbeddingClient()
    if settings.embedding_provider in {"openai", "openai_compatible", "openrouter"}:
        base_url = settings.openrouter_base_url if settings.embedding_provider == "openrouter" else None
        api_key = (
            settings.openrouter_api_key
            if settings.embedding_provider == "openrouter"
            else settings.embedding_api_key or settings.openai_api_key
        )
        return OpenAICompatibleEmbeddingClient(
            provider=settings.embedding_provider,
            api_key=api_key,
            base_url=base_url,
        )
    raise EmbeddingError(f"Unsupported EMBEDDING_PROVIDER: {settings.embedding_provider}")


def memory_embedding_text(memory_item: MemoryItem) -> str:
    return "\n".join(
        [
            f"Title: {memory_item.title}",
            f"Type: {memory_item.memory_type}",
            f"Scope: {memory_item.scope}",
            f"Impact: {memory_item.impact_level}",
            f"Content: {memory_item.content}",
        ]
    )


def _validate_embedding(value) -> list[float]:
    if not isinstance(value, list) or not value:
        raise EmbeddingError("Embedding response did not contain a vector.")
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise EmbeddingError("Embedding response contained non-numeric values.") from exc


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
