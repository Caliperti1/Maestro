import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import MemoryItem, MemoryProposal, SeedPackage
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.memory.document_extract import SUPPORTED_DROPBOX_SUFFIXES
from app.memory.dropbox import MemoryDropboxProcessor
from app.memory.retrieval import (
    MemoryRetrievalError,
    MemoryRetrievalQuery,
    MemoryRetrievalService,
    RetrievedMemory,
    RetrievedMemoryLink,
)
from app.memory.service import MemoryAccessError, MemoryService

router = APIRouter(prefix="/memory", tags=["memory"])


class RejectProposalRequest(BaseModel):
    reason: str | None = None


class ReclassifySourceRequest(BaseModel):
    target_domain_key: str
    reason: str | None = None


@router.get("/dropbox/status")
def get_dropbox_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    root = _dropbox_root()
    return {
        "root": str(root),
        "domains": [_domain_status(root, key) for key in _domain_keys(db)],
    }


@router.post("/dropbox/{domain_key}/upload")
async def upload_dropbox_file(
    domain_key: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _validate_domain_key(db, domain_key)
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="Uploaded file must have a filename.")
    if Path(filename).suffix.lower() not in SUPPORTED_DROPBOX_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_DROPBOX_SUFFIXES))
        raise HTTPException(status_code=400, detail=f"Supported file types: {supported}.")

    inbox = _dropbox_root() / domain_key / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    destination = _available_destination(inbox / filename)
    destination.write_bytes(await file.read())
    return {
        "domain_key": domain_key,
        "filename": destination.name,
        "path": str(destination),
        "status": "uploaded",
    }


@router.post("/dropbox/process")
def process_dropbox(db: Session = Depends(get_db)) -> dict[str, Any]:
    results = MemoryDropboxProcessor(db).process_once()
    return {
        "processed": len(results),
        "results": [
            {
                "source_path": str(result.source_path),
                "destination_path": str(result.destination_path),
                "preview_path": str(result.preview_path) if result.preview_path else None,
                "status": result.status,
                "candidate_count": result.candidate_count,
                "written_count": result.written_count,
                "pending_approval_count": result.pending_approval_count,
                "error": result.error,
            }
            for result in results
        ],
    }


@router.get("/dropbox/previews")
def list_dropbox_previews(domain_key: str | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    root = _dropbox_root()
    domain_keys = [domain_key] if domain_key else _domain_keys(db)
    previews: list[dict[str, Any]] = []
    for key in domain_keys:
        _validate_domain_key(db, key)
        preview_dir = root / key / "previews"
        if not preview_dir.exists():
            continue
        preview_paths = sorted(
            preview_dir.glob("*.preview.json"),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        for path in preview_paths:
            previews.append(_preview_payload(path, key))
    return {"previews": previews}


@router.get("/proposals/pending")
def list_pending_proposals(db: Session = Depends(get_db)) -> dict[str, Any]:
    proposals = MemoryService(db).list_pending_approvals()
    return {"proposals": [_proposal_payload(proposal) for proposal in proposals]}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        memory_item = MemoryService(db).approve_proposal(proposal_id)
    except MemoryAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "approved", "memory_item": _memory_item_payload(memory_item)}


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: uuid.UUID,
    request: RejectProposalRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        proposal = MemoryService(db).reject_proposal(proposal_id, reason=request.reason)
    except MemoryAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "rejected", "proposal": _proposal_payload(proposal)}


@router.get("/items")
def list_memory_items(limit: int = 20, db: Session = Depends(get_db)) -> dict[str, Any]:
    query = select(MemoryItem).order_by(MemoryItem.created_at.desc()).limit(limit)
    items = db.scalars(query).all()
    return {"items": [_memory_item_payload(item) for item in items]}


@router.get("/retrieve")
def retrieve_memory(
    audience: str = "maestro",
    domain_key: str | None = None,
    agent_id: uuid.UUID | None = None,
    query_text: str | None = None,
    memory_type: list[str] | None = Query(default=None),
    min_importance: float | None = None,
    include_agent_memory: bool = False,
    include_session_memory: bool = True,
    include_links: bool = True,
    limit: int = 12,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if audience not in {"maestro", "agent"}:
        raise HTTPException(status_code=400, detail="audience must be maestro or agent.")
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    try:
        result = MemoryRetrievalService(db).retrieve(
            MemoryRetrievalQuery(
                audience=audience,  # type: ignore[arg-type]
                domain_id=domain_id,
                agent_id=agent_id,
                query_text=query_text,
                memory_types=set(memory_type) if memory_type else None,
                min_importance=min_importance,
                include_agent_memory=include_agent_memory,
                include_session_memory=include_session_memory,
                include_links=include_links,
                limit=limit,
            )
        )
    except MemoryRetrievalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "query": {
            "audience": audience,
            "domain_key": domain_key,
            "agent_id": str(agent_id) if agent_id else None,
            "query_text": query_text,
            "memory_type": memory_type or [],
            "min_importance": min_importance,
            "include_agent_memory": include_agent_memory,
            "include_session_memory": include_session_memory,
            "include_links": include_links,
            "limit": limit,
        },
        "total_visible": result.total_visible,
        "results": [_retrieved_memory_payload(db, retrieved) for retrieved in result.results],
    }


