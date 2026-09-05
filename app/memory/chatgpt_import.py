"""Incremental ChatGPT export adapter that stages normalized conversations for curation."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.repositories import DomainRepository
from app.memory.dropbox import MemoryDropboxProcessor
from app.memory.ingestion import (
    ContextEnvelope,
    IngestionLedgerService,
    SourcePolicy,
    policy_for_domain,
)


@dataclass(frozen=True)
class ChatGPTImportResult:
    conversations_seen: int
    staged: int
    unchanged: int
    failed: int
    files: list[str]
    errors: list[str]


class ChatGPTExportImporter:
    def __init__(self, session: Session, *, root: Path | None = None):
        self.session = session
        self.root = root or Path(get_settings().memory_dropbox_root)

    def import_bytes(self, payload: bytes, *, filename: str, default_domain_key: str = "personal") -> ChatGPTImportResult:
        conversations = self._load_conversations(payload, filename)
        MemoryDropboxProcessor(self.session, root=self.root).ensure_directories()
        staged = unchanged = failed = 0
        files: list[str] = []
        errors: list[str] = []
        for conversation in conversations:
            try:
                markdown, source_timestamp = self._normalize_conversation(conversation)
                if not markdown.strip():
                    unchanged += 1
                    continue
                domain_key = self._infer_domain(conversation, markdown, default_domain_key)
                domain = DomainRepository(self.session).get_by_key(domain_key)
                if domain is None:
                    raise ValueError(f"Unknown target domain: {domain_key}")
                conversation_id = str(conversation.get("id") or conversation.get("conversation_id") or _sha(markdown)[:24])
                content_hash = _sha(markdown)
                title = str(conversation.get("title") or "Untitled ChatGPT conversation")
                destination = self.root / domain_key / "inbox" / f"chatgpt-{_slug(title)}-{conversation_id[:10]}-{content_hash[:8]}.md"
                base_policy = policy_for_domain(domain_key)
                policy = SourcePolicy(
                    sensitivity=base_policy.sensitivity,
                    trust_level="user_provided",
                    transfer_method="chatgpt_export",
                    egress_policy=base_policy.egress_policy,
                    retention=base_policy.retention,
                )
                envelope = ContextEnvelope(
                    source_registration_key=f"chatgpt-export:{domain_key}",
                    source_system="chatgpt",
                    external_id=conversation_id,
                    source_version=content_hash,
                    content_hash=content_hash,
                    content_type="conversation_markdown",
                    domain_key=domain_key,
                    source_timestamp=source_timestamp,
                    artifact_uri=str(destination),
                    policy=policy,
                    metadata={"title": title, "import_filename": filename},
                )
                ledger = IngestionLedgerService(self.session)
                ledger.ensure_registration(key=envelope.source_registration_key, source_system="chatgpt", display_name=f"ChatGPT export ({domain_key})", adapter_type="chatgpt_export", domain=domain, policy=policy)
                claim = ledger.claim(envelope, domain=domain)
                if not claim.should_process:
                    unchanged += 1
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(markdown, encoding="utf-8")
                ledger.mark_processed(claim.record, processed_path=destination)
                staged += 1
                files.append(str(destination))
            except Exception as exc:
                failed += 1
                errors.append(str(exc))
        return ChatGPTImportResult(len(conversations), staged, unchanged, failed, files, errors)

    def _load_conversations(self, payload: bytes, filename: str) -> list[dict[str, Any]]:
        if filename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = [name for name in archive.namelist() if name.endswith("conversations.json")]
                if not names:
                    raise ValueError("ChatGPT export does not contain conversations.json.")
                data = json.loads(archive.read(names[0]).decode("utf-8"))
        else:
            data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, list):
            raise ValueError("ChatGPT conversations export must be a JSON list.")
        return [item for item in data if isinstance(item, dict)]

    def _normalize_conversation(self, conversation: dict[str, Any]) -> tuple[str, datetime | None]:
        messages: list[tuple[float, str, str]] = []
        for node in (conversation.get("mapping") or {}).values():
            message = node.get("message") if isinstance(node, dict) else None
            if not isinstance(message, dict):
                continue
            author = message.get("author") or {}
            role = str(author.get("role") or "unknown")
            content = message.get("content") or {}
            parts = content.get("parts") if isinstance(content, dict) else None
            text_parts = [part for part in (parts or []) if isinstance(part, str) and part.strip()]
            if not text_parts:
                continue
            created = float(message.get("create_time") or 0.0)
            messages.append((created, role, "\n\n".join(text_parts).strip()))
        messages.sort(key=lambda item: item[0])
        title = str(conversation.get("title") or "Untitled ChatGPT conversation")
        created_at = _timestamp(conversation.get("create_time") or (messages[0][0] if messages else None))
        updated_at = _timestamp(conversation.get("update_time") or (messages[-1][0] if messages else None))
        header = [f"# {title}", "", "Source: ChatGPT export", f"Conversation ID: {conversation.get('id') or conversation.get('conversation_id') or 'unknown'}"]
        if created_at:
            header.append(f"Created: {created_at.isoformat()}")
        if updated_at:
            header.append(f"Updated: {updated_at.isoformat()}")
        body = [f"## {role.title()}\n{text}" for _, role, text in messages]
        return "\n".join(header) + "\n\n" + "\n\n".join(body) + "\n", updated_at or created_at

    def _infer_domain(self, conversation: dict[str, Any], markdown: str, default: str) -> str:
        haystack = f"{conversation.get('title') or ''}\n{markdown[:4000]}".lower()
        aliases = {
            "praxis": ("praxis", "groundtruth"),
            "perti-laboratories": ("perti labs", "perti laboratories", "maestro", "ophi"),
            "usma": ("west point", "usma"),
        }
        matches = [key for key, terms in aliases.items() if any(term in haystack for term in terms)]
        return matches[0] if len(matches) == 1 else default


def _timestamp(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=UTC) if value else None
    except (TypeError, ValueError, OSError):
        return None


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "conversation"
