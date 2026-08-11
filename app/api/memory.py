import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.time import ensure_aware_utc, home_isoformat, home_timezone
from app.db.models import (
    Artifact,
    CalendarEvent,
    Contact,
    ContactHydrationJob,
    DecisionRecord,
    Domain,
    Entity,
    Idea,
    IngestionRecord,
    MemoryItem,
    MemoryHygieneRun,
    MemoryProposal,
    RoutedItem,
    SeedPackage,
    SourceRegistration,
    Todo,
)
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.memory.document_extract import SUPPORTED_DROPBOX_SUFFIXES
from app.memory.calendar_intelligence import CalendarIntelligenceService
from app.memory.contact_intelligence import ContactEmbeddingService, ContactIntelligenceService
from app.memory.contact_hydration import ContactHydrationError, ContactHydrationService
from app.memory.dropbox import MemoryDropboxProcessor
from app.memory.ingestion import IngestionLedgerService
from app.memory.embeddings import MemoryEmbeddingService
from app.memory.chatgpt_import import ChatGPTExportImporter
from app.memory.context_gateway import (
    ContextGatewayService,
    GatewayItem,
    parse_sanitized_context_manifest,
)
from app.memory.hygiene import DurableMemoryHygieneService
from app.memory.ingestion import SourcePolicy
from app.memory.repository_observer import RepositoryObserverService
from app.memory.federated_retrieval import (
    FederatedIndexService,
    FederatedRetrievalRequest,
    FederatedRetrievalService,
    federated_bundle_payload,
)
from app.memory.retrieval import (
    MemoryContextBundle,
    MemoryContextBundleRequest,
    MemoryContextSection,
    MemoryContextSnippet,
    MemoryRetrievalError,
    MemoryRetrievalQuery,
    MemoryRetrievalService,
    RetrievedMemory,
    RetrievedMemoryLink,
)
from app.memory.service import MemoryAccessError, MemoryService
from app.memory.routed_hygiene import RoutedHygieneService
from app.memory.routed_retrieval import (
    ContactAliasConflictError,
    RoutedEditService,
    RoutedRetrievalService,
)
from app.memory.routed_service import RoutedMemoryService
from app.memory.organization_intelligence import OrganizationEmbeddingService, OrganizationIntelligenceService

router = APIRouter(prefix="/memory", tags=["memory"])
CALENDAR_EVENT_STATUSES = {"scheduled", "tentative", "cancelled", "archived"}


class RejectProposalRequest(BaseModel):
    reason: str | None = None


class ArchiveMemoryRequest(BaseModel):
    reason: str | None = None


class UpdateRoutedItemRequest(BaseModel):
    status: str
    reason: str | None = None


class PromoteRoutedItemsRequest(BaseModel):
    limit: int = 100


class UpdateRoutedObjectRequest(BaseModel):
    updates: dict[str, Any]


class CreateCalendarEventRequest(BaseModel):
    domain_key: str
    title: str = Field(min_length=1, max_length=240)
    summary: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str = "America/New_York"
    all_day: bool = False
    recurrence_rule: str | None = None
    location: str | None = None
    conferencing_url: str | None = None
    attendees: list[dict[str, Any]] = Field(default_factory=list)
    organizations: list[dict[str, Any]] = Field(default_factory=list)


class RepositorySourceRequest(BaseModel):
    key: str = Field(min_length=3, max_length=200)
    domain_key: str
    path: str
    display_name: str | None = None


class RepositoryObservationRequest(BaseModel):
    force_full: bool = False


class MergeContactRequest(BaseModel):
    duplicate_contact_id: uuid.UUID


class MergeOrganizationRequest(BaseModel):
    duplicate_organization_id: uuid.UUID


class CreateContactHydrationRequest(BaseModel):
    domain_key: str
    query: str = "in:sent newer_than:90d"
    page_size: int = 25
    max_messages: int = 200
    max_contacts: int = 100
    enable_enrichment: bool = True
    enable_cloud_fallback: bool = False
    max_cloud_calls: int = 0


class ContactHydrationActionRequest(BaseModel):
    action: str


class ApproveContactHydrationRequest(BaseModel):
    candidate_ids: list[uuid.UUID] | None = None
    minimum_confidence: float = 0.8


class ReviewContactHydrationCandidateRequest(BaseModel):
    decision: str
    existing_object_id: uuid.UUID | None = None


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


@router.get("/ingestion/status")
def get_ingestion_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    service = IngestionLedgerService(db)
    recent = db.execute(
        select(IngestionRecord, SourceRegistration)
        .join(
            SourceRegistration,
            SourceRegistration.id == IngestionRecord.source_registration_id,
        )
        .order_by(IngestionRecord.created_at.desc())
        .limit(12)
    ).all()
    return {
        **service.status(),
        "recent": [
            _ingestion_record_payload(record, registration) for record, registration in recent
        ],
    }