@router.get("/sources")
def list_memory_sources(limit: int = 20, db: Session = Depends(get_db)) -> dict[str, Any]:
    query = select(SeedPackage).order_by(SeedPackage.created_at.desc()).limit(limit)
    seed_packages = db.scalars(query).all()
    return {"sources": [_source_payload(db, seed_package, include_generated=False) for seed_package in seed_packages]}


@router.get("/sources/{source_id}")
def get_memory_source(source_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    seed_package = db.get(SeedPackage, source_id)
    if seed_package is None:
        raise HTTPException(status_code=404, detail=f"Memory source {source_id} was not found.")
    return {"source": _source_payload(db, seed_package, include_generated=True)}


@router.post("/sources/{source_id}/reclassify")
def reclassify_memory_source(
    source_id: uuid.UUID,
    request: ReclassifySourceRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    seed_package = db.get(SeedPackage, source_id)
    if seed_package is None:
        raise HTTPException(status_code=404, detail=f"Memory source {source_id} was not found.")

    target_domain = None
    target_scope = "global"
    if request.target_domain_key != "global":
        _validate_domain_key(db, request.target_domain_key)
        target_domain = DomainRepository(db).get_by_key(request.target_domain_key)
        if target_domain is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown memory domain: {request.target_domain_key}",
            )
        target_scope = "domain"

    generated_memories = _items_for_seed_package(db, seed_package.id)
    generated_proposals = _proposals_for_seed_package(db, seed_package.id)
    reclassification = {
        "at": datetime.now(UTC).isoformat(),
        "target_domain_key": request.target_domain_key,
        "target_scope": target_scope,
        "reason": request.reason,
    }

    seed_package.domain_id = target_domain.id if target_domain is not None else None
    seed_package.metadata_ = _metadata_reclassified(
        seed_package.metadata_,
        reclassification=reclassification,
        target_domain_key=request.target_domain_key,
    )

    for item in generated_memories:
        item.scope = target_scope
        item.domain_id = target_domain.id if target_domain is not None else None
        item.agent_id = None
        item.metadata_ = _metadata_reclassified(
            item.metadata_,
            reclassification=reclassification,
            target_domain_key=request.target_domain_key,
        )

    for proposal in generated_proposals:
        proposal.scope = target_scope
        proposal.domain_id = target_domain.id if target_domain is not None else None
        proposal.agent_id = None
        proposal.metadata_ = _metadata_reclassified(
            proposal.metadata_,
            reclassification=reclassification,
            target_domain_key=request.target_domain_key,
        )

    db.commit()
    db.refresh(seed_package)
    return {"source": _source_payload(db, seed_package, include_generated=True)}


def _dropbox_root() -> Path:
    return Path(get_settings().memory_dropbox_root)


def _domain_keys(db: Session) -> list[str]:
    seed_default_domains(db)
    return ["global"] + [domain.key for domain in DomainRepository(db).list_active()]


def _validate_domain_key(db: Session, domain_key: str) -> None:
    if domain_key not in _domain_keys(db):
        raise HTTPException(status_code=404, detail=f"Unknown memory domain: {domain_key}")


def _domain_status(root: Path, domain_key: str) -> dict[str, Any]:
    return {
        "key": domain_key,
        "inbox": _folder_count(root / domain_key / "inbox", supported_only=True),
        "processed": _folder_count(root / domain_key / "processed"),
        "failed": _folder_count(root / domain_key / "failed"),
        "previews": _folder_count(root / domain_key / "previews", pattern="*.preview.json"),
    }


def _folder_count(path: Path, *, supported_only: bool = False, pattern: str = "*") -> int:
    if not path.exists():
        return 0
    paths = [candidate for candidate in path.glob(pattern) if candidate.is_file()]
    if supported_only:
        return sum(1 for candidate in paths if candidate.suffix.lower() in SUPPORTED_DROPBOX_SUFFIXES)
    return len(paths)


def _available_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination
    counter = 1
    while True:
        candidate = destination.with_name(f"{destination.stem}-{counter}{destination.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _preview_payload(path: Path, domain_key: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {"status": "invalid", "candidates": [], "results": []}
    return {
        "domain_key": domain_key,
        "filename": path.name,
        "path": str(path),
        "source_file": payload.get("source_file"),
        "status": payload.get("status"),
        "generated_at": payload.get("generated_at"),
        "candidate_count": len(payload.get("candidates", [])),
        "written_count": sum(
            1 for result in payload.get("results", []) if result.get("memory_item_id")
        ),
        "deduped_count": sum(
            1
            for result in payload.get("results", [])
            if result.get("outcome") in {"duplicate_skipped", "reinforced"}
        ),
        "pending_approval_count": sum(
            1
            for result in payload.get("results", [])
            if result.get("outcome") == "pending_user_approval"
        ),
        "payload": payload,
    }


def _proposal_payload(proposal: MemoryProposal) -> dict[str, Any]:
    return {
        "id": str(proposal.id),
        "scope": proposal.scope,
        "memory_type": proposal.memory_type,
        "title": proposal.title,
        "content": proposal.content,
        "rationale": proposal.rationale,
        "impact_level": proposal.impact_level,
        "status": proposal.status,
        "source_refs": proposal.source_refs,
        "metadata": proposal.metadata_,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
    }


def _memory_item_payload(item: MemoryItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "scope": item.scope,
        "memory_type": item.memory_type,
        "title": item.title,
        "content": item.content,
        "impact_level": item.impact_level,
        "importance": item.importance,
        "metadata": item.metadata_,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _source_payload(
    db: Session,
    seed_package: SeedPackage,
    *,
    include_generated: bool,
) -> dict[str, Any]:
    memories = _items_for_seed_package(db, seed_package.id)
    proposals = _proposals_for_seed_package(db, seed_package.id)
    payload = {
        "id": str(seed_package.id),
        "name": seed_package.name,
        "source_type": seed_package.source_type,
        "status": seed_package.status,
        "domain_key": _domain_key_for_id(db, seed_package.domain_id),
        "metadata": seed_package.metadata_,
        "memory_count": len(memories),
        "proposal_count": len(proposals),
        "created_at": seed_package.created_at.isoformat() if seed_package.created_at else None,
        "processed_at": seed_package.processed_at.isoformat() if seed_package.processed_at else None,
    }
    if include_generated:
        payload["memories"] = [_memory_item_payload(item) for item in memories]
        payload["proposals"] = [_proposal_payload(proposal) for proposal in proposals]
    return payload


def _items_for_seed_package(db: Session, seed_package_id: uuid.UUID) -> list[MemoryItem]:
    items = db.scalars(select(MemoryItem).order_by(MemoryItem.created_at.desc())).all()
    return [
        item
        for item in items
        if item.metadata_.get("seed_package_id") == str(seed_package_id)
    ]


def _proposals_for_seed_package(db: Session, seed_package_id: uuid.UUID) -> list[MemoryProposal]:
    proposals = db.scalars(select(MemoryProposal).order_by(MemoryProposal.created_at.desc())).all()
    return [
        proposal
        for proposal in proposals
        if proposal.metadata_.get("seed_package_id") == str(seed_package_id)
    ]


def _domain_key_for_id(db: Session, domain_id: uuid.UUID | None) -> str:
    if domain_id is None:
        return "global"
    domain = DomainRepository(db).get(domain_id)
    return domain.key if domain is not None else "unknown"


def _domain_id_for_key(db: Session, domain_key: str | None) -> uuid.UUID | None:
    if domain_key is None or domain_key == "global":
        return None
    _validate_domain_key(db, domain_key)
    domain = DomainRepository(db).get_by_key(domain_key)
    if domain is None:
        raise HTTPException(status_code=404, detail=f"Unknown memory domain: {domain_key}")
    return domain.id


def _retrieved_memory_payload(db: Session, retrieved: RetrievedMemory) -> dict[str, Any]:
    payload = _memory_item_payload(retrieved.memory)
    payload["domain_key"] = _domain_key_for_id(db, retrieved.memory.domain_id)
    payload["agent_id"] = str(retrieved.memory.agent_id) if retrieved.memory.agent_id else None
    payload["score"] = retrieved.score
    payload["score_reasons"] = retrieved.score_reasons
    payload["provenance"] = {
        "source_refs": retrieved.provenance.source_refs,
        "seed_package": retrieved.provenance.seed_package,
        "artifact": retrieved.provenance.artifact,
        "processed_path": retrieved.provenance.processed_path,
    }
    payload["links"] = [_retrieved_link_payload(db, link) for link in retrieved.links]
    return payload


def _retrieved_link_payload(db: Session, link: RetrievedMemoryLink) -> dict[str, Any]:
    return {
        "relation_type": link.relation_type,
        "direction": link.direction,
        "metadata": link.metadata,
        "memory": {
            **_memory_item_payload(link.memory),
            "domain_key": _domain_key_for_id(db, link.memory.domain_id),
        },
    }


def _metadata_reclassified(
    metadata: dict[str, Any] | None,
    *,
    reclassification: dict[str, Any],
    target_domain_key: str,
) -> dict[str, Any]:
    updated = dict(metadata or {})
    history = list(updated.get("reclassification_history", []))
    history.append(reclassification)
    updated["reclassification_history"] = history
    updated["dropbox_domain"] = target_domain_key
    return updated
