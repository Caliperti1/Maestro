"""Owner and device APIs for remotely executing durable Maestro node jobs."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.models import ExecutionNode
from app.db.session import get_db
from app.nodes.security import verify_payload_signature
from app.nodes.service import NodeService, NodeServiceError, utcnow

router = APIRouter(tags=["nodes"])


def require_node_admin() -> None:
    """Pluggable owner-auth boundary; override when the application auth layer lands."""
    return


class CapabilityBody(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    version: str | None = Field(default=None, max_length=80)
    status: str = Field(default="ready", min_length=1, max_length=40)
    mode: str | None = Field(default=None, max_length=40)
    details: dict[str, Any] = Field(default_factory=dict)


CapabilityInput = str | CapabilityBody


class EnrollmentTokenBody(BaseModel):
    expires_in_seconds: int = Field(default=900, ge=60, le=86400)
    allowed_capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EnrollBody(BaseModel):
    enrollment_code: str = Field(min_length=20, max_length=256)
    display_name: str = Field(min_length=1, max_length=200)
    platform: str = Field(min_length=1, max_length=80)
    client_version: str | None = Field(default=None, max_length=80)
    public_key: str = Field(min_length=40, max_length=256)
    capabilities: list[CapabilityInput] = Field(default_factory=list)


class HeartbeatBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    protocol_version: str | None = None
    node_id: uuid.UUID | None = None
    client_version: str | None = Field(default=None, max_length=80)
    platform: str | None = Field(default=None, max_length=320)
    hostname: str | None = Field(default=None, max_length=320)
    capabilities: list[CapabilityInput] | None = None
    metadata: dict[str, Any] | None = None


class LeaseBody(BaseModel):
    node_id: uuid.UUID | None = None
    capabilities: list[str] = Field(default_factory=list)
    wait_seconds: int = Field(default=0, ge=0, le=25)
    lease_seconds: int = Field(default=60, ge=15, le=3600)


class RenewBody(BaseModel):
    lease_token: str = Field(min_length=20, max_length=256)
    lease_generation: int | None = Field(default=None, ge=1)
    lease_seconds: int = Field(default=60, ge=15, le=3600)


class CompleteBody(BaseModel):
    lease_token: str = Field(min_length=20, max_length=256)
    lease_generation: int | None = Field(default=None, ge=1)
    output_envelope: dict[str, Any] | None = None
    result_envelope: dict[str, Any] | None = None
    signature: str | None = Field(default=None, max_length=4096)


class FailBody(BaseModel):
    lease_token: str = Field(min_length=20, max_length=256)
    lease_generation: int | None = Field(default=None, ge=1)
    error_message: str | None = Field(default=None, max_length=10000)
    message: str | None = Field(default=None, max_length=10000)
    error_code: str | None = Field(default=None, max_length=160)
    retryable: bool = False


class DiagnosticJobBody(BaseModel):
    message: str = Field(default="hello from Maestro", min_length=1, max_length=10000)
    idempotency_key: str | None = Field(default=None, max_length=240)


class PauseBody(BaseModel):
    paused: bool


def _service_error(exc: NodeServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _capabilities(values: list[CapabilityInput]) -> list[str | dict[str, Any]]:
    normalized: list[str | dict[str, Any]] = []
    for value in values:
        if isinstance(value, str):
            normalized.append(value)
            continue
        item = value.model_dump()
        if item.pop("mode", None) is not None:
            item["details"] = {**item["details"], "mode": value.mode}
        normalized.append(item)
    return normalized


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def authenticated_node(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> ExecutionNode:
    try:
        return NodeService(db).authenticate(_bearer_token(authorization))
    except NodeServiceError as exc:
        raise _service_error(exc) from exc


@router.post("/nodes/enrollment-tokens", dependencies=[Depends(require_node_admin)])
def create_enrollment_token(
    body: EnrollmentTokenBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    enrollment, raw_token = NodeService(db).create_enrollment_token(
        expires_in_seconds=body.expires_in_seconds,
        allowed_capabilities=body.allowed_capabilities,
        metadata=body.metadata,
    )
    return {
        "token_id": str(enrollment.id),
        "enrollment_code": raw_token,
        "expires_at": enrollment.expires_at,
        "allowed_capabilities": enrollment.allowed_capabilities,
    }


@router.post("/nodes/enroll")
def enroll_node(body: EnrollBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    service = NodeService(db)
    try:
        node, access_token = service.enroll(
            enrollment_code=body.enrollment_code,
            display_name=body.display_name,
            platform=body.platform,
            client_version=body.client_version,
            public_key=body.public_key,
            capabilities=_capabilities(body.capabilities),
        )
    except NodeServiceError as exc:
        raise _service_error(exc) from exc
    return {
        "node_id": str(node.id),
        "access_token": access_token,
        "node": service.node_payload(node),
    }


@router.get("/nodes", dependencies=[Depends(require_node_admin)])
def list_nodes(db: Session = Depends(get_db)) -> dict[str, Any]:
    service = NodeService(db)
    return {"nodes": [service.node_payload(node) for node in service.list_nodes()]}


@router.get("/nodes/{node_id}", dependencies=[Depends(require_node_admin)])
def get_node(node_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    service = NodeService(db)
    try:
        node = service.get_node(node_id)
    except NodeServiceError as exc:
        raise _service_error(exc) from exc
    return {
        "node": service.node_payload(node),
        "jobs": [service.job_payload(job) for job in service.jobs.list_by_node(node.id)],
        "events": [service.event_payload(event) for event in service.events.list_by_node(node.id)],
    }


@router.post("/nodes/{node_id}/diagnostic-jobs", dependencies=[Depends(require_node_admin)])
def create_diagnostic_job(
    node_id: uuid.UUID,
    body: DiagnosticJobBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = NodeService(db)
    try:
        job = service.create_diagnostic_job(
            node_id,
            message=body.message,
            idempotency_key=body.idempotency_key,
        )
    except NodeServiceError as exc:
        raise _service_error(exc) from exc
    return {"job": service.job_payload(job)}


@router.patch("/nodes/{node_id}/pause", dependencies=[Depends(require_node_admin)])
def pause_node(
    node_id: uuid.UUID,
    body: PauseBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = NodeService(db)
    try:
        node = service.set_paused(node_id, paused=body.paused)
    except NodeServiceError as exc:
        raise _service_error(exc) from exc
    return {"node": service.node_payload(node)}


@router.post("/nodes/{node_id}/revoke", dependencies=[Depends(require_node_admin)])
def revoke_node(node_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    service = NodeService(db)
    try:
        node = service.revoke_node(node_id)
    except NodeServiceError as exc:
        raise _service_error(exc) from exc
    return {"node": service.node_payload(node)}


@router.post("/node/v1/heartbeat")
def heartbeat(
    body: HeartbeatBody,
    node: ExecutionNode = Depends(authenticated_node),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = NodeService(db)
    if body.node_id is not None and body.node_id != node.id:
        raise HTTPException(status_code=403, detail="Heartbeat node_id does not match bearer token.")
    try:
        heartbeat_metadata = body.metadata
        if heartbeat_metadata is None:
            heartbeat_metadata = {}
        if body.platform is not None:
            heartbeat_metadata["platform_detail"] = body.platform
        if body.hostname is not None:
            heartbeat_metadata["hostname"] = body.hostname
        if body.protocol_version is not None:
            heartbeat_metadata["protocol_version"] = body.protocol_version
        updated = service.heartbeat(
            node,
            client_version=body.client_version,
            capabilities=None if body.capabilities is None else _capabilities(body.capabilities),
            metadata=heartbeat_metadata,
        )
    except NodeServiceError as exc:
        raise _service_error(exc) from exc
    return {
        "status": "ok",
        "server_time": utcnow(),
        "heartbeat_interval_seconds": 30,
        "node": service.node_payload(updated),
    }


@router.post("/node/v1/jobs/lease")
async def lease_job(
    body: LeaseBody,
    node: ExecutionNode = Depends(authenticated_node),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = NodeService(db)
    if body.node_id is not None and body.node_id != node.id:
        raise HTTPException(status_code=403, detail="Lease node_id does not match bearer token.")
    deadline = time.monotonic() + body.wait_seconds
    while True:
        try:
            leased = service.lease_job(
                node,
                advertised_capabilities=body.capabilities,
                lease_seconds=body.lease_seconds,
            )
        except NodeServiceError as exc:
            raise _service_error(exc) from exc
        if leased is not None:
            job, lease_token = leased
            payload = service.job_payload(job, include_envelopes=False)
            payload.update(
                job_id=str(job.id),
                capability=job.capability_key,
                lease_token=lease_token,
                lease_generation=job.attempt_count,
                lease_seconds=body.lease_seconds,
                correlation_id=str(job.task_id) if job.task_id else None,
                input_envelope=job.input_envelope,
            )
            return {"job": payload}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"job": None}
        await asyncio.sleep(min(1.0, remaining))
        db.expire_all()


@router.post("/node/v1/jobs/{job_id}/renew")
def renew_job(
    job_id: uuid.UUID,
    body: RenewBody,
    node: ExecutionNode = Depends(authenticated_node),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = NodeService(db)
    try:
        job = service.renew_job(
            node,
            job_id,
            lease_token=body.lease_token,
            lease_seconds=body.lease_seconds,
            lease_generation=body.lease_generation,
        )
    except NodeServiceError as exc:
        raise _service_error(exc) from exc
    return {"job": service.job_payload(job, include_envelopes=False)}


@router.post("/node/v1/jobs/{job_id}/complete")
def complete_job(
    job_id: uuid.UUID,
    body: CompleteBody,
    node: ExecutionNode = Depends(authenticated_node),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = NodeService(db)
    output_envelope = body.output_envelope
    if output_envelope is None:
        if body.result_envelope is None:
            raise HTTPException(status_code=422, detail="A result envelope is required.")
        if not verify_payload_signature(node.public_key, body.result_envelope, body.signature):
            raise HTTPException(status_code=403, detail="Invalid node result signature.")
        output_envelope = {
            "result_envelope": body.result_envelope,
            "signature": body.signature,
        }
    else:
        signed_payload = output_envelope.get("result_envelope", output_envelope)
        signature = body.signature or output_envelope.get("signature")
        if not isinstance(signed_payload, dict) or not verify_payload_signature(
            node.public_key, signed_payload, signature
        ):
            raise HTTPException(status_code=403, detail="Invalid node result signature.")
    try:
        job = service.complete_job(
            node,
            job_id,
            lease_token=body.lease_token,
            output_envelope=output_envelope,
            lease_generation=body.lease_generation,
        )
    except NodeServiceError as exc:
        raise _service_error(exc) from exc
    return {"job": service.job_payload(job)}


@router.post("/node/v1/jobs/{job_id}/fail")
def fail_job(
    job_id: uuid.UUID,
    body: FailBody,
    node: ExecutionNode = Depends(authenticated_node),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = NodeService(db)
    error_message = body.error_message or body.message
    if not error_message:
        raise HTTPException(status_code=422, detail="An error message is required.")
    try:
        job = service.fail_job(
            node,
            job_id,
            lease_token=body.lease_token,
            error_message=error_message,
            retryable=body.retryable,
            lease_generation=body.lease_generation,
            error_code=body.error_code,
        )
    except NodeServiceError as exc:
        raise _service_error(exc) from exc
    return {"job": service.job_payload(job)}
