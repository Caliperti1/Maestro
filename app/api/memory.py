import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import MemoryItem, MemoryProposal
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.memory.document_extract import SUPPORTED_DROPBOX_SUFFIXES
from app.memory.dropbox import MemoryDropboxProcessor
from app.memory.service import MemoryAccessError, MemoryService

router = APIRouter(prefix="/memory", tags=["memory"])


class RejectProposalRequest(BaseModel):
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