@router.get("/ingestion/records")
def list_ingestion_records(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    statement = (
        select(IngestionRecord, SourceRegistration)
        .join(
            SourceRegistration,
            SourceRegistration.id == IngestionRecord.source_registration_id,
        )
        .order_by(IngestionRecord.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    if status:
        statement = statement.where(IngestionRecord.status == status)
    rows = db.execute(statement).all()
    return {
        "records": [
            _ingestion_record_payload(record, registration) for record, registration in rows
        ]
    }


@router.post("/ingestion/recover")
def recover_stale_ingestion(db: Session = Depends(get_db)) -> dict[str, Any]:
    recovered = IngestionLedgerService(db).recover_stale()
    return {"status": "recovered", "recovered_count": recovered}


@router.get("/ingestion/sources")
def list_context_sources(db: Session = Depends(get_db)) -> dict[str, Any]:
    registrations = db.scalars(select(SourceRegistration).order_by(SourceRegistration.display_name)).all()
    domains = {domain.id: domain.key for domain in db.scalars(select(Domain)).all()}
    return {
        "sources": [
            {
                "id": str(source.id),
                "key": source.key,
                "source_system": source.source_system,
                "display_name": source.display_name,
                "adapter_type": source.adapter_type,
                "domain_key": domains.get(source.domain_id),
                "policy": source.policy,
                "config": source.config,
                "is_active": source.is_active,
            }
            for source in registrations
        ]
    }


@router.post("/ingestion/sources/repositories")
def register_repository_source(
    body: RepositorySourceRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    domain = DomainRepository(db).get_by_key(body.domain_key)
    if domain is None:
        raise HTTPException(status_code=404, detail=f"Unknown domain: {body.domain_key}")
    try:
        registration = RepositoryObserverService(db).register(
            key=body.key,
            path=body.path,
            domain=domain,
            display_name=body.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": str(registration.id), "key": registration.key, "config": registration.config}


@router.post("/ingestion/sources/repositories/{source_key}/observe")
def observe_repository_source(
    source_key: str,
    body: RepositoryObservationRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    registration = db.scalar(select(SourceRegistration).where(SourceRegistration.key == source_key))
    if registration is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_key}")
    try:
        result = RepositoryObserverService(db).observe(registration, force_full=body.force_full)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": result.status,
        "repository_key": result.repository_key,
        "commit": result.commit,
        "previous_commit": result.previous_commit,
        "mode": result.mode,
        "changed_files": result.changed_files,
        "gateway": result.gateway.__dict__ if result.gateway else None,
    }


@router.post("/ingestion/sanitized-context")
async def ingest_sanitized_context(
    file: UploadFile = File(...),
    domain_key: str = "usma",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    domain = DomainRepository(db).get_by_key(domain_key)
    if domain is None or domain_key not in {"usma", "l3"}:
        raise HTTPException(status_code=404, detail="Sanitized context destination must be usma or l3.")
    try:
        text = (await file.read()).decode("utf-8")
        metadata, body = parse_sanitized_context_manifest(text, expected_domain=domain_key)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        source_timestamp = datetime.fromisoformat(metadata["reviewed_at"].replace("Z", "+00:00"))
        result = ContextGatewayService(db).ingest(
            GatewayItem(
                source_registration_key=f"sanitized-context:{domain_key}",
                source_system=metadata["source_system"],
                external_id=metadata.get("source_id") or Path(file.filename or "context.md").name,
                source_version=content_hash,
                content_type="sanitized_context_manifest",
                domain_key=domain_key,
                title=metadata.get("title") or f"{domain.name} sanitized context",
                content=text,
                source_timestamp=source_timestamp,
                policy=SourcePolicy(sensitivity="sanitized_work_context", trust_level="user_reviewed", transfer_method="sanitized_context_drop", egress_policy="external_allowed"),
                metadata={"reviewed_by": metadata["reviewed_by"], "contains_restricted": False, "body_chars": len(body)},
            ),
            domain=domain,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.__dict__


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
                "routed_count": result.routed_count,
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


@router.get("/routed-items")
def list_routed_items(
    domain_key: str | None = None,
    route_type: str | None = None,
    status: str | None = "open",
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    query = select(RoutedItem)
    if domain_id is not None:
        query = query.where(RoutedItem.domain_id == domain_id)
    if route_type is not None:
        query = query.where(RoutedItem.route_type == route_type)
    if status is not None and status != "all":
        query = query.where(RoutedItem.status == status)
    items = db.scalars(query.order_by(RoutedItem.created_at.desc()).limit(limit)).all()
    return {"items": [_routed_item_payload(db, item) for item in items]}


@router.post("/routed-items/promote")
def promote_pending_routed_items(
    request: PromoteRoutedItemsRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    results = RoutedMemoryService(db).process_pending(limit=request.limit)
    return {
        "promoted": [
            {
                "routed_item_id": str(result.routed_item_id),
                "route_type": result.route_type,
                "object_type": result.object_type,
                "object_id": str(result.object_id),
                "action": result.action,
            }
            for result in results
        ]
    }


@router.patch("/routed-items/{item_id}")
def update_routed_item(
    item_id: uuid.UUID,
    request: UpdateRoutedItemRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    allowed_statuses = {"open", "done", "archived"}
    if request.status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(sorted(allowed_statuses))}.",
        )
    item = db.get(RoutedItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Routed item {item_id} was not found.")
    item.status = request.status
    if request.reason:
        item.metadata_ = {
            **(item.metadata_ or {}),
            "last_status_reason": request.reason,
            "last_status_change_at": datetime.now(UTC).isoformat(),
        }
    db.commit()
    db.refresh(item)
    return {"status": "updated", "item": _routed_item_payload(db, item)}


@router.get("/routed-objects")
def list_routed_objects(
    domain_key: str | None = None,
    query_text: str | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    RoutedMemoryService(db, enable_llm_resolver=False).process_pending(limit=100)
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    return RoutedMemoryService(db).build_context_bundle(
        domain_id=domain_id,
        query_text=query_text,
        limit=limit,
    )


@router.get("/routed-context")
def routed_context_bundle(
    domain_key: str | None = None,
    query_text: str | None = None,
    limit: int = 12,
    max_chars: int = 3000,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    RoutedMemoryService(db, enable_llm_resolver=False).process_pending(limit=100)
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    bundle = RoutedRetrievalService(db).build_context_bundle(
        domain_id=domain_id,
        query_text=query_text,
        limit=limit,
        max_chars=max_chars,
    )
    return {
        "query_text": bundle.query_text,
        "domain_key": domain_key,
        "stores": bundle.stores,
        "rendered_text": bundle.rendered_text,
    }


@router.post("/routed-hygiene/run")
def run_routed_hygiene(db: Session = Depends(get_db)) -> dict[str, Any]:
    report = RoutedHygieneService(db).run_once()
    return {
        "aliases_backfilled": report.aliases_backfilled,
        "organization_identifiers_backfilled": report.organization_identifiers_backfilled,
        "aliases_pruned": report.aliases_pruned,
        "display_fields_canonicalized": report.display_fields_canonicalized,
        "duplicates_merged": report.duplicates_merged,
        "suggestions": report.suggestions,
    }


@router.get("/routed-objects/events")
def list_calendar_events(
    domain_key: str | None = None,
    status: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    RoutedMemoryService(db, enable_llm_resolver=False).process_pending(limit=100)
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    query = select(CalendarEvent)
    if domain_id is not None:
        query = query.where(CalendarEvent.domain_id == domain_id)
    if status is not None:
        query = query.where(CalendarEvent.status == status)
    if start_at is not None:
        query = query.where(or_(CalendarEvent.end_at.is_(None), CalendarEvent.end_at >= start_at))
    if end_at is not None:
        query = query.where(CalendarEvent.start_at < end_at)
    events = db.scalars(query.order_by(CalendarEvent.start_at, CalendarEvent.created_at.desc()).limit(limit)).all()
    calendar = CalendarIntelligenceService(db)
    for event in events:
        calendar.ensure_links(event)
    db.commit()
    return {"events": [_calendar_event_payload(db, event) for event in events]}


@router.post("/routed-objects/events")
def create_calendar_event(
    body: CreateCalendarEventRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    domain_id = _domain_id_for_key(db, body.domain_key)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Event title cannot be empty.")
    start_at = _ensure_aware(body.start_at)
    end_at = _ensure_aware(body.end_at)
    _validate_calendar_window(start_at, end_at, body.timezone)
    event = CalendarEvent(
        domain_id=domain_id,
        title=title,
        summary=body.summary,
        start_at=start_at,
        end_at=end_at,
        timezone=body.timezone,
        all_day=body.all_day,
        recurrence_rule=body.recurrence_rule,
        location=body.location,
        conferencing_url=body.conferencing_url,
        attendees=[],
        supporting_refs=[],
        source_refs=[],
        provenance={"source": "maestro_calendar_ui"},
        status="scheduled",
        metadata_={"created_in_ui": True},
    )
    db.add(event)
    db.flush()
    calendar = CalendarIntelligenceService(db)
    calendar.replace_attendees(event, body.attendees, commit=False)
    calendar.replace_organizations(event, body.organizations, commit=False)
    db.commit()
    db.refresh(event)
    return {"event": _calendar_event_payload(db, event)}


@router.patch("/routed-objects/events/{event_id}")
def update_calendar_event(
    event_id: uuid.UUID,
    body: UpdateRoutedObjectRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if "title" in body.updates and not str(body.updates.get("title") or "").strip():
        raise HTTPException(status_code=422, detail="Event title cannot be empty.")
    if "status" in body.updates and body.updates.get("status") not in CALENDAR_EVENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="Event status must be scheduled, tentative, cancelled, or archived.",
        )
    current = db.get(CalendarEvent, event_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Event not found.")
    proposed_start = _coerce_calendar_datetime(body.updates.get("start_at")) if "start_at" in body.updates else current.start_at
    proposed_end = _coerce_calendar_datetime(body.updates.get("end_at")) if "end_at" in body.updates else current.end_at
    proposed_timezone = str(body.updates.get("timezone") or current.timezone)
    _validate_calendar_window(proposed_start, proposed_end, proposed_timezone)
    try:
        event = RoutedEditService(db).update_event(event_id, body.updates)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"event": _calendar_event_payload(db, event)}


@router.get("/routed-objects/todos")
def list_todos(
    domain_key: str | None = None,
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    RoutedMemoryService(db, enable_llm_resolver=False).process_pending(limit=100)
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    query = select(Todo)
    if domain_id is not None:
        query = query.where(Todo.domain_id == domain_id)
    if status is not None:
        query = query.where(Todo.status == status)
    todos = db.scalars(query.order_by(Todo.due_at, Todo.created_at.desc()).limit(limit)).all()
    return {"todos": [_todo_payload(db, todo) for todo in todos]}


@router.patch("/routed-objects/todos/{todo_id}")
def update_todo(
    todo_id: uuid.UUID,
    body: UpdateRoutedObjectRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        todo = RoutedEditService(db).update_todo(todo_id, body.updates)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"todo": _todo_payload(db, todo)}


@router.get("/routed-objects/contacts")
def list_contacts(
    limit: int = 50,
    query_text: str | None = None,
    domain_key: str | None = None,
    use_semantic: bool = True,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    RoutedMemoryService(db, enable_llm_resolver=False).process_pending(limit=100)
    domain_id = None
    if domain_key:
        domain = DomainRepository(db).get_by_key(domain_key)
        if domain is None:
            raise HTTPException(status_code=404, detail="Domain not found.")
        domain_id = domain.id
    service = ContactIntelligenceService(db)
    if query_text:
        results = service.search(
            query_text,
            domain_id=domain_id,
            limit=limit,
            use_semantic=use_semantic,
        )
        return {
            "contacts": [
                {
                    **result.payload,
                    "retrieval_score": result.score,
                    "match_reasons": result.match_reasons,
                    "semantic_similarity": result.semantic_similarity,
                }
                for result in results
            ]
        }
    contacts = service.search("", domain_id=domain_id, limit=limit, use_semantic=False)
    return {"contacts": [result.payload for result in contacts]}


@router.post("/routed-objects/contacts/embeddings/backfill")
def backfill_contact_embeddings(
    limit: int | None = Query(default=None, ge=1, le=10000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ContactEmbeddingService(db).backfill(limit=limit)


@router.get("/routed-objects/contacts/{contact_id}")
def get_contact(
    contact_id: uuid.UUID,
    domain_key: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    domain_id = None
    if domain_key:
        domain = DomainRepository(db).get_by_key(domain_key)
        if domain is None:
            raise HTTPException(status_code=404, detail="Domain not found.")
        domain_id = domain.id
    try:
        payload = ContactIntelligenceService(db).get(contact_id, domain_id=domain_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"contact": payload}


@router.patch("/routed-objects/contacts/{contact_id}")
def update_contact(
    contact_id: uuid.UUID,
    body: UpdateRoutedObjectRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        contact = RoutedEditService(db).update_contact(contact_id, body.updates)
    except ContactAliasConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    ContactEmbeddingService(db).upsert(contact)
    db.commit()
    return {"contact": _contact_payload(db, contact)}


@router.post("/routed-objects/contacts/{contact_id}/merge")
def merge_contact(
    contact_id: uuid.UUID,
    body: MergeContactRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    survivor = db.get(Contact, contact_id)
    duplicate = db.get(Contact, body.duplicate_contact_id)
    if survivor is None or duplicate is None:
        raise HTTPException(status_code=404, detail="Contact not found.")
    if survivor.id == duplicate.id:
        raise HTTPException(status_code=400, detail="A contact cannot be merged into itself.")
    RoutedHygieneService(db).merge_contacts(survivor, duplicate, commit=False)
    ContactEmbeddingService(db).upsert(survivor)
    db.commit()
    return {"contact": _contact_payload(db, survivor), "merged_contact_id": str(duplicate.id)}


@router.patch("/routed-objects/{object_type}/{object_id}/archive")
def archive_routed_object(
    object_type: str,
    object_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        obj = RoutedEditService(db).archive_object(object_type, object_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "archived", "object_type": object_type, "object_id": str(obj.id)}


@router.post("/contact-hydration/jobs")
def create_contact_hydration_job(
    body: CreateContactHydrationRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        domain_id = _domain_id_for_key(db, body.domain_key)
        job = ContactHydrationService(db).create_job(
            domain_id=domain_id,
            query=body.query,
            page_size=body.page_size,
            max_messages=body.max_messages,
            max_contacts=body.max_contacts,
            enable_enrichment=body.enable_enrichment,
            enable_cloud_fallback=body.enable_cloud_fallback,
            max_cloud_calls=body.max_cloud_calls,
        )
    except (ContactHydrationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": ContactHydrationService(db).job_payload(job)}


@router.get("/contact-hydration/jobs")
def list_contact_hydration_jobs(
    domain_key: str | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    service = ContactHydrationService(db)
    return {"jobs": [service.job_payload(job) for job in service.list_jobs(domain_id=domain_id, limit=limit)]}


@router.get("/contact-hydration/jobs/{job_id}")
def get_contact_hydration_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(ContactHydrationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Contact hydration job not found.")
    return {"job": ContactHydrationService(db).job_payload(job)}


@router.get("/contact-hydration/jobs/{job_id}/candidates")
def list_contact_hydration_candidates(
    job_id: uuid.UUID,
    candidate_type: str | None = None,
    status: str | None = None,
    limit: int = 500,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = ContactHydrationService(db)
    if db.get(ContactHydrationJob, job_id) is None:
        raise HTTPException(status_code=404, detail="Contact hydration job not found.")
    return {
        "candidates": [
            service.candidate_payload(candidate)
            for candidate in service.candidates(
                job_id,
                candidate_type=candidate_type,
                status=status,
                limit=limit,
            )
        ]
    }


@router.post("/contact-hydration/jobs/{job_id}/action")
def action_contact_hydration_job(
    job_id: uuid.UUID,
    body: ContactHydrationActionRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = ContactHydrationService(db)
    try:
        job = service.update_job_status(job_id, body.action)
    except ContactHydrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": service.job_payload(job)}


@router.post("/contact-hydration/jobs/{job_id}/approve")
def approve_contact_hydration_candidates(
    job_id: uuid.UUID,
    body: ApproveContactHydrationRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = ContactHydrationService(db)
    try:
        job = service.approve_candidates(
            job_id,
            candidate_ids=body.candidate_ids,
            minimum_confidence=body.minimum_confidence,
        )
    except ContactHydrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": service.job_payload(job)}


@router.post("/contact-hydration/candidates/{candidate_id}/review")
def review_contact_hydration_candidate(
    candidate_id: uuid.UUID,
    body: ReviewContactHydrationCandidateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = ContactHydrationService(db)
    try:
        candidate = service.review_candidate(
            candidate_id,
            decision=body.decision,
            existing_object_id=body.existing_object_id,
        )
    except ContactHydrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"candidate": service.candidate_payload(candidate)}


@router.post("/contact-hydration/process-once")
def process_contact_hydration_once(db: Session = Depends(get_db)) -> dict[str, Any]:
    job = ContactHydrationService(db).process_once(owner="contact-hydration-api")
    return {"job": ContactHydrationService(db).job_payload(job) if job else None}


@router.get("/routed-objects/entities")
def list_entities(
    query_text: str | None = None,
    domain_key: str | None = None,
    use_semantic: bool = True,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    RoutedMemoryService(db, enable_llm_resolver=False).process_pending(limit=100)
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    service = OrganizationIntelligenceService(db)
    results = service.search(
        query_text or "",
        domain_id=domain_id,
        limit=limit,
        use_semantic=use_semantic,
    )
    return {
        "entities": [
            {
                **result.payload,
                "retrieval_score": round(result.score, 4),
                "match_reasons": result.match_reasons,
                "semantic_similarity": result.semantic_similarity,
            }
            for result in results
        ]
    }


@router.get("/routed-objects/entities/{entity_id}")
def get_entity(
    entity_id: uuid.UUID,
    domain_key: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    try:
        entity = OrganizationIntelligenceService(db).get(entity_id, domain_id=domain_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"entity": entity}


@router.patch("/routed-objects/entities/{entity_id}")
def update_entity(
    entity_id: uuid.UUID,
    body: UpdateRoutedObjectRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        entity = RoutedEditService(db).update_entity(entity_id, body.updates)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    OrganizationEmbeddingService(db).upsert(entity)
    db.commit()
    return {"entity": OrganizationIntelligenceService(db).organization_payload(entity)}


@router.post("/routed-objects/entities/{entity_id}/merge")
def merge_entity(
    entity_id: uuid.UUID,
    body: MergeOrganizationRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    survivor = db.get(Entity, entity_id)
    duplicate = db.get(Entity, body.duplicate_organization_id)
    if survivor is None or duplicate is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    if survivor.id == duplicate.id:
        raise HTTPException(status_code=400, detail="An organization cannot be merged into itself.")
    RoutedHygieneService(db).merge_entities(survivor, duplicate, commit=False)
    OrganizationEmbeddingService(db).upsert(survivor)
    db.commit()
    return {
        "entity": OrganizationIntelligenceService(db).organization_payload(survivor),
        "merged_organization_id": str(duplicate.id),
    }


@router.post("/routed-objects/entities/embedding-backfill")
def backfill_organization_embeddings(
    limit: int = 200,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = OrganizationEmbeddingService(db).backfill(limit=limit)
    db.commit()
    return result


@router.post("/hygiene/run")
def run_durable_memory_hygiene(db: Session = Depends(get_db)) -> dict[str, Any]:
    run = DurableMemoryHygieneService(db).run()
    return _memory_hygiene_payload(run)


@router.get("/hygiene/runs")
def list_durable_memory_hygiene_runs(
    limit: int = 20,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    runs = db.scalars(select(MemoryHygieneRun).order_by(MemoryHygieneRun.started_at.desc()).limit(limit)).all()
    return {"runs": [_memory_hygiene_payload(run) for run in runs]}


@router.post("/imports/chatgpt")
async def import_chatgpt_export(
    file: UploadFile = File(...),
    domain_key: str = "personal",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    filename = Path(file.filename or "conversations.json").name
    if not filename.lower().endswith((".json", ".zip")):
        raise HTTPException(status_code=400, detail="Upload a ChatGPT export ZIP or conversations.json.")
    if DomainRepository(db).get_by_key(domain_key) is None:
        raise HTTPException(status_code=404, detail=f"Unknown domain: {domain_key}")
    try:
        result = ChatGPTExportImporter(db).import_bytes(await file.read(), filename=filename, default_domain_key=domain_key)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.__dict__


@router.get("/routed-objects/ideas")
def list_ideas(
    domain_key: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    query = select(Idea)
    if domain_id is not None:
        query = query.where(Idea.domain_id == domain_id)
    ideas = db.scalars(query.order_by(Idea.updated_at.desc()).limit(limit)).all()
    return {"ideas": [_idea_payload(db, idea) for idea in ideas]}


@router.patch("/routed-objects/ideas/{idea_id}")
def update_idea(
    idea_id: uuid.UUID,
    body: UpdateRoutedObjectRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        idea = RoutedEditService(db).update_idea(idea_id, body.updates)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"idea": _idea_payload(db, idea)}


@router.get("/routed-objects/decisions")
def list_decisions(
    domain_key: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    query = select(DecisionRecord)
    if domain_id is not None:
        query = query.where(DecisionRecord.domain_id == domain_id)
    decisions = db.scalars(query.order_by(DecisionRecord.updated_at.desc()).limit(limit)).all()
    return {"decisions": [_decision_payload(db, decision) for decision in decisions]}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        memory_item = MemoryService(
            db,
            embedding_service=MemoryEmbeddingService(db),
        ).approve_proposal(proposal_id)
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
def list_memory_items(
    limit: int = 20,
    include_archived: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(MemoryItem)
    if not include_archived:
        now = datetime.now(UTC)
        query = query.where(or_(MemoryItem.valid_until.is_(None), MemoryItem.valid_until > now))
    query = query.order_by(MemoryItem.created_at.desc()).limit(limit)
    items = db.scalars(query).all()
    return {"items": [_memory_item_payload(item) for item in items]}


@router.get("/artifacts")
def list_memory_artifacts(
    limit: int = 20,
    canonical_only: bool = True,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(Artifact).order_by(Artifact.created_at.desc()).limit(limit * 3)
    artifacts = db.scalars(query).all()
    if canonical_only:
        artifacts = [
            artifact
            for artifact in artifacts
            if _artifact_is_canonical_memory_source(artifact)
        ]
    return {"artifacts": [_artifact_payload(db, artifact) for artifact in artifacts[:limit]]}


@router.delete("/items/{memory_item_id}")
def archive_memory_item(
    memory_item_id: uuid.UUID,
    request: ArchiveMemoryRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        memory_item = MemoryService(db).archive_memory_item(memory_item_id, reason=request.reason)
    except MemoryAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "archived", "memory_item": _memory_item_payload(memory_item)}


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
    use_semantic: bool = True,
    mode: str = "balanced",
    egress_target: str = "human",
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
                use_semantic=use_semantic,
                mode=mode,  # type: ignore[arg-type]
                egress_target=egress_target,  # type: ignore[arg-type]
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
            "use_semantic": use_semantic,
            "mode": mode,
            "egress_target": egress_target,
            "limit": limit,
        },
        "total_visible": result.total_visible,
        "filtered_count": result.filtered_count,
        "policy_filtered_count": result.policy_filtered_count,
        "semantic_status": result.semantic_status,
        "results": [_retrieved_memory_payload(db, retrieved) for retrieved in result.results],
    }


@router.get("/federated-retrieve")
def retrieve_federated_context(
    query_text: str,
    audience: str = "maestro",
    domain_key: str | None = None,
    agent_id: uuid.UUID | None = None,
    store: list[str] | None = Query(default=None),
    egress_target: str = "external",
    use_semantic: bool = True,
    max_items: int = 12,
    max_chars: int = 5000,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if audience not in {"maestro", "agent"}:
        raise HTTPException(status_code=400, detail="audience must be maestro or agent.")
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    try:
        bundle = FederatedRetrievalService(db).retrieve(
            FederatedRetrievalRequest(
                query_text=query_text,
                audience=audience,  # type: ignore[arg-type]
                domain_id=domain_id,
                agent_id=agent_id,
                egress_target=egress_target,  # type: ignore[arg-type]
                stores=set(store) if store else None,
                use_semantic=use_semantic,
                max_items=max_items,
                max_chars=max_chars,
            )
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return federated_bundle_payload(bundle)


@router.post("/federated-index/sync")
def sync_federated_index(
    embed_missing: bool = True,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = FederatedIndexService(db).sync(embed_missing=embed_missing)
    db.commit()
    return result.__dict__


@router.get("/context-bundle")
def build_memory_context_bundle(
    profile: str = "agent_prompt",
    audience: str = "agent",
    domain_key: str | None = None,
    agent_id: uuid.UUID | None = None,
    query_text: str | None = None,
    memory_type: list[str] | None = Query(default=None),
    min_importance: float | None = None,
    use_semantic: bool = True,
    egress_target: str = "human",
    max_items: int = 12,
    max_chars: int = 4000,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if audience not in {"maestro", "agent"}:
        raise HTTPException(status_code=400, detail="audience must be maestro or agent.")
    domain_id = _domain_id_for_key(db, domain_key) if domain_key else None
    try:
        bundle = MemoryRetrievalService(db).build_context_bundle(
            MemoryContextBundleRequest(
                profile=profile,  # type: ignore[arg-type]
                audience=audience,  # type: ignore[arg-type]
                domain_id=domain_id,
                agent_id=agent_id,
                query_text=query_text,
                memory_types=set(memory_type) if memory_type else None,
                min_importance=min_importance,
                use_semantic=use_semantic,
                egress_target=egress_target,  # type: ignore[arg-type]
                max_items=max_items,
                max_chars=max_chars,
            )
        )
    except MemoryRetrievalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _context_bundle_payload(db, bundle, domain_key=domain_key)


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
        "processing": _folder_count(root / domain_key / "processing", supported_only=True),
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
        payload = {"status": "invalid", "candidates": [], "routed_items": [], "results": []}
    candidates = payload.get("candidates", [])
    routed_items = payload.get("routed_items", [])
    results = payload.get("results", [])
    candidate_count = len(candidates)
    result_count = len(results)
    written_count = sum(1 for result in results if result.get("memory_item_id"))
    deduped_count = sum(
        1
        for result in results
        if result.get("outcome") in {"duplicate_skipped", "reinforced"}
    )
    pending_approval_count = sum(
        1 for result in results if result.get("outcome") == "pending_user_approval"
    )
    return {
        "domain_key": domain_key,
        "filename": path.name,
        "path": str(path),
        "source_file": payload.get("source_file"),
        "status": payload.get("status"),
        "is_processing": payload.get("status") in {"writing"},
        "generated_at": payload.get("generated_at"),
        "candidate_count": candidate_count,
        "routed_count": len(routed_items),
        "result_count": result_count,
        "written_count": written_count,
        "deduped_count": deduped_count,
        "pending_approval_count": pending_approval_count,
        "progress_count": result_count,
        "progress_total": candidate_count,
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


def _routed_item_payload(db: Session, item: RoutedItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "domain_key": _domain_key_for_id(db, item.domain_id),
        "agent_id": str(item.agent_id) if item.agent_id else None,
        "task_id": str(item.task_id) if item.task_id else None,
        "report_id": str(item.report_id) if item.report_id else None,
        "seed_package_id": str(item.seed_package_id) if item.seed_package_id else None,
        "artifact_id": str(item.artifact_id) if item.artifact_id else None,
        "route_type": item.route_type,
        "title": item.title,
        "content": item.content,
        "priority": item.priority,
        "status": item.status,
        "source_refs": item.source_refs,
        "metadata": item.metadata_,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _calendar_event_payload(db: Session, event: CalendarEvent) -> dict[str, Any]:
    return CalendarIntelligenceService(db).event_payload(event)


def _ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=home_timezone())


def _coerce_calendar_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, str):
        try:
            return _ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Expected an ISO calendar date/time.") from exc
    raise HTTPException(status_code=422, detail="Expected an ISO calendar date/time.")


def _validate_calendar_window(
    start_at: datetime | None,
    end_at: datetime | None,
    timezone_name: str,
) -> None:
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Unknown timezone: {timezone_name}") from exc
    if (
        start_at is not None
        and end_at is not None
        and ensure_aware_utc(end_at) <= ensure_aware_utc(start_at)
    ):
        raise HTTPException(status_code=422, detail="Event end must be after its start.")


def _todo_payload(db: Session, todo: Todo) -> dict[str, Any]:
    return {
        "id": str(todo.id),
        "domain_key": _domain_key_for_id(db, todo.domain_id),
        "title": todo.title,
        "description": todo.description,
        "todo_type": todo.todo_type,
        "owner_type": todo.owner_type,
        "owner_ref": todo.owner_ref,
        "due_at": todo.due_at.isoformat() if todo.due_at else None,
        "priority": todo.priority,
        "status": todo.status,
        "source_refs": todo.source_refs,
        "provenance": todo.provenance,
        "metadata": todo.metadata_,
        "created_at": todo.created_at.isoformat() if todo.created_at else None,
    }


def _contact_payload(db: Session, contact: Contact) -> dict[str, Any]:
    return ContactIntelligenceService(db).contact_payload(contact)


def _entity_payload(entity: Entity) -> dict[str, Any]:
    return {
        "id": str(entity.id),
        "name": entity.name,
        "website": entity.website,
        "summary": entity.summary,
        "source_refs": entity.source_refs,
        "provenance": entity.provenance,
        "status": entity.status,
        "metadata": entity.metadata_,
        "created_at": entity.created_at.isoformat() if entity.created_at else None,
    }


def _idea_payload(db: Session, idea: Idea) -> dict[str, Any]:
    return {
        "id": str(idea.id),
        "domain_key": _domain_key_for_id(db, idea.domain_id),
        "title": idea.title,
        "content": idea.content,
        "status": idea.status,
        "source_refs": idea.source_refs,
        "provenance": idea.provenance,
        "metadata": idea.metadata_,
        "created_at": idea.created_at.isoformat() if idea.created_at else None,
    }


def _decision_payload(db: Session, decision: DecisionRecord) -> dict[str, Any]:
    return {
        "id": str(decision.id),
        "domain_key": _domain_key_for_id(db, decision.domain_id),
        "title": decision.title,
        "decision": decision.decision,
        "rationale": decision.rationale,
        "status": decision.status,
        "source_refs": decision.source_refs,
        "provenance": decision.provenance,
        "metadata": decision.metadata_,
        "created_at": decision.created_at.isoformat() if decision.created_at else None,
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


def _artifact_payload(db: Session, artifact: Artifact) -> dict[str, Any]:
    memory_count = len(_items_for_artifact(db, artifact.id))
    proposal_count = len(_proposals_for_artifact(db, artifact.id))
    metadata = artifact.metadata_ or {}
    return {
        "id": str(artifact.id),
        "name": artifact.name,
        "artifact_type": artifact.artifact_type,
        "uri": artifact.uri,
        "mime_type": artifact.mime_type,
        "domain_key": str(metadata.get("domain_key") or "global"),
        "task_id": str(artifact.task_id) if artifact.task_id else None,
        "report_id": str(artifact.report_id) if artifact.report_id else None,
        "seed_package_id": str(artifact.seed_package_id) if artifact.seed_package_id else None,
        "memory_count": memory_count,
        "proposal_count": proposal_count,
        "canonical": _artifact_is_canonical_memory_source(artifact),
        "metadata": metadata,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
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


def _items_for_artifact(db: Session, artifact_id: uuid.UUID) -> list[MemoryItem]:
    artifact_id_text = str(artifact_id)
    items = db.scalars(select(MemoryItem).order_by(MemoryItem.created_at.desc())).all()
    return [
        item
        for item in items
        if item.metadata_.get("artifact_id") == artifact_id_text
        or any(ref.get("id") == artifact_id_text for ref in item.metadata_.get("source_refs", []))
    ]


def _proposals_for_artifact(db: Session, artifact_id: uuid.UUID) -> list[MemoryProposal]:
    artifact_id_text = str(artifact_id)
    proposals = db.scalars(select(MemoryProposal).order_by(MemoryProposal.created_at.desc())).all()
    return [
        proposal
        for proposal in proposals
        if proposal.metadata_.get("artifact_id") == artifact_id_text
        or any(ref.get("id") == artifact_id_text for ref in proposal.source_refs)
    ]


def _artifact_is_canonical_memory_source(artifact: Artifact) -> bool:
    metadata = artifact.metadata_ or {}
    return bool(
        metadata.get("canonical_workflow_artifact")
        or metadata.get("canonical_scheduled_workflow_artifact")
        or metadata.get("canonical_session_artifact")
    )


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
    payload["query_relevance"] = retrieved.query_relevance
    payload["semantic_similarity"] = retrieved.semantic_similarity
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


def _context_bundle_payload(
    db: Session,
    bundle: MemoryContextBundle,
    *,
    domain_key: str | None,
) -> dict[str, Any]:
    request = bundle.request
    return {
        "profile": request.profile,
        "audience": request.audience,
        "domain_key": domain_key,
        "agent_id": str(request.agent_id) if request.agent_id else None,
        "query_text": request.query_text,
        "memory_type": sorted(request.memory_types or []),
        "min_importance": request.min_importance,
        "use_semantic": request.use_semantic,
        "egress_target": request.egress_target,
        "semantic_status": bundle.semantic_status,
        "max_items": request.max_items,
        "max_chars": bundle.max_chars,
        "used_chars": bundle.used_chars,
        "total_visible": bundle.total_visible,
        "filtered_count": bundle.filtered_count,
        "policy_filtered_count": bundle.policy_filtered_count,
        "retrieved_count": bundle.retrieved_count,
        "included_count": bundle.included_count,
        "dropped_count": bundle.dropped_count,
        "retrieval_query": {
            "mode": bundle.retrieval_query.mode,
            "limit": bundle.retrieval_query.limit,
            "include_agent_memory": bundle.retrieval_query.include_agent_memory,
            "include_session_memory": bundle.retrieval_query.include_session_memory,
            "include_links": bundle.retrieval_query.include_links,
        },
        "sections": [_context_section_payload(db, section) for section in bundle.sections],
        "rendered_text": bundle.rendered_text,
    }


def _ingestion_record_payload(
    record: IngestionRecord,
    registration: SourceRegistration,
) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "source_registration_key": registration.key,
        "source_system": registration.source_system,
        "external_id": record.external_id,
        "source_version": record.source_version,
        "content_hash": record.content_hash,
        "content_type": record.content_type,
        "source_timestamp": (
            record.source_timestamp.isoformat() if record.source_timestamp else None
        ),
        "status": record.status,
        "attempt_count": record.attempt_count,
        "duplicate_count": record.duplicate_count,
        "last_error": record.last_error,
        "policy": record.policy,
        "seed_package_id": str(record.seed_package_id) if record.seed_package_id else None,
        "artifact_id": str(record.artifact_id) if record.artifact_id else None,
        "created_at": record.created_at.isoformat(),
        "processed_at": record.processed_at.isoformat() if record.processed_at else None,
    }


def _context_section_payload(db: Session, section: MemoryContextSection) -> dict[str, Any]:
    return {
        "key": section.key,
        "label": section.label,
        "used_chars": section.used_chars,
        "memories": [_context_snippet_payload(db, snippet) for snippet in section.snippets],
    }


def _context_snippet_payload(db: Session, snippet: MemoryContextSnippet) -> dict[str, Any]:
    return {
        **_retrieved_memory_payload(
            db,
            RetrievedMemory(
                memory=snippet.memory,
                score=snippet.score,
                query_relevance=snippet.query_relevance,
                semantic_similarity=snippet.semantic_similarity,
                score_reasons=snippet.score_reasons,
                provenance=snippet.provenance,
                links=snippet.links,
            ),
        ),
        "excerpt": snippet.excerpt,
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


def _memory_hygiene_payload(run: MemoryHygieneRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "status": run.status,
        "scanned_count": run.scanned_count,
        "embedding_backfilled_count": run.embedding_backfilled_count,
        "provenance_repaired_count": run.provenance_repaired_count,
        "duplicate_merged_count": run.duplicate_merged_count,
        "proposal_count": run.proposal_count,
        "error_message": run.error_message,
        "details": run.details,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }
